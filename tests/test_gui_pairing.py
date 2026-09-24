"""Chrome-assisted Quotex pairing for the interactive terminals.

The desktop terminal's LINK pane gets a ``CHROME LOGIN`` button alongside
``CONNECT``: Chrome opens qxbroker.com in a persistent profile, the operator
signs in and solves the CAPTCHA, and the resulting ``sessionid`` cookie is
adopted into the live wire. These tests cover the orchestration without Tk
(the toolkit is unavailable in CI and the logic is deliberately toolkit-free).
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest.mock as mock
import threading
import time
import unittest
from typing import Any, Dict, List

from cybertrade.config import AppConfig
from cybertrade.exceptions import ConfigError
from cybertrade.gui import pairing
from cybertrade.gui.pairing import pairing_form
from tests.tk_shim import fake_tk

try:
    import tkinter  # noqa: F401
    HAVE_TK = True
except ModuleNotFoundError:        # CI / headless sandbox
    HAVE_TK = False


class FakePair:
    """Stands in for ``pair_session`` — no Chrome, no network."""

    def __init__(self, sess=None, exc=None, delay=0.0):
        if sess is None:
            sess = {"ssid": "QX.paired", "cookies": "sessionid=QX.paired"}
        self.sess = sess
        self.exc = exc
        self.delay = delay
        self.calls = []
        self.event = threading.Event()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            self.event.wait(self.delay + 1.0)
        if self.exc is not None:
            raise self.exc
        return self.sess


class TestPairingForm(unittest.TestCase):
    def test_defaults_fill_a_blank_form(self):
        ok, error, values = pairing_form()
        self.assertTrue(ok, error)
        self.assertEqual(values["profile"], pairing.DEFAULT_PROFILE)
        self.assertEqual(values["port"], pairing.DEFAULT_CDP_PORT)
        self.assertEqual(values["timeout"], pairing.DEFAULT_TIMEOUT)

    def test_operator_values_survive(self):
        ok, error, values = pairing_form(profile="  /tmp/p  ", port="9444",
                                        timeout="30")
        self.assertTrue(ok, error)
        self.assertEqual(values["profile"], "/tmp/p")
        self.assertEqual(values["port"], 9444)
        self.assertEqual(values["timeout"], 30.0)

    def test_blank_fields_fall_back_to_defaults(self):
        ok, _, values = pairing_form(profile="", port="", timeout="")
        self.assertTrue(ok)
        self.assertEqual(values["port"], pairing.DEFAULT_CDP_PORT)

    def test_bad_port_is_reported_not_raised(self):
        for bad in ("abc", "0", "-1", "70000", "9.5"):
            ok, error, _ = pairing_form(port=bad)
            self.assertFalse(ok, bad)
            self.assertIn("port", error)

    def test_bad_timeout_is_reported_not_raised(self):
        for bad in ("soon", "0", "-5"):
            ok, error, _ = pairing_form(timeout=bad)
            self.assertFalse(ok, bad)
            self.assertIn("wait seconds", error)


class TestRunPairing(unittest.TestCase):
    def test_success_reaches_the_done_callback(self):
        fake = FakePair()
        done, errors = [], []
        thread = pairing.run_pairing(
            session_path="/tmp/s.json", profile="p", port=9333, timeout=10,
            on_done=lambda s: done.append(s), on_error=errors.append, pair=fake)
        thread.join(5)
        self.assertEqual(done, [fake.sess])
        self.assertEqual(errors, [])
        self.assertEqual(fake.calls[0]["profile_dir"], "p")
        self.assertEqual(fake.calls[0]["port"], 9333)
        self.assertEqual(fake.calls[0]["session_path"], "/tmp/s.json")

    def test_failure_reaches_the_error_callback(self):
        boom = TimeoutError("no session after 240s")
        fake = FakePair(exc=boom)
        done, errors = [], []
        thread = pairing.run_pairing(
            session_path="s", profile="p", port=1, timeout=1,
            on_done=done.append, on_error=errors.append, pair=fake)
        thread.join(5)
        self.assertEqual(done, [])
        self.assertEqual(errors, [boom])

    def test_a_missing_chrome_binary_is_reported(self):
        fake = FakePair(exc=FileNotFoundError("Chrome/Chromium not found"))
        errors = []
        thread = pairing.run_pairing(
            session_path="s", profile="p", port=1, timeout=1,
            on_done=lambda s: None, on_error=errors.append, pair=fake)
        thread.join(5)
        self.assertIn("Chrome", str(errors[0]))

    def test_the_worker_is_a_daemon_so_a_closed_window_never_waits(self):
        fake = FakePair(delay=5.0)
        thread = pairing.run_pairing(
            session_path="s", profile="p", port=1, timeout=1,
            on_done=lambda s: None, on_error=lambda e: None, pair=fake)
        self.assertTrue(thread.daemon)
        self.assertEqual(thread.name, "quotex-pairing")
        fake.event.set()          # let it finish so the test does not leak it
        thread.join(5)

    def test_callbacks_fire_off_the_calling_thread(self):
        fake = FakePair()
        seen = []
        thread = pairing.run_pairing(
            session_path="s", profile="p", port=1, timeout=1,
            on_done=lambda s: seen.append(threading.current_thread().name),
            on_error=lambda e: None, pair=fake)
        thread.join(5)
        self.assertEqual(seen, ["quotex-pairing"])

    def test_an_empty_session_is_not_a_crash(self):
        fake = FakePair(sess={})
        done = []
        thread = pairing.run_pairing(
            session_path="s", profile="p", port=1, timeout=1,
            on_done=done.append, on_error=lambda e: None, pair=fake)
        thread.join(5)
        self.assertEqual(done, [{}])


class TestSessionStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "qx.json")

    def test_missing_session_is_described_not_raised(self):
        self.assertEqual(pairing.session_status(self.path), "no session yet")

    def test_saved_session_is_described(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"ssid": "QX.s", "cookies": "sessionid=QX.s",
                       "domain": "qxbroker.com"}, fh)
        self.assertEqual(pairing.session_status(self.path),
                         "session saved for qxbroker.com")

    def test_corrupt_session_is_described_not_raised(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(pairing.session_status(self.path), "no session yet")

    def test_profile_presence(self):
        profile = os.path.join(self.tmp.name, "profile")
        self.assertFalse(pairing.profile_exists(profile))
        os.makedirs(profile)
        self.assertFalse(pairing.profile_exists(profile))    # empty dir
        with open(os.path.join(profile, "Preferences"), "w") as fh:
            fh.write("{}")
        self.assertTrue(pairing.profile_exists(profile))


@unittest.skipUnless(HAVE_TK, "tkinter unavailable in this sandbox")
class TestPanelWithTk(unittest.TestCase):
    """Panel behaviour that needs the real toolkit."""

    def _panel(self):
        from cybertrade.gui.panels import ConnectionPanel
        from cybertrade.gui.theme import Theme

        self.seen: List[Dict[str, Any]] = []
        return ConnectionPanel(None, Theme("neon_abyss"),
                               connect_cb=self.seen.append,
                               login_cb=self.seen.append)

    def test_both_buttons_exist(self):
        panel = self._panel()
        self.assertIn("CONNECT", panel.connect_btn.label)
        self.assertIn("CHROME LOGIN", panel.login_btn.label)

    def test_pairing_defaults_are_the_cli_defaults(self):
        panel = self._panel()
        self.assertEqual(panel.profile_var.get(), pairing.DEFAULT_PROFILE)
        self.assertEqual(panel.port_var.get(), str(pairing.DEFAULT_CDP_PORT))
        self.assertEqual(panel.timeout_var.get(),
                         str(int(pairing.DEFAULT_TIMEOUT)))

    def test_no_purse_blocks_every_action(self):
        panel = self._panel()
        panel._login()
        panel._connect()
        self.assertEqual(self.seen, [])
        self.assertIn("pick a purse", panel.status._label)

    def test_login_sends_the_purse_and_the_form(self):
        panel = self._panel()
        panel.purse.set("real")
        panel.port_var.set("9444")
        panel.timeout_var.set("30")
        panel._login()
        self.assertEqual(self.seen[-1], {
            "mode": "quotex", "demo": False,
            "profile": "data/chrome-profile", "port": 9444, "timeout": 30.0,
        })
        self.assertIn("launching Chrome", panel.status._label)

    def test_bad_form_is_reported_before_chrome_launches(self):
        panel = self._panel()
        panel.purse.set("practice")
        panel.port_var.set("not-a-port")
        panel._login()
        self.assertEqual(self.seen, [])
        self.assertIn("port", panel.status._label)

    def test_a_running_pairing_blocks_a_second_one(self):
        panel = self._panel()
        panel.purse.set("practice")
        panel._login()
        panel._login()
        self.assertEqual(len(self.seen), 1)
        self.assertIn("already running", panel.status._label)

    def test_connect_is_blocked_while_pairing(self):
        panel = self._panel()
        panel.purse.set("practice")
        panel._login()
        panel._connect()
        self.assertEqual(len(self.seen), 1)
        self.assertIn("already running", panel.status._label)

    def test_busy_toggles_the_button(self):
        panel = self._panel()
        panel.set_busy(True, "working…")
        self.assertIn("PAIRING", panel.login_btn.label)
        panel.set_busy(False)
        self.assertIn("CHROME LOGIN", panel.login_btn.label)

    def test_adopt_session_fills_the_field_and_clears_busy(self):
        panel = self._panel()
        panel.set_busy(True)
        panel.adopt_session("QX.paired", "session captured — connecting…")
        self.assertEqual(panel.ssid_var.get(), "QX.paired")
        self.assertFalse(panel._pairing)
        self.assertIn("CHROME LOGIN", panel.login_btn.label)
        self.assertIn("session captured", panel.status._label)


class TestPanelHeadless(unittest.TestCase):
    """Same panel, driven through the tkinter shim — runs everywhere."""

    def setUp(self) -> None:
        self._tk = fake_tk()
        self._tk.__enter__()

    def tearDown(self) -> None:
        self._tk.__exit__(None, None, None)

    def _panel(self):
        from cybertrade.gui.panels import ConnectionPanel
        from cybertrade.gui.theme import Theme

        self.seen: List[Dict[str, Any]] = []
        return ConnectionPanel(None, Theme("neon_abyss"),
                               connect_cb=self.seen.append,
                               login_cb=self.seen.append)

    def test_panel_offers_the_login_button(self):
        panel = self._panel()
        self.assertIn("CHROME LOGIN", panel.login_btn.label)
        self.assertIn("CONNECT", panel.connect_btn.label)

    def test_hidden_without_a_login_callback(self):
        from cybertrade.gui.panels import ConnectionPanel
        from cybertrade.gui.theme import Theme

        panel = ConnectionPanel(None, Theme("neon_abyss"),
                                connect_cb=lambda body: None)
        forgotten = [c for c in panel.login_btn.calls if c[0] == "pack_forget"]
        self.assertTrue(forgotten)
        self.assertIsNone(panel.login_cb)
        panel.purse.set("practice")
        panel._login()
        self.assertIn("unavailable", panel.status._label)

    def test_login_payload_reaches_the_app(self):
        panel = self._panel()
        panel.purse.set("practice")
        panel.port_var.set("9333")
        panel._login()
        self.assertEqual(self.seen, [{
            "mode": "quotex", "demo": True,
            "profile": "data/chrome-profile", "port": 9333, "timeout": 240.0,
        }])

    def test_real_purse_is_passed_as_false(self):
        panel = self._panel()
        panel.purse.set("real")
        panel._login()
        self.assertIs(self.seen[-1]["demo"], False)

    def test_practice_purse_is_passed_as_true(self):
        panel = self._panel()
        panel.purse.set("practice")
        panel._login()
        self.assertIs(self.seen[-1]["demo"], True)

    def test_blank_profile_falls_back_to_the_default(self):
        panel = self._panel()
        panel.purse.set("practice")
        panel.profile_var.set("   ")
        panel._login()
        self.assertEqual(self.seen[-1]["profile"], pairing.DEFAULT_PROFILE)

    def test_busy_and_adopt_round_trip(self):
        panel = self._panel()
        panel.set_busy(True, "launching Chrome…")
        self.assertIn("PAIRING", panel.login_btn.label)
        self.assertTrue(panel._pairing)
        panel.adopt_session("QX.x")
        self.assertEqual(panel.ssid_var.get(), "QX.x")
        self.assertFalse(panel._pairing)
        self.assertIn("CHROME LOGIN", panel.login_btn.label)

    def test_connect_still_works_without_the_login_button(self):
        panel = self._panel()
        panel.purse.set("practice")
        panel.ssid_var.set("QX.manual")
        panel._connect()
        self.assertEqual(self.seen[-1], {
            "mode": "quotex", "ssid": "QX.manual", "demo": True})


class _FakeEngine:
    """Just enough engine for the app's login path."""

    def __init__(self) -> None:
        self.fired: List[Any] = []
        self.alerts = self

    def fire(self, alert: Any) -> None:
        self.fired.append(alert)


class TestAppLoginFlow(unittest.TestCase):
    """The app half of the pairing, driven through the shim."""

    def setUp(self) -> None:
        self._tk = fake_tk()
        self._tk.__enter__()
        import cybertrade.gui.app as app_mod
        from cybertrade.gui.panels import ConnectionPanel
        from cybertrade.gui.theme import Theme
        from cybertrade.config import AppConfig

        self.app_mod = app_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = AppConfig()
        cfg.qx_session_path = os.path.join(self.tmp.name, "qx.json")
        self.engine = _FakeEngine()
        self.wired: List[Dict[str, Any]] = []
        panel = ConnectionPanel(
            None, Theme("neon_abyss"),
            connect_cb=self.wired.append,
            login_cb=lambda body: self._started.append(body))
        self._started: List[Dict[str, Any]] = []
        panel.login_cb = lambda body: self._started.append(body)
        app = app_mod.CybertradeApp.__new__(app_mod.CybertradeApp)
        app.calls = []                      # the shim's widget bookkeeping
        app.engine = self.engine
        app.config = cfg
        app.link = panel
        # the venue wire itself is covered by the broker tests; here we only
        # care that the captured cookie is handed to it
        app._connect = self.wired.append
        self.app = app
        self.panel = panel
        self._real_run_pairing = app_mod.run_pairing

    def tearDown(self) -> None:
        self.app_mod.run_pairing = self._real_run_pairing
        self._tk.__exit__(None, None, None)

    def _pair_ok(self, sess):
        self.app_mod.run_pairing = lambda **kw: kw["on_done"](sess)

    def _pair_fails(self, exc):
        self.app_mod.run_pairing = lambda **kw: kw["on_error"](exc)

    def test_success_adopts_the_cookie_and_wires_the_session(self):
        self._pair_ok({"ssid": "QX.captured", "cookies": "sessionid=QX.captured"})
        self.app._login({"demo": True, "profile": "p", "port": 9333,
                         "timeout": 5})
        self.assertEqual(self.panel.ssid_var.get(), "QX.captured")
        self.assertFalse(self.panel._pairing)
        self.assertIn("session captured", self.panel.status._label)
        # the captured cookie is wired straight into the live session
        self.assertEqual(self.wired, [{"mode": "quotex", "ssid": "QX.captured",
                                       "demo": True}])

    def test_real_purse_survives_the_pairing(self):
        self._pair_ok({"ssid": "QX.captured"})
        self.app._login({"demo": False, "profile": "p", "port": 9333,
                         "timeout": 5})
        self.assertIs(self.wired[-1]["demo"], False)

    def test_failure_clears_busy_reports_and_alerts(self):
        self._pair_fails(TimeoutError("no session after 5s"))
        self.app._login({"demo": True, "profile": "p", "port": 9333,
                         "timeout": 5})
        self.assertFalse(self.panel._pairing)
        self.assertIn("pairing failed", self.panel.status._label)
        self.assertIn("no session after 5s", self.panel.status._label)
        self.assertEqual([a.kind for a in self.engine.fired], ["session"])
        self.assertEqual(self.engine.fired[0].severity, "warn")

    def test_empty_cookie_is_reported_not_crashed(self):
        self._pair_ok({})
        self.app._login({"demo": True, "profile": "p", "port": 9333,
                         "timeout": 5})
        self.assertFalse(self.panel._pairing)
        self.assertIn("without a sessionid cookie", self.panel.status._label)
        self.assertEqual(self.wired, [])      # never wire an empty session

    def test_missing_chrome_is_reported(self):
        self._pair_fails(FileNotFoundError("Chrome/Chromium not found"))
        self.app._login({"demo": True, "profile": "p", "port": 9333,
                         "timeout": 5})
        self.assertIn("Chrome", self.panel.status._label)

    def test_bad_form_never_reaches_the_worker(self):
        self.app._login({"demo": True, "profile": "p", "port": "abc",
                         "timeout": 5})
        self.assertFalse(self.panel._pairing)
        self.assertIn("port", self.panel.status._label)

    def test_missing_purse_never_reaches_the_worker(self):
        self.app._login({"profile": "p", "port": 9333, "timeout": 5})
        self.assertFalse(self.panel._pairing)
        self.assertIn("pick a purse", self.panel.status._label)

    def test_the_session_path_comes_from_config(self):
        seen = {}
        self.app_mod.run_pairing = lambda **kw: seen.update(kw) or kw["on_done"]({"ssid": "QX.x"})
        self.app._login({"demo": True, "profile": "p", "port": 9333,
                         "timeout": 5})
        self.assertEqual(seen["session_path"], self.app.config.qx_session_path)
        self.assertEqual(seen["profile"], "p")
        self.assertEqual(seen["port"], 9333)


class TestAppWiring(unittest.TestCase):
    """The app must hand the form to the toolkit-free pairing module."""

    def _source(self) -> str:
        return open("cybertrade/gui/app.py", encoding="utf-8").read()

    def test_panel_gets_a_login_callback(self):
        self.assertIn("login_cb=self._login", self._source())

    def test_login_delegates_to_the_pairing_module(self):
        source = self._source()
        self.assertIn("from .pairing import pairing_form, run_pairing", source)
        self.assertIn("run_pairing(", source)

    def test_worker_results_are_marshalled_onto_the_ui_thread(self):
        source = self._source()
        self.assertIn("self.after(", source)
        self.assertIn("_login_done(sess, purse)", source)
        self.assertIn("_login_failed(exc)", source)

    def test_done_adopts_the_cookie_and_connects(self):
        source = self._source()
        self.assertIn("self.link.adopt_session(ssid", source)
        self.assertIn('self._connect({"mode": "quotex", "ssid": ssid', source)

    def test_failure_clears_busy_and_alerts(self):
        source = self._source()
        self.assertIn("self.link.set_busy(False)", source)
        self.assertIn('Alert(kind="session"', source)


class TestSessionGate(unittest.TestCase):
    """The pre-flight window a first-time operator actually lands on."""

    def setUp(self) -> None:
        self._tk = fake_tk()
        self._tk.__enter__()
        import cybertrade.gui.session_gate as gate_mod
        from cybertrade.config import AppConfig

        self.gate_mod = gate_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = AppConfig()
        self.cfg.qx_session_path = os.path.join(self.tmp.name, "qx.json")
        self.ready: List[Any] = []
        self.cancelled: List[Any] = []
        self.gate = gate_mod.SessionGate(
            self.cfg,
            on_ready=lambda ssid, purse: self.ready.append((ssid, purse)),
            on_cancel=lambda: self.cancelled.append(True))
        self._real = gate_mod.run_pairing

    def tearDown(self) -> None:
        self.gate_mod.run_pairing = self._real
        self._tk.__exit__(None, None, None)

    def _pair_ok(self, sess):
        self.gate_mod.run_pairing = lambda **kw: kw["on_done"](sess)

    def _pair_fails(self, exc):
        self.gate_mod.run_pairing = lambda **kw: kw["on_error"](exc)

    def test_window_opens_without_a_session(self):
        self.assertIn("no venue session yet", self.gate.status._label)
        self.assertIn("CHROME LOGIN", self.gate.login_btn.label)
        self.assertEqual(self.gate.purse.get(), "")

    def test_a_cli_purse_carries_into_the_window(self):
        """--demo/--real is an explicit choice, so the gate must not re-ask."""
        for flag, expected in ((True, "practice"), (False, "real")):
            self.cfg.broker.demo_account = flag
            gate = self.gate_mod.SessionGate(self.cfg)
            self.addCleanup(gate.destroy)
            self.assertEqual(gate.purse.get(), expected)

    def test_defaults_match_the_cli(self):
        self.assertEqual(self.gate.profile_var.get(), pairing.DEFAULT_PROFILE)
        self.assertEqual(self.gate.port_var.get(),
                         str(pairing.DEFAULT_CDP_PORT))
        self.assertEqual(self.gate.timeout_var.get(),
                         str(int(pairing.DEFAULT_TIMEOUT)))

    def test_no_purse_blocks_pairing(self):
        self.gate._login()
        self.assertIn("pick a purse", self.gate.status._label)
        self.assertEqual(self.ready, [])

    def test_bad_form_blocks_pairing(self):
        self.gate.purse.set("practice")
        self.gate.port_var.set("nope")
        self.gate._login()
        self.assertIn("port", self.gate.status._label)
        self.assertEqual(self.ready, [])

    def test_success_reports_the_purse_and_the_cookie(self):
        self._pair_ok({"ssid": "QX.gate"})
        self.gate.purse.set("real")
        self.gate._login()
        self.assertEqual(self.ready, [("QX.gate", False)])
        self.assertIn("session captured", self.gate.status._label)
        self.assertFalse(self.gate._pairing)

    def test_practice_purse_is_reported_as_true(self):
        self._pair_ok({"ssid": "QX.gate"})
        self.gate.purse.set("practice")
        self.gate._login()
        self.assertIs(self.ready[-1][1], True)

    def test_failure_keeps_the_window_open(self):
        self._pair_fails(TimeoutError("no session after 240s"))
        self.gate.purse.set("practice")
        self.gate._login()
        self.assertIn("pairing failed", self.gate.status._label)
        self.assertFalse(self.gate._pairing)
        self.assertEqual(self.ready, [])

    def test_empty_cookie_is_reported(self):
        self._pair_ok({})
        self.gate.purse.set("practice")
        self.gate._login()
        self.assertIn("without a sessionid cookie", self.gate.status._label)
        self.assertEqual(self.ready, [])

    def test_quit_reports_cancellation(self):
        self.gate._cancel()
        self.assertEqual(self.cancelled, [True])

    def test_the_session_path_comes_from_config(self):
        seen = {}
        self.gate_mod.run_pairing = (
            lambda **kw: seen.update(kw) or kw["on_done"]({"ssid": "QX.x"}))
        self.gate.purse.set("practice")
        self.gate._login()
        self.assertEqual(seen["session_path"], self.cfg.qx_session_path)


class TestGuiBootsWithoutASession(unittest.TestCase):
    """A first run must open the pairing window, not die in the shell."""

    def setUp(self) -> None:
        class StubEngine:
            closed = False

            def shutdown(self):
                self.closed = True

        self.engine = StubEngine()
        # the gate is a tkinter module, so drive it under the fake toolkit
        self._tk = fake_tk()
        self._tk.__enter__()
        self.addCleanup(self._tk.__exit__, None, None, None)
        import cybertrade.gui as gui_pkg
        import cybertrade.gui.session_gate as gate_mod

        self.gate_mod = gate_mod
        self.gui_pkg = gui_pkg

    def test_missing_session_opens_the_gate(self):
        import cybertrade.cli as cli
        gate_mod, gui_pkg = self.gate_mod, self.gui_pkg

        calls = {}

        def fake_gate(cfg, on_ready=None, on_cancel=None):
            calls["gate"] = True
            on_ready("QX.paired", True)          # operator pairs successfully

        def fake_build(cfg, durable=False):
            calls.setdefault("builds", []).append(durable)
            if len(calls["builds"]) == 1:
                raise ConfigError("no Quotex session — run `cybertrade quotex login`")
            return self.engine

        with mock.patch.object(cli, "_build_engine", fake_build), \
                mock.patch.object(cli, "_load_config",
                                  lambda args: AppConfig()), \
                mock.patch.object(cli, "_resolve_purse", lambda c, a: None), \
                mock.patch.object(cli, "_confirm_live", lambda a, c: True), \
                mock.patch.object(self.gui_pkg, "run_app") as run_app, \
                mock.patch.object(gate_mod, "run_gate", fake_gate):
            args = argparse.Namespace(demo=True)
            self.assertEqual(cli.cmd_gui(args), 0)
        self.assertTrue(calls["gate"])
        self.assertEqual(calls["builds"], [True, True])   # retried after pairing
        self.assertTrue(self.engine.closed)               # and shut down after
        run_app.assert_called_once_with(self.engine, mock.ANY)

    def test_other_config_errors_still_fail_loudly(self):
        import cybertrade.cli as cli
        gate_mod, gui_pkg = self.gate_mod, self.gui_pkg

        def boom(cfg, durable=False):
            raise ConfigError("broker.mode='paper' is not supported")

        with mock.patch.object(cli, "_build_engine", boom), \
                mock.patch.object(cli, "_load_config",
                                  lambda args: AppConfig()), \
                mock.patch.object(cli, "_resolve_purse", lambda c, a: None), \
                mock.patch.object(cli, "_confirm_live", lambda a, c: True), \
                mock.patch.object(gate_mod, "run_gate") as gate:
            with self.assertRaises(ConfigError):
                cli.cmd_gui(argparse.Namespace(demo=True))
        gate.assert_not_called()

    def test_a_cancelled_gate_exits_cleanly(self):
        import cybertrade.cli as cli
        gate_mod, gui_pkg = self.gate_mod, self.gui_pkg

        def fake_gate(cfg, on_ready=None, on_cancel=None):
            pass                                     # operator closes the window

        with mock.patch.object(cli, "_build_engine",
                               side_effect=ConfigError("no Quotex session")), \
                mock.patch.object(cli, "_load_config",
                                  lambda args: AppConfig()), \
                mock.patch.object(cli, "_resolve_purse", lambda c, a: None), \
                mock.patch.object(cli, "_confirm_live", lambda a, c: True), \
                mock.patch.object(self.gui_pkg, "run_app") as run_app, \
                mock.patch.object(gate_mod, "run_gate", fake_gate):
            self.assertEqual(cli.cmd_gui(argparse.Namespace(demo=True)), 1)
        run_app.assert_not_called()

    def test_the_gate_never_writes_the_ssid_to_disk(self):
        import cybertrade.cli as cli
        gate_mod, gui_pkg = self.gate_mod, self.gui_pkg

        captured = {}

        def fake_gate(cfg, on_ready=None, on_cancel=None):
            captured["before"] = cfg.broker.ssid
            on_ready("QX.secret", True)
            captured["after"] = cfg.broker.ssid

        with mock.patch.object(cli, "_build_engine",
                               side_effect=[ConfigError("no Quotex session"),
                                            self.engine]), \
                mock.patch.object(cli, "_load_config",
                                  lambda args: AppConfig()), \
                mock.patch.object(cli, "_resolve_purse", lambda c, a: None), \
                mock.patch.object(cli, "_confirm_live", lambda a, c: True), \
                mock.patch.object(gui_pkg, "run_app"), \
                mock.patch.object(gate_mod, "run_gate", fake_gate):
            cli.cmd_gui(argparse.Namespace(demo=True))
        self.assertEqual(captured["before"], "")
        self.assertEqual(captured["after"], "QX.secret")   # memory only


if __name__ == "__main__":
    unittest.main()
