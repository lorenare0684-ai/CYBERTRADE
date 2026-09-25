"""CLI robustness: no argument may hang, crash, or leak a traceback.

These are the failures an operator actually meets. On Windows the console
closes the instant the process exits, so a traceback is not merely ugly --
it is the only output anyone ever sees, and it is gone before it can be
read. A command that *hangs* is worse still: it looks exactly like one
that is working.

Everything here runs the real ``main()`` in a subprocess with stdin closed,
which is what a double-clicked ``CYBERTRADE.bat``, a pipe, or CI produces.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest

from cybertrade.cli import _bounded_int, main
from cybertrade.config import ConfigError


class TestBoundedInt(unittest.TestCase):
    """A count is clamped, not trusted."""

    def test_a_sane_value_passes_through(self):
        self.assertEqual(_bounded_int(5, 1, 100, "--runs"), 5)

    def test_a_non_number_is_a_usage_error(self):
        for raw in ("abc", None, "", "1.5.2", [], {}):
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError):
                    _bounded_int(raw, 1, 100, "--runs")

    def test_out_of_band_values_are_rejected(self):
        with self.assertRaises(ConfigError):
            _bounded_int(0, 1, 100, "--runs")
        with self.assertRaises(ConfigError):
            _bounded_int(-5, 1, 100, "--runs")
        with self.assertRaises(ConfigError):
            _bounded_int(101, 1, 100, "--runs")

    def test_a_billion_is_rejected_rather_than_attempted(self):
        """``--runs 999999999`` used to hang forever."""
        with self.assertRaises(ConfigError) as ctx:
            _bounded_int(999999999, 1, 100_000, "--runs")
        self.assertIn("at most", str(ctx.exception))


def _run_cli(argv, timeout=90):
    """Run the real CLI in a subprocess with stdin closed."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path[:1]) or ".")
    env["PYTHONPATH"] = os.getcwd()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "cybertrade"] + list(argv),
        capture_output=True, timeout=timeout, env=env,
        stdin=subprocess.DEVNULL, cwd=os.getcwd(),
    )


class TestNoCommandCrashes(unittest.TestCase):
    """A hostile argv must never produce a traceback."""

    CASES = [
        ["--version"], ["--help"], ["run", "--bogus"],
        ["run", "--demo", "--real"], ["run", "--demo", "--port", "abc"],
        ["web", "--port", "99999"], ["web", "--port", "-1"],
        ["quotex"], ["quotex", "bogus"],
        ["quotex", "login", "--timeout", "abc"], ["quotex", "login", "--port", "abc"],
        ["journal", "--limit", "abc"], ["journal", "--limit", "-5"],
        ["edge", "--payout", "abc"], ["edge", "--winrate", "abc"],
        ["montecarlo"], ["montecarlo", "--pnl", "abc"],
        ["montecarlo", "--pnl", ""], ["montecarlo", "--pnl", ","],
        ["montecarlo", "--pnl", "1,2,3", "--runs", "abc"],
        ["montecarlo", "--pnl", "1,2,3", "--horizon", "abc"],
        ["montecarlo", "--pnl", "1,2,3", "--runs", "0"],
        ["montecarlo", "--pnl", "1,2,3", "--runs", "-5"],
        ["montecarlo", "--pnl", "1,2,3", "--runs", "999999999"],
        ["montecarlo", "--pnl", "1,2,3", "--horizon", "999999999"],
        ["montecarlo", "--wins", "abc"], ["montecarlo", "--wins", "-1"],
        ["montecarlo", "--wins", "999999999"],
        ["strategies", "--bogus"], ["calendar", "--bogus"], ["doctor", "--bogus"],
        ["gui", "--demo", "--real"],
        ["--config", "/nonexistent/dir/x.json", "doctor"],
    ]

    def test_nothing_leaks_a_traceback_or_hangs(self):
        leaks = []
        for argv in self.CASES:
            with self.subTest(argv=argv):
                try:
                    proc = _run_cli(argv, timeout=90)
                except subprocess.TimeoutExpired:
                    leaks.append((argv, "HANG"))
                    continue
                out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
                if "Traceback (most recent call last)" in out:
                    leaks.append((argv, "traceback"))
                if proc.returncode not in (0, 1, 2, 78):
                    leaks.append((argv, f"rc={proc.returncode}"))
        self.assertEqual(leaks, [], f"bad argv: {leaks}")

    def test_a_bogus_config_path_is_reported_cleanly(self):
        proc = _run_cli(["--config", "/nonexistent/dir/x.json", "doctor"])
        self.assertNotIn("Traceback", (proc.stdout + proc.stderr).decode())


class TestMontecarloAcceptsARealSample(unittest.TestCase):
    """The clamp must not break the happy path."""

    def test_a_valid_sample_still_works(self):
        proc = _run_cli(["montecarlo", "--pnl", "8.5,-10,8.5,-10",
                         "--runs", "200", "--horizon", "20"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:300])
        self.assertIn(b"verdict", proc.stdout)

    def test_an_oversized_but_legal_run_works(self):
        proc = _run_cli(["montecarlo", "--pnl", "8.5,-10", "--runs", "100000",
                         "--horizon", "10"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:300])


class TestThePortIsCheckedBeforeItIsBound(unittest.TestCase):
    """``web --port 99999`` used to reach bind() and raise OverflowError.

    On Windows that is a traceback flash behind a console that closes before
    it can be read. A typo is a usage error: exit 2 and a plain message.
    """

    def test_out_of_range_ports_are_a_clean_usage_error(self):
        for port in (99999, -1, 70000, 65536, 100000):
            with self.subTest(port=port):
                proc = _run_cli(["web", "--demo", "--yes", "--port", str(port)],
                                timeout=40)
                out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
                self.assertNotIn("Traceback", out)
                self.assertNotIn("OverflowError", out)
                self.assertEqual(proc.returncode, 2, out[-200:])
                self.assertIn("--port must be 0-65535", out)

    def test_a_non_numeric_port_is_rejected_by_argparse(self):
        proc = _run_cli(["web", "--demo", "--yes", "--port", "abc"], timeout=40)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", (proc.stdout + proc.stderr).decode())

    def test_a_busy_port_is_explained_not_thrown(self):
        """A second terminal on the same port is the common case."""
        import socket

        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        try:
            proc = _run_cli(["web", "--demo", "--yes", "--port", str(busy)],
                            timeout=60)
        finally:
            s.close()
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        self.assertNotIn("Traceback", out)
        self.assertIn("cannot listen", out)
        self.assertIn("--port", out)          # tells the operator what to do
        self.assertEqual(proc.returncode, 1)

    def test_port_zero_reports_the_port_the_os_picked(self):
        """:0 is not an address; the bound one is.

        The terminal serves forever, so this has to start it, let it bind,
        kill it, and read what it printed. ``-u`` matters: block-buffered
        stdout is lost when the process is terminated.
        """
        import signal

        env = dict(os.environ, PYTHONPATH=os.getcwd(),
                   PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "cybertrade",
             "web", "--demo", "--yes", "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, cwd=os.getcwd())
        try:
            chunks = []
            deadline = time.time() + 25
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                chunks.append(line)
                if b"web terminal : http://" in line:
                    break
            proc.send_signal(signal.SIGINT)
            try:
                rest, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                rest, _ = proc.communicate(timeout=10)
            out = (b"".join(chunks) + (rest or b"")).decode("utf-8", "replace")
            self.assertNotIn("Traceback", out)
            self.assertIn("web terminal", out)
            self.assertNotIn(":0\n", out)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()



class TestAProtectedInstallDirectory(unittest.TestCase):
    """Windows users extract into C:\\Program Files often enough to matter.

    A raw ``PermissionError`` traceback names the directory and nothing else,
    behind a console that closes before it can be read. Six of twelve
    commands used to die that way.
    """

    def _protected_tree(self):
        """A read-only copy of the package, like a protected install."""
        import shutil
        import stat

        base = tempfile.mkdtemp(prefix="ctro ")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        work = os.path.join(base, "CYBERTRADE")
        os.makedirs(work)
        shutil.copytree("cybertrade", os.path.join(work, "cybertrade"))
        os.chmod(work, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, work, 0o755)
        return work

    def _run_in(self, work, argv, timeout=60):
        env = dict(os.environ, PYTHONPATH=work, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "cybertrade"] + list(argv),
            capture_output=True, timeout=timeout, env=env,
            stdin=subprocess.DEVNULL, cwd=work)

    def test_no_command_leaks_a_permission_error(self):
        work = self._protected_tree()
        leaks = []
        for argv in (["doctor"], ["journal"], ["quotex", "status"],
                     ["calibrate"], ["run", "--demo", "--yes"],
                     ["web", "--demo", "--yes", "--port", "0"],
                     ["gui", "--demo", "--yes"], ["strategies"]):
            with self.subTest(argv=argv):
                try:
                    proc = self._run_in(work, argv, timeout=60)
                except subprocess.TimeoutExpired:
                    continue          # a server that started is a pass
                out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
                if "Traceback (most recent call last)" in out:
                    leaks.append((argv, out.strip().splitlines()[-1][:80]))
                if "PermissionError" in out:
                    leaks.append((argv, "PermissionError leaked"))
        self.assertEqual(leaks, [])

    def test_the_message_says_what_to_do_about_it(self):
        work = self._protected_tree()
        proc = self._run_in(work, ["journal"])
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        self.assertNotIn("Traceback", out)
        self.assertIn("writable", out)
        # actionable, not just diagnostic
        self.assertTrue("--config" in out or "Install" in out, out)

    def test_the_preflight_finds_the_real_directory(self):
        """The check must report the directory it actually failed on."""
        from cybertrade.cli import _require_writable_data_dir
        from cybertrade.config import AppConfig

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.journal_path = os.path.join(tmp, "sub", "journal.db")
            # a writable tree: must pass silently
            _require_writable_data_dir(cfg)

            # now a file where the directory needs to be
            blocker = os.path.join(tmp, "blocker")
            open(blocker, "w").close()
            cfg.journal_path = os.path.join(blocker, "journal.db")
            with self.assertRaises(ConfigError) as ctx:
                _require_writable_data_dir(cfg)
            self.assertIn("blocker", str(ctx.exception))

    def test_a_writable_data_dir_is_not_reported(self):
        from cybertrade.cli import _require_writable_data_dir
        from cybertrade.config import AppConfig

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            for attr in ("log_path", "journal_path", "qx_session_path",
                         "calibration_path", "operator_path",
                         "continuity_path", "heartbeat_path"):
                setattr(cfg, attr, os.path.join(tmp, attr))
            _require_writable_data_dir(cfg)      # must not raise

    def test_a_missing_log_file_does_not_stop_the_program(self):
        """Losing the log is never a reason to lose the run."""
        from cybertrade.logging_setup import setup_logging

        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "blocker")
            open(blocker, "w").close()
            # the log path cannot be created; setup must still return a logger
            root = setup_logging(level="INFO",
                                 log_file=os.path.join(blocker, "x.log"))
            self.assertIsNotNone(root)



if __name__ == "__main__":
    unittest.main()


class TestDoctorReadsTheRealConfig(unittest.TestCase):
    """doctor is the preflight run before going live with real money, so it has
    to read the operator's config rather than a fresh default -- a passing
    checklist that hides a disabled playbook is the same lie as a config knob
    that does nothing."""

    def _doctor(self, cfg_text=None):
        argv = []
        if cfg_text is not None:
            path = os.path.join(tempfile.mkdtemp(prefix="ctdoc_"), "c.json")
            io.open(path, "w", encoding="utf-8").write(cfg_text)
            argv += ["--config", path]
        argv.append("doctor")
        p = _run_cli(argv, timeout=120)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")

    def test_a_default_config_passes_every_check(self):
        rc, out = self._doctor()
        self.assertIn("risk switches", out)
        self.assertIn("protective defaults", out)
        self.assertEqual(rc, 0, out[-400:])

    def test_a_disabled_playbook_fails_the_preflight(self):
        rc, out = self._doctor('{"survivor": {"enabled": false}}')
        self.assertIn("survivor playbook DISABLED", out)
        self.assertEqual(rc, 1)

    def test_every_weakened_switch_is_named(self):
        rc, out = self._doctor(
            '{"survivor": {"weekend_lock": false, "trend_filter": false},'
            ' "strategy": {"trade_on_weak": true, "max_signals_per_candle": 3},'
            ' "risk": {"edge_gate": "off"}}')
        for needle in ("weekend lock off", "trend filter off",
                       "trade_on_weak ON", "vote cap 3/candle", "edge gate OFF"):
            self.assertIn(needle, out)
        self.assertEqual(rc, 1)

    def test_a_broken_config_fails_rather_than_crashing(self):
        rc, out = self._doctor('{"strategy": {"max_signals_per_candle": "abc"}}')
        self.assertNotIn("Traceback", out)
        self.assertEqual(rc, 1)

    def test_the_check_count_is_reported(self):
        rc, out = self._doctor()
        self.assertIn("14/14 checks passed", out)


class TestSessionVerify(unittest.TestCase):
    """_verify_session_data: auth alone is not a live session."""

    def _api(self, data: bool):
        from types import SimpleNamespace

        pokes = []

        def wait_for_data(timeout=0):
            return data

        return SimpleNamespace(
            subscribe=lambda a, tf: pokes.append(("sub", a, tf)),
            request_instruments=lambda: pokes.append(("inst",)),
            wait_for_data=wait_for_data,
            pokes=pokes,
        )

    def test_flowing_data_passes(self):
        from cybertrade.cli import _verify_session_data

        api = self._api(True)
        self.assertIsNone(_verify_session_data(api, timeout=0.01))
        self.assertTrue(api.pokes)  # the wire was poked first

    def test_silence_raises_pairing_screen_case(self):
        from cybertrade.cli import _verify_session_data
        from cybertrade.exceptions import BrokerConnectionError

        with self.assertRaises(BrokerConnectionError) as ctx:
            _verify_session_data(self._api(False), timeout=0.01)
        self.assertIn("no market data", str(ctx.exception))

    def test_stub_apis_without_wait_are_skipped(self):
        from types import SimpleNamespace

        from cybertrade.cli import _verify_session_data

        stub = SimpleNamespace(subscribe=lambda a, t: None,
                               request_instruments=lambda: None)
        self.assertIsNone(_verify_session_data(stub, timeout=0.01))


class TestArmOrHold(unittest.TestCase):
    """_arm_or_hold: a latched kill holds, never kills the terminal."""

    def test_success_arms(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace

        from cybertrade.cli import _arm_or_hold

        engine = SimpleNamespace(armed=[], arm=lambda: engine.armed.append(1))
        with redirect_stdout(io.StringIO()):
            self.assertTrue(_arm_or_hold(engine))
        self.assertEqual(engine.armed, [1])

    def test_kill_switch_holds_with_a_message(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace

        from cybertrade.cli import _arm_or_hold
        from cybertrade.exceptions import KillSwitchEngaged

        def arm():
            raise KillSwitchEngaged("recovery hold: nope")

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertFalse(_arm_or_hold(SimpleNamespace(arm=arm)))
        self.assertIn("DISARMED", buf.getvalue())


class TestNeedsRebuild(unittest.TestCase):
    """_needs_rebuild: dead-on-arrival rebuilds, live books re-seat."""

    def _engine(self, **kw):
        from types import SimpleNamespace

        from cybertrade.constants import EngineState

        base = dict(state=EngineState.DISARMED, degraded="",
                    broker=SimpleNamespace(open_positions=lambda: []),
                    continuity=SimpleNamespace(fault=""))
        base.update(kw)
        return SimpleNamespace(**base)

    def test_degraded_disarmed_rebuilds(self):
        from cybertrade.cli import _needs_rebuild

        self.assertTrue(_needs_rebuild(self._engine(degraded="0 candles")))

    def test_degraded_live_stays_in_place(self):
        from cybertrade.cli import _needs_rebuild
        from cybertrade.constants import EngineState

        engine = self._engine(degraded="0 candles", state=EngineState.LIVE)
        self.assertFalse(_needs_rebuild(engine))

    def test_bootstrap_hold_rebuilds(self):
        from cybertrade.cli import BOOTSTRAP_HOLD, _needs_rebuild
        from types import SimpleNamespace

        engine = self._engine(continuity=SimpleNamespace(fault=BOOTSTRAP_HOLD))
        self.assertTrue(_needs_rebuild(engine))

    def test_healthy_engine_reseats(self):
        from cybertrade.cli import _needs_rebuild

        self.assertFalse(_needs_rebuild(self._engine()))

    def test_open_positions_never_rebuild(self):
        from cybertrade.cli import _needs_rebuild
        from types import SimpleNamespace

        broker = SimpleNamespace(open_positions=lambda: ["x1"])
        engine = self._engine(degraded="0 candles", broker=broker)
        self.assertFalse(_needs_rebuild(engine))

    def test_unreadable_book_never_rebuilds(self):
        from cybertrade.cli import _needs_rebuild
        from types import SimpleNamespace

        def boom():
            raise RuntimeError("book locked")

        broker = SimpleNamespace(open_positions=boom)
        engine = self._engine(degraded="0 candles", broker=broker)
        self.assertFalse(_needs_rebuild(engine))


class TestLiveApiBoot(unittest.TestCase):
    """_live_api: ensure the purse, and never strand a socket."""

    def _cfg(self):
        from types import SimpleNamespace

        return SimpleNamespace(broker=SimpleNamespace(ssid="S"),
                               qx_session_path="")

    def _stub(self, data=True):
        from types import SimpleNamespace

        calls = []
        return SimpleNamespace(
            set_ssid=lambda s, c="": calls.append("ssid"),
            connect=lambda: calls.append("connect") or True,
            subscribe=lambda a, t: None,
            request_instruments=lambda: None,
            wait_for_data=lambda timeout=0: data,
            ensure_purse=lambda: calls.append("ensure") or True,
            close=lambda: calls.append("close"),
            calls=calls,
        )

    def test_boot_verifies_then_ensures(self):
        from cybertrade.cli import _live_api

        stub = self._stub(data=True)
        self.assertIs(_live_api(self._cfg(), api_factory=lambda: stub), stub)
        self.assertEqual(stub.calls, ["ssid", "connect", "ensure"])

    def test_failed_boot_closes_the_api(self):
        from cybertrade.cli import _live_api
        from cybertrade.exceptions import BrokerConnectionError

        stub = self._stub(data=False)
        with self.assertRaises(BrokerConnectionError):
            _live_api(self._cfg(), api_factory=lambda: stub)
        self.assertIn("close", stub.calls)
        self.assertNotIn("ensure", stub.calls)


class TestResolveSession(unittest.TestCase):
    """_resolve_session: cfg → env → paired file, cookies paired correctly."""

    def _cfg(self, ssid="", cookies="", path=""):
        from types import SimpleNamespace

        return SimpleNamespace(
            broker=SimpleNamespace(ssid=ssid, cookies=cookies),
            qx_session_path=path)

    def test_cfg_wins_with_its_cookies(self):
        from cybertrade.cli import _resolve_session

        ssid, cookies = _resolve_session(self._cfg("S", "a=b"))
        self.assertEqual((ssid, cookies), ("S", "a=b"))

    def test_env_fallback_is_cookieless(self):
        import os
        from unittest import mock

        from cybertrade.cli import _resolve_session

        with mock.patch.dict(os.environ, {"QX_SSID": "E"}):
            ssid, cookies = _resolve_session(self._cfg())
        self.assertEqual((ssid, cookies), ("E", ""))

    def test_file_fallback(self):
        import json
        import tempfile

        from cybertrade.cli import _resolve_session

        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump({"ssid": "F", "cookies": "x=y"}, fh)
            path = fh.name
        try:
            ssid, cookies = _resolve_session(self._cfg(path=path))
        finally:
            import os

            os.unlink(path)
        self.assertEqual((ssid, cookies), ("F", "x=y"))

    def test_nothing_resolves_empty(self):
        import os
        from unittest import mock

        from cybertrade.cli import _resolve_session

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QX_SSID", None)
            self.assertEqual(_resolve_session(self._cfg()), ("", ""))


class TestCookieHandoff(unittest.TestCase):
    """Paired cookies must reach the handshake, never the disk."""

    def test_config_blanks_cookies(self):
        from cybertrade.config import AppConfig

        cfg = AppConfig()
        cfg.broker.cookies = "a=b"
        self.assertEqual(cfg.to_dict()["broker"]["cookies"], "")

    def test_controller_passes_cookies(self):
        from types import SimpleNamespace

        from cybertrade.web.pairing import PairingController

        got = []
        ctl = PairingController(
            SimpleNamespace(qx_session_path=""),
            on_ready=lambda s, p, c: got.append((s, p, c)))
        ctl._done({"ssid": "S", "cookies": "a=b"}, ctl._epoch)
        self.assertEqual(got, [("S", ctl._purse, "a=b")])

    def test_reseat_carries_cookies(self):
        from types import SimpleNamespace
        from unittest import mock

        from cybertrade.brokers.quotex.api import QuotexAPI
        from cybertrade.brokers.quotex.ghost import Pacekeeper
        from cybertrade.cli import _reseat_session

        api = QuotexAPI(pace=Pacekeeper(enabled=False))
        engine = SimpleNamespace(
            feed=SimpleNamespace(api=api),
            health=SimpleNamespace(note_message=lambda m: None))
        with mock.patch.object(QuotexAPI, "connect", return_value=True), \
                mock.patch("cybertrade.cli._verify_session_data",
                           return_value=None):
            _reseat_session(engine, "SID", "a=b")
        self.assertEqual(api.session.ssid, "SID")
        self.assertEqual(api.session.cookies, "a=b")

    def test_live_api_forwards_cfg_cookies(self):
        from types import SimpleNamespace

        from cybertrade.cli import _live_api

        seen = {}

        def factory():
            return SimpleNamespace(
                set_ssid=lambda s, c="": seen.update(ssid=s, cookies=c),
                connect=lambda: True,
                subscribe=lambda a, t: None,
                request_instruments=lambda: None,
                wait_for_data=lambda timeout=0: True,
                ensure_purse=lambda: True,
                close=lambda: None)

        cfg = SimpleNamespace(
            broker=SimpleNamespace(ssid="S", cookies="a=b"),
            qx_session_path="")
        _live_api(cfg, api_factory=factory)
        self.assertEqual(seen, {"ssid": "S", "cookies": "a=b"})
