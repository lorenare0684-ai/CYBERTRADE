"""Browser-terminal pairing: the same Chrome flow, driven over HTTP.

The desktop GUI pairs from a Tk window; the browser terminal pairs from
``/api/pair/*``. The hard part — a human logging in and solving a CAPTCHA
across several requests — is identical, so these tests care about the things
HTTP makes different:

- the terminal must boot with **no engine** when no session exists, and must
  not report a balance, a candle or an open order it does not have;
- a cookie landing on a worker thread must be adopted serially, exactly once;
- re-pairing a live engine must re-seat the api, never rebuild the engine;
- a cancelled or superseded attempt must not attach anything.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import unittest
import unittest.mock as mock
import itertools
import urllib.error
import urllib.request

from cybertrade.config import AppConfig
from cybertrade.web.server import WebTerminal
from cybertrade.exceptions import BrokerConnectionError, ConfigError

from tests.venue_stubs import VenueFeed, VenueStub

PAIR = "cybertrade.web.pairing"

# each test boots its own terminal and never stops it (the thread is a
# daemon), so ports must never be reused or a stale server answers.
_PORT = itertools.count(18731)


def _cfg(tmp, name="qx.json"):
    cfg = AppConfig()
    cfg.qx_session_path = os.path.join(tmp, name)
    cfg.journal_path = os.path.join(tmp, "journal.db")
    cfg.calibration_path = os.path.join(tmp, "cal.json")
    cfg.operator_path = os.path.join(tmp, "operator.json")
    cfg.continuity_path = os.path.join(tmp, "continuity.json")
    cfg.heartbeat_path = os.path.join(tmp, "heartbeat.json")
    return cfg


class _StubApi:
    """The seam ``_reseat_session`` touches; no venue protocol, no network."""

    def __init__(self, ssid="", connected=True):
        self.ssid = ssid
        self.connected = connected

    def set_ssid(self, ssid, cookies=""):
        self.ssid = ssid
        return ssid

    def connect(self, authorize=True):
        return self.connected


class _StubEngine:
    """A real engine on the stub venue, with a stubbed api for re-seating.

    It has to be the genuine :class:`TradingEngine` because ``hub.state()``
    walks the whole machine — snapshot, oms, survivor, corr, ensemble — and a
    hand-rolled double would be testing the hub against a fiction. Only the
    venue api is faked, so ``_reseat_session`` exercises the real seam with no
    network.
    """

    def __init__(self, connected=True):
        from cybertrade.bot.engine import TradingEngine

        cfg = AppConfig()
        tmp = tempfile.mkdtemp(prefix="stub-eng-")
        cfg.journal_path = os.path.join(tmp, "j.db")
        cfg.calibration_path = os.path.join(tmp, "c.json")
        cfg.operator_path = os.path.join(tmp, "o.json")
        cfg.continuity_path = os.path.join(tmp, "k.json")
        cfg.heartbeat_path = os.path.join(tmp, "h.json")
        feed = VenueFeed(assets=["EURUSD_otc"])
        feed.api = _StubApi(connected=connected)
        self._eng = TradingEngine(cfg, feed=feed, broker=VenueStub())
        self._eng.config = cfg

    def __getattr__(self, name):
        return getattr(self._eng, name)

    def shutdown(self):
        self._eng.shutdown()
        self.closed = True


class TestPairingController(unittest.TestCase):
    """The state machine behind ``/api/pair/*``."""

    def setUp(self):
        from cybertrade.web.pairing import PairingController

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)
        self.ready = []
        self.ctl = PairingController(self.cfg,
                                    lambda ssid, purse, cookies="": self.ready.append(
                                        (ssid, purse, cookies)))
        self._real = PairingController.start.__globals__["run_pairing"]

    def tearDown(self):
        import cybertrade.web.pairing as wp

        wp.run_pairing = self._real

    def _ok(self, sess=None):
        import cybertrade.web.pairing as wp

        # `sess or {...}` would turn an empty dict into the happy path
        payload = {"ssid": "QX.x"} if sess is None else sess
        wp.run_pairing = lambda **kw: kw["on_done"](payload)

    def _boom(self, exc):
        import cybertrade.web.pairing as wp

        wp.run_pairing = lambda **kw: kw["on_error"](exc)

    # -- gating ------------------------------------------------------------
    def test_no_purse_never_pairs(self):
        r = self.ctl.start("data/chrome-profile", 9333, 240, None)
        self.assertFalse(r["ok"])
        self.assertIn("purse", r["error"])
        self.assertEqual(self.ctl.state(), "idle")
        self.assertEqual(self.ready, [])

    def test_a_numeric_purse_string_is_refused(self):
        r = self.ctl.start("p", 9333, 240, "practice")
        self.assertFalse(r["ok"])
        self.assertEqual(self.ready, [])

    def test_bad_form_never_launches_chrome(self):
        r = self.ctl.start("p", "not-a-port", 240, True)
        self.assertFalse(r["ok"])
        self.assertIn("port", r["error"])
        self.assertEqual(self.ctl.state(), "idle")

    def test_a_blank_profile_falls_back_to_the_default(self):
        seen = {}
        import cybertrade.web.pairing as wp

        def spy(**kw):
            seen.update(kw)
            return kw["on_done"]({"ssid": "QX.x"})

        wp.run_pairing = spy
        self.ctl.start("", 9333, 240, True)
        self.assertEqual(seen["profile"], "data/chrome-profile")

    # -- the state machine -------------------------------------------------
    def test_success_reports_ready_and_the_purse(self):
        self._ok()
        r = self.ctl.start("data/chrome-profile", 9333, 240, False)
        self.assertTrue(r["ok"])
        # WAITING is set *before* the launch, so a worker that finishes first
        # is never clobbered by start()'s own bookkeeping
        self.assertEqual(self.ctl.state(), "ready")
        self.assertEqual(self.ready, [("QX.x", False, "")])

    def test_a_synchronous_launcher_is_not_clobbered(self):
        """The bug this guards: start() used to overwrite the worker's READY."""
        self._ok()
        self.ctl.start("data/chrome-profile", 9333, 240, True)
        st = self.ctl.status()
        self.assertEqual(st["state"], "ready")
        self.assertTrue(st["ssid_present"])

    def test_failure_is_reported_and_recovers(self):
        self._boom(TimeoutError("no session after 240s"))
        self.ctl.start("data/chrome-profile", 9333, 240, True)
        st = self.ctl.status()
        self.assertEqual(st["state"], "failed")
        self.assertIn("no session after 240s", st["message"])
        self.assertEqual(self.ready, [])
        # the operator can simply try again
        self._ok()
        r = self.ctl.start("data/chrome-profile", 9333, 240, True)
        self.assertTrue(r["ok"])
        self.assertEqual(self.ctl.state(), "ready")

    def test_an_empty_cookie_is_a_failure_not_a_ready(self):
        self._ok({})
        self.ctl.start("data/chrome-profile", 9333, 240, True)
        self.assertEqual(self.ctl.state(), "failed")
        self.assertEqual(self.ready, [])

    def test_a_second_start_is_refused_while_busy(self):
        import cybertrade.web.pairing as wp

        started = []
        wp.run_pairing = lambda **kw: started.append(1)
        self.ctl.start("data/chrome-profile", 9333, 240, True)
        r = self.ctl.start("data/chrome-profile", 9333, 240, True)
        self.assertFalse(r["ok"])
        self.assertIn("already running", r["error"])
        self.assertEqual(len(started), 1)

    def test_cancel_ignores_a_late_cookie(self):
        """A cookie arriving after cancel must not attach anything."""
        import cybertrade.web.pairing as wp

        hold = {}
        wp.run_pairing = lambda **kw: hold.update(kw)
        self.ctl.start("data/chrome-profile", 9333, 240, True)
        r = self.ctl.cancel()
        self.assertTrue(r["ok"])
        self.assertEqual(self.ctl.state(), "idle")
        # the worker finally reports in — too late
        hold["on_done"]({"ssid": "QX.late"})
        self.assertEqual(self.ctl.state(), "idle")
        self.assertEqual(self.ready, [])

    def test_a_superseded_attempt_cannot_report(self):
        import cybertrade.web.pairing as wp

        hold = {}
        wp.run_pairing = lambda **kw: hold.update(kw)
        self.ctl.start("data/chrome-profile", 9333, 240, True)
        self.ctl.cancel()
        self._ok({"ssid": "QX.new"})
        self.ctl.start("data/chrome-profile", 9333, 240, True)
        self.assertEqual(self.ctl.state(), "ready")
        # the stale worker's result is dropped, the fresh one wins
        hold["on_done"]({"ssid": "QX.stale"})
        self.assertEqual(self.ready, [("QX.new", True, "")])

    def test_a_live_terminal_does_not_claim_to_have_no_session(self):
        """The message an operator reads when opening RE-PAIR on a live engine."""
        from cybertrade.web.pairing import PairingController

        live = PairingController(self.cfg, lambda s, p: None, probe=lambda: True)
        st = live.status()
        self.assertTrue(st["active"])
        self.assertIn("live", st["message"])
        self.assertNotIn("no session yet", st["message"])

    def test_a_broken_probe_never_breaks_status(self):
        from cybertrade.web.pairing import PairingController

        def boom():
            raise RuntimeError("probe exploded")

        ctl = PairingController(self.cfg, lambda s, p: None, probe=boom)
        self.assertFalse(ctl.status()["active"])

    def test_status_describes_the_disk_session(self):
        st = self.ctl.status()
        self.assertIn("session", st)
        self.assertEqual(st["session_path"], self.cfg.qx_session_path)
        self.assertFalse(st["busy"])

    def test_captured_but_unadopted_reports_adopting(self):
        """READY-the-cookie vs READY-the-terminal are minutes apart.

        Reporting "ready" while the engine boots makes the browser veil
        flap open/closed, so a probed controller reports "adopting" until
        the engine actually holds the session.
        """
        from cybertrade.web.pairing import PairingController

        live = {"on": False}
        ctl = PairingController(self.cfg, lambda s, p, c="": None,
                                probe=lambda: live["on"])
        ctl._done({"ssid": "QX.x"}, ctl._epoch)
        st = ctl.status()
        self.assertEqual(st["state"], "adopting")
        self.assertTrue(st["busy"])
        live["on"] = True
        st = ctl.status()
        self.assertEqual(st["state"], "ready")
        self.assertFalse(st["busy"])

    def test_note_error_fails_with_a_way_back(self):
        from cybertrade.web.pairing import PairingController

        ctl = PairingController(self.cfg, lambda s, p: None,
                                probe=lambda: False)
        ctl._done({"ssid": "QX.x"}, ctl._epoch)
        r = ctl.note_error("venue unreachable")
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "failed")
        self.assertIn("venue unreachable", r["message"])
        # the operator can pair again without restarting anything
        self._ok()
        r2 = ctl.start("data/chrome-profile", 9333, 240, True)
        self.assertTrue(r2["ok"])


class TestHubWithoutAnEngine(unittest.TestCase):
    """The payload the browser gets before any session exists."""

    def setUp(self):
        from cybertrade.web.server import EngineHub

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)
        self.hub = EngineHub(None, self.cfg)

    def test_state_reports_no_engine_and_no_balances(self):
        st = self.hub.state()
        self.assertFalse(st["paired"])
        self.assertEqual(st["engine_state"], "pairing")
        self.assertEqual(st["assets"], [])
        self.assertEqual(st["trades"], [])
        self.assertEqual(st["candles"], {})
        # a balance, a position or a candle it does not have must not appear
        self.assertEqual(st["snapshot"]["health"]["balance"], 0.0)
        self.assertEqual(st["snapshot"]["health"]["open_positions"], 0)

    def test_logs_still_flow_without_an_engine(self):
        self.hub._on_log(type("E", (), {"payload": {
            "ts": 1.0, "level": "INFO", "name": "x", "msg": "boot"}})())
        self.assertEqual(len(self.hub.state()["logs"]), 1)

    def test_a_real_engine_reports_paired(self):
        from cybertrade.web.server import EngineHub

        eng = _StubEngine()
        hub = EngineHub(eng, self.cfg)
        st = hub.state()
        self.assertTrue(st["paired"])
        self.assertIn("pairing", st)


class TestQuietHttpServer(unittest.TestCase):
    """Dropped browser connections must not print tracebacks.

    Stock ``socketserver`` logs a full traceback every time a browser
    aborts an idle keep-alive connection (Windows: ``ConnectionAbortedError``
    10053), burying real errors.  Only those exact failures go quiet.
    """

    def _server(self):
        from http.server import BaseHTTPRequestHandler

        from cybertrade.web.server import _QuietHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def handle(self):  # pragma: no cover - never serves
                pass

        srv = _QuietHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(srv.server_close)
        return srv

    def _handled(self, srv, exc):
        import io
        from contextlib import redirect_stderr

        try:
            raise exc
        except Exception:
            buf = io.StringIO()
            with redirect_stderr(buf):
                srv.handle_error(None, ("127.0.0.1", 1))
            return buf.getvalue()

    def test_aborted_connections_are_silent(self):
        srv = self._server()
        for exc in (ConnectionAbortedError(10053, "aborted"),
                    ConnectionResetError(104, "reset"),
                    BrokenPipeError(32, "broken pipe")):
            self.assertEqual(self._handled(srv, exc), "", repr(exc))

    def test_real_errors_still_shout(self):
        srv = self._server()
        out = self._handled(srv, ValueError("boom"))
        self.assertIn("ValueError", out)


class TestWebBootsWithoutASession(unittest.TestCase):
    """``cmd_web`` must serve a pairing screen, not refuse to start."""

    def setUp(self):
        import cybertrade.cli as cli
        import cybertrade.web.pairing as wp

        self.cli = cli
        self.wp = wp
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)
        self.built = []
        self.reseats = []
        self._real_build = cli._build_engine
        self._real_reseat = cli._reseat_session

        def fake_build(cfg, durable=False):
            if not cfg.broker.ssid:
                raise ConfigError(
                    "no Quotex session — run `cybertrade quotex login`")
            self.built.append(cfg.broker.ssid)
            return _StubEngine()

        def fake_reseat(engine, ssid, cookies=""):
            self.reseats.append(ssid)
            return self._real_reseat(engine, ssid, cookies)

        cli._build_engine = fake_build
        cli._reseat_session = fake_reseat

    def tearDown(self):
        self.cli._build_engine = self._real_build
        self.cli._reseat_session = self._real_reseat

    def _boot(self, auto=True):
        """Start the terminal with no session and wait for it to listen."""
        import threading

        port = next(_PORT)
        args = argparse.Namespace(host="127.0.0.1", port=port, auto=auto)
        errs = []

        def work():
            try:
                self.cli._run_web(self.cfg, args)
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        threading.Thread(target=work, daemon=True).start()
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
                    st = json.loads(r.read())
                return port, st, errs
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        self.fail(f"terminal never came up: {errs}")

    def _pair(self, port, ssid, purse=True):
        """POST a pairing whose cookie lands immediately."""
        self.wp.run_pairing = lambda **kw: kw["on_done"]({"ssid": ssid})
        body = json.dumps({"profile": "data/chrome-profile", "port": 9333,
                           "timeout": 30, "purse": purse}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/pair/start", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            started = json.loads(r.read())
        self.assertTrue(started["ok"], started)
        return started

    def _wait_paired(self, port):
        for _ in range(80):
            time.sleep(0.1)
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
                st = json.loads(r.read())
            if st["paired"]:
                return st
        self.fail("engine never attached after pairing")

    def test_boots_unpaired_and_pairs_from_http(self):
        port, st, errs = self._boot()
        self.assertFalse(st["paired"])
        self.assertEqual(st["engine_state"], "pairing")
        self.assertEqual(self.built, [])            # no engine before a cookie
        self._pair(port, "QX.first")
        st = self._wait_paired(port)
        self.assertEqual(self.built, ["QX.first"])   # built exactly once
        self.assertTrue(st["paired"])

    def test_the_ssid_is_never_sent_to_the_browser(self):
        port, _st, _errs = self._boot()
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/pair/status", timeout=5) as r:
            raw = r.read().decode("utf-8")
        self.assertNotIn("QX.first", raw)
        self.assertIn("ssid_present", raw)

    def test_a_rebuild_never_happens_for_a_live_engine(self):
        port, _st, _errs = self._boot()
        self._pair(port, "QX.first")
        self._wait_paired(port)
        # a re-pair's status stays "ready" from the first success, so wait on
        # the observable effect rather than the state word
        self._pair(port, "QX.second", purse=False)
        for _ in range(100):
            time.sleep(0.1)
            if self.reseats:
                break
        else:
            self.fail("the second cookie never reached the running engine")
        self.assertEqual(self.built, ["QX.first"])    # only the first boot
        self.assertEqual(self.reseats, ["QX.second"],
                         "the second cookie must re-seat, not rebuild")

    def test_other_config_errors_still_fail_loudly(self):
        def boom(cfg, durable=False):
            raise ConfigError("broker.mode='paper' is not supported")

        self.cli._build_engine = boom
        args = argparse.Namespace(host="127.0.0.1", port=8901, auto=True)
        with self.assertRaises(ConfigError):
            self.cli._run_web(self.cfg, args)

    def test_dead_venue_serves_pairing_instead_of_dying(self):
        """A rejected/unreachable venue must not kill the terminal.

        Regression: ``FeedError``/``BrokerConnectionError`` from the first
        engine build propagated out of ``_run_web`` and took the whole
        server (and its pairing screen) down with it.
        """
        def down(cfg, durable=False):
            raise BrokerConnectionError("venue down")

        self.cli._build_engine = down
        port, st, errs = self._boot()
        self.assertEqual(errs, [])
        self.assertFalse(st["paired"])
        self.assertEqual(st["engine_state"], "pairing")

    def test_failed_adoption_reports_failed_and_stays_up(self):
        """A cookie the engine cannot start on fails the veil, not the process."""
        port, _st, _errs = self._boot()

        def down(cfg, durable=False):
            raise BrokerConnectionError("venue down")

        self.cli._build_engine = down
        self._pair(port, "QX.doomed")
        for _ in range(60):
            time.sleep(0.2)
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/pair/status",
                    timeout=5) as r:
                st = json.loads(r.read())
            if st["state"] == "failed":
                break
        else:
            self.fail("adoption failure never reached the veil")
        self.assertIn("venue down", st["message"])
        # and the terminal is still serving the pairing screen
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
            state = json.loads(r.read())
        self.assertFalse(state["paired"])

    def test_adopting_shown_until_engine_attaches(self):
        """The veil must not claim "ready" while the engine still boots."""
        def slow_build(cfg, durable=False):
            if not cfg.broker.ssid:
                raise ConfigError("no Quotex session")
            time.sleep(2.0)
            eng = _StubEngine()
            eng._eng.feed.api.ssid = cfg.broker.ssid
            return eng

        self.cli._build_engine = slow_build
        port, _st, _errs = self._boot()
        self._pair(port, "QX.slow")
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/pair/status",
                timeout=5) as r:
            st = json.loads(r.read())
        self.assertEqual(st["state"], "adopting")
        self._wait_paired(port)
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/pair/status",
                timeout=5) as r:
            st = json.loads(r.read())
        self.assertEqual(st["state"], "ready")


class TestReseatSession(unittest.TestCase):
    """Re-pairing a live engine must not restart it."""

    def setUp(self):
        import cybertrade.cli as cli

        self.cli = cli
        self.eng = _StubEngine()

    def test_the_api_is_reseeded_in_place(self):
        self.cli._reseat_session(self.eng, "QX.new")
        self.assertEqual(self.eng.feed.api.ssid, "QX.new")

    def test_a_rejected_session_raises(self):
        eng = _StubEngine(connected=False)
        with self.assertRaises(ConfigError):
            self.cli._reseat_session(eng, "QX.new")

    def test_an_engine_without_an_api_is_reported(self):
        eng = _StubEngine()
        del eng.feed.api
        with self.assertRaises(ConfigError):
            self.cli._reseat_session(eng, "QX.new")


class TestHeadRequests(unittest.TestCase):
    """A HEAD must not answer 501: probes and browsers lead with it."""

    def setUp(self):
        from cybertrade.web.server import EngineHub

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = _cfg(self.tmp.name)
        hub = EngineHub(_StubEngine(), cfg)
        self.web = WebTerminal(hub, host="127.0.0.1", port=next(_PORT))
        self.web.start()

    def tearDown(self):
        self.web.stop()

    def _head(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.web.port}{path}", method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def test_static_head_reports_its_type_and_no_body(self):
        for path, want in (("/", "text/html"),
                           ("/static/js/app.js", "text/javascript"),
                           ("/static/css/cyber.css", "text/css")):
            status, ctype, body = self._head(path)
            self.assertEqual(status, 200, path)
            self.assertIn(want, ctype or "")
            self.assertIn("charset=utf-8", ctype or "")
            self.assertEqual(body, b"", path)      # headers only

    def test_api_head_works_too(self):
        status, _ctype, body = self._head("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_an_unknown_path_still_404s(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._head("/nope")
        self.assertEqual(ctx.exception.code, 404)



class TestHostileRequests(unittest.TestCase):
    """A malformed request must never kill the connection.

    An exception escaping a handler does not just lose that request: it tears
    down the keep-alive connection mid-response, and the console gets a dead
    socket where a JSON error would have told the operator what broke. These
    are the payloads a browser, a proxy or a stray script actually send.
    """

    def setUp(self):
        from cybertrade.web.pairing import PairingController
        from cybertrade.web.server import EngineHub

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = _cfg(self.tmp.name)
        hub = EngineHub(None, cfg)          # unpaired: the first-boot state
        hub.pairing = PairingController(cfg, lambda ssid, purse: None)
        self.web = WebTerminal(hub, host="127.0.0.1", port=next(_PORT))
        self.web.start()
        self.addCleanup(self.web.stop)

    def _post(self, path, body):
        raw = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.web.port}{path}", data=raw,
            method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _get(self, path):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.web.port}{path}", timeout=8) as r:
                return r.status, self._decode(r.read())
        except urllib.error.HTTPError as e:
            return e.code, self._decode(e.read())

    @staticmethod
    def _decode(raw):
        """Static assets are not JSON; keep whatever came back."""
        try:
            return json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"_raw_len": len(raw)}

    def _must_not_500(self, method, path, body=None):
        status, payload = self._post(path, body) if method == "POST" \
            else self._get(path)
        self.assertNotEqual(status, 500, f"{method} {path} -> 500: {payload}")
        return status, payload

    def test_a_non_object_json_body_is_rejected_not_fatal(self):
        for body in ("[]", "null", "3", '"text"', "{bad json", "", "{"):
            with self.subTest(body=body):
                self._must_not_500("POST", "/api/command", body)
                self._must_not_500("POST", "/api/pair/start", body)

    def test_a_command_with_no_engine_says_pair_first(self):
        status, payload = self._must_not_500("POST", "/api/command", '{"cmd":"arm"}')
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("no venue session", payload["error"])
        self.assertTrue(payload["pairing"])

    def test_an_unknown_command_is_answered_not_crashed(self):
        status, payload = self._must_not_500("POST", "/api/command",
                                            '{"cmd":"' + "z" * 4000 + '"}')
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)
        self.assertNotIn("Traceback", json.dumps(payload))

    def test_a_garbage_query_string_is_clamped_not_fatal(self):
        for qs in ("runs=abc", "runs=-5", "runs=99999999", "runs=50&horizon=abc",
                   "runs=1e9", "runs=", "runs=%20%20"):
            with self.subTest(qs=qs):
                status, payload = self._must_not_500("GET", f"/api/montecarlo?{qs}")
                self.assertEqual(status, 200)
                # unpaired: the HUD renders NO DATA rather than a 500
                self.assertTrue(payload.get("empty"), payload)

    def test_engine_routes_degrade_when_unpaired(self):
        for path in ("/api/strategies", "/api/calendar", "/api/montecarlo"):
            with self.subTest(path=path):
                status, payload = self._must_not_500("GET", path)
                self.assertEqual(status, 200)
                self.assertNotIn("NoneType", json.dumps(payload))

    def test_a_method_we_do_not_serve_is_a_405_not_a_501(self):
        for method in ("PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.web.port}/api/state", method=method)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 405, method)

    def test_a_bad_content_length_header_does_not_tear_down_the_server(self):
        import socket

        s = socket.create_connection(("127.0.0.1", self.web.port), timeout=5)
        try:
            s.sendall(b"POST /api/command HTTP/1.1\r\nHost: x\r\n"
                      b"Content-Length: not-a-number\r\n"
                      b"Connection: close\r\n\r\n")
            raw = s.recv(400)
        finally:
            s.close()
        self.assertIn(b"400", raw.split(b"\r\n")[0])
        # the server is still serving afterwards
        status, _payload = self._get("/api/state")
        self.assertEqual(status, 200)

    def test_the_server_survives_a_whole_battery_of_junk(self):
        """Nothing above may leave the listener wedged."""
        paths = ["/" + "A" * 4000, "/api/state", "/api/pair/status",
                 "/static/js/app.js", "/static/../server.py", "/nope",
                 "/api/montecarlo?runs=abc", "/api/montecarlo?runs=999999"]
        for path in paths:
            with self.subTest(path=path[:40]):
                self._must_not_500("GET", path)
        status, _payload = self._get("/api/state")
        self.assertEqual(status, 200)



class TestUnknownCommandOnALiveTerminal(unittest.TestCase):
    """A paired terminal must answer an unknown command, not crash."""

    def setUp(self):
        from cybertrade.web.server import EngineHub

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = _cfg(self.tmp.name)
        hub = EngineHub(_StubEngine(), cfg)
        self.web = WebTerminal(hub, host="127.0.0.1", port=next(_PORT))
        self.web.start()
        self.addCleanup(self.web.stop)

    def test_unknown_cmd_is_reported(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.web.port}/api/command",
            data=json.dumps({"cmd": "explode"}).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            payload = json.loads(r.read())
        self.assertFalse(payload["ok"])
        self.assertIn("unknown cmd", payload["error"])

    def test_a_non_object_body_is_a_400(self):
        for body in ("[]", "null"):
            with self.subTest(body=body):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.web.port}/api/command",
                    data=body.encode(), method="POST")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 400)



class TestALiveTerminalDoesNotLieAboutItsSession(unittest.TestCase):
    """``--ssid``/``QX_SSID`` never touch disk.

    A running engine holding an in-memory session used to report
    ``session: "no session yet"`` out of the same payload whose ``message``
    and ``active`` keys said the session was live. An operator reading the
    status label would re-pair a terminal that is already paired.
    """

    def setUp(self):
        from cybertrade.web.pairing import PairingController
        from cybertrade.web.server import EngineHub

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)
        self.hub = EngineHub(None, self.cfg)
        self.live = False
        self.hub.pairing = PairingController(
            self.cfg, lambda ssid, purse: None,
            probe=lambda: self.live)

    def test_the_status_key_agrees_with_the_probe(self):
        self.assertIn("no session yet", self.hub.pairing.status()["session"])
        self.live = True
        st = self.hub.pairing.status()
        self.assertTrue(st["active"])
        self.assertIn("live", st["session"])
        self.assertNotIn("no session yet", st["session"])

    def test_the_raw_file_status_is_still_available(self):
        self.live = True
        st = self.hub.pairing.status()
        self.assertIn("no session yet", st["saved"])
        self.assertNotEqual(st["session"], st["saved"])

    def test_the_message_already_said_it_and_now_so_does_session(self):
        self.live = True
        st = self.hub.pairing.status()
        self.assertIn("re-pair only if it dies", st["message"])
        self.assertNotIn("no session yet", st["session"])

    def test_a_saved_session_still_reports_the_domain(self):
        from cybertrade.brokers.quotex.pairing import save_session

        save_session(self.cfg.qx_session_path,
                     {"ssid": "S." + "x" * 32, "cookies": "c=1"})
        st = self.hub.pairing.status()
        self.assertIn("session saved for", st["session"])


class TestPairedPayloadKeepsItsShape(unittest.TestCase):
    """The unpaired payload reports ``engine_state``; so must the paired one.

    A client reading the top-level key must not see it vanish the instant a
    session arrives, or "pairing" is the last state it ever reports.
    """

    def setUp(self):
        from cybertrade.web.server import EngineHub

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = _cfg(self.tmp.name)
        self.hub = EngineHub(_StubEngine(), cfg)
        self._cfg = cfg

    def test_engine_state_is_present_and_matches_the_snapshot(self):
        st = self.hub.state()
        self.assertIn("engine_state", st)
        self.assertEqual(st["engine_state"],
                         st["snapshot"]["health"]["engine_state"])
        self.assertNotEqual(st["engine_state"], "pairing")

    def test_the_unpaired_payload_still_says_pairing(self):
        from cybertrade.web.server import EngineHub

        hub = EngineHub(None, self._cfg)
        st = hub.state()
        self.assertEqual(st["engine_state"], "pairing")
        self.assertFalse(st["paired"])


if __name__ == "__main__":
    unittest.main()
