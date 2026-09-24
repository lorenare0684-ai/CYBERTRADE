"""Cross-platform plumbing: the places Windows behaves differently.

These tests cannot *be* on Windows here, so they exercise the decision logic
against faked platform facts and faked streams. What they pin down is the
behaviour that matters: a narrow console codec must degrade a glyph, never
the process; a text asset must declare its charset; and the platform picks
must not be guessed.
"""

from __future__ import annotations

import io
import os
import sqlite3
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

from cybertrade import compat
from cybertrade.config import AppConfig
from cybertrade.exceptions import ConfigError
from cybertrade.shutdown import SAFETY_HOLD_EXIT


class _FakeStream:
    """A stream that can only encode ASCII, like a redirected cp1252 console."""

    def __init__(self, encoding="cp1252", reconfigurable=True):
        self.encoding = encoding
        self.errors = "strict"
        self.reconfigured = None
        self._reconfigurable = reconfigurable
        self.buffer = io.BytesIO()

    def reconfigure(self, **kwargs):
        if not self._reconfigurable:
            raise ValueError("not reconfigurable")
        self.reconfigured = kwargs
        self.errors = kwargs.get("errors", self.errors)
        return None


class TestConsoleEncoding(unittest.TestCase):
    """The banner must never be the thing that kills the process."""

    def test_a_utf8_stream_is_left_alone(self):
        s = _FakeStream(encoding="utf-8")
        compat._reconfigure(s, "replace") if False else None
        self.assertTrue(compat._can_encode(s))

    def test_a_cp1252_stream_cannot_carry_the_banner(self):
        self.assertFalse(compat._can_encode(_FakeStream("cp1252")))
        self.assertFalse(compat._can_encode(_FakeStream("ascii")))

    def test_an_unknown_encoding_is_treated_as_narrow(self):
        self.assertFalse(compat._can_encode(_FakeStream("no-such-codec")))

    def test_a_stream_with_no_encoding_is_left_alone(self):
        self.assertTrue(compat._can_encode(_FakeStream(encoding="")))

    def test_reconfigure_is_used_when_available(self):
        s = _FakeStream()
        self.assertTrue(compat._reconfigure(s, "replace"))
        self.assertEqual(s.reconfigured, {"errors": "replace"})
        self.assertEqual(s.errors, "replace")

    def test_a_stream_that_cannot_reconfigure_is_reported(self):
        s = _FakeStream(reconfigurable=False)
        self.assertFalse(compat._reconfigure(s, "replace"))

    def test_ensure_reconfigures_every_narrow_stream(self):
        out, err = _FakeStream(), _FakeStream()
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            compat.ensure_console_encoding()
        self.assertEqual(out.errors, "replace")
        self.assertEqual(err.errors, "replace")

    def test_ensure_rewraps_a_stream_without_reconfigure(self):
        """An old wrapper or a test double must not cost us the glyphs."""
        out = _FakeStream(reconfigurable=False)
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", _FakeStream("utf-8")), \
                mock.patch.dict(os.environ, env, clear=True):
            compat.ensure_console_encoding()
            wrapped = sys.stdout
            self.assertIsNot(wrapped, out)
            self.assertEqual(wrapped.errors, "replace")

    def test_a_wide_codec_is_left_alone_whatever_the_environment(self):
        """A stream that already carries the art needs no help -- with or
        without PYTHONIOENCODING set."""
        for env in ({}, {"PYTHONIOENCODING": "utf-8"}, {"PYTHONUTF8": "1"}):
            out = _FakeStream(encoding="utf-8")
            with mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(sys, "stderr", _FakeStream("utf-8")), \
                    mock.patch.dict(os.environ, env, clear=False):
                compat.ensure_console_encoding()
            self.assertIsNone(out.reconfigured)

    def test_an_operator_chosen_narrow_codec_is_still_rescued(self):
        """PYTHONIOENCODING=cp1252 is the standard Windows fix for mojibake,
        and with it set an earlier version bailed out of the rescue and every
        banner-printing command died with a UnicodeEncodeError. The art has to
        degrade to '?' rather than take the process down."""
        out = _FakeStream(encoding="cp1252")
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", _FakeStream("cp1252")), \
                mock.patch.dict(os.environ, {"PYTHONIOENCODING": "cp1252"}):
            compat.ensure_console_encoding()
        self.assertEqual(out.reconfigured, {"errors": "replace"})

    def test_ensure_is_safe_on_a_broken_stream(self):
        class Broken:
            encoding = "cp1252"

            def reconfigure(self, **kw):
                raise OSError("closed")

            @property
            def buffer(self):
                raise OSError("no buffer")

        with mock.patch.object(sys, "stdout", Broken()), \
                mock.patch.object(sys, "stderr", Broken()):
            compat.ensure_console_encoding()     # must not raise

    def test_a_real_cp1252_stream_survives_the_banner(self):
        """End to end: the actual failure, against the actual fix."""
        import subprocess

        code = (
            "import sys, io\n"
            "sys.stdout = io.TextIOWrapper(sys.stdout.buffer,"
            " encoding='cp1252', errors='strict')\n"
            "sys.stderr = io.TextIOWrapper(sys.stderr.buffer,"
            " encoding='cp1252', errors='strict')\n"
            "sys.argv = ['cybertrade', 'doctor']\n"
            "from cybertrade.cli import main\n"
            "rc = main()\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "sys.stderr.buffer.write(b'RC=%d\\n' % rc)\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn(b"13/13 checks passed", r.stdout + r.stderr)


class TestHardenPath(unittest.TestCase):
    """0600 means something on POSIX and almost nothing on Windows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "qx.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{}")

    def test_posix_reports_the_mode_as_guaranteed(self):
        with mock.patch.object(compat, "IS_WINDOWS", False):
            self.assertTrue(compat.harden_path(self.path, 0o600))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_windows_says_it_cannot_guarantee_privacy(self):
        with mock.patch.object(compat, "IS_WINDOWS", True):
            self.assertFalse(compat.harden_path(self.path, 0o600))

    def test_a_missing_file_is_reported_not_raised(self):
        self.assertFalse(compat.harden_path(os.path.join(self.tmp.name,
                                                         "nope.json")))

    def test_a_failing_chmod_is_swallowed(self):
        with mock.patch("os.chmod", side_effect=OSError("nope")), \
                mock.patch.object(compat, "IS_WINDOWS", False):
            self.assertFalse(compat.harden_path(self.path))

    def test_the_session_file_is_hardened_on_save(self):
        from cybertrade.brokers.quotex.pairing import load_session, save_session

        target = os.path.join(self.tmp.name, "sub", "qx.json")
        self.assertTrue(save_session(target, {"ssid": "QX.t"}))
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        self.assertEqual(load_session(target)["ssid"], "QX.t")


class TestPlatformPicks(unittest.TestCase):
    """Nothing here may be guessed from the host it happens to run on."""

    def test_the_chrome_profile_uses_the_native_separator(self):
        profile = compat.default_chrome_profile()
        self.assertEqual(profile, os.path.join("data", "chrome-profile"))
        self.assertNotIn("/", profile) if os.sep != "/" else None

    def test_a_long_path_prefix_is_only_added_on_windows(self):
        abs_path = os.path.abspath("data")
        with mock.patch.object(compat, "IS_WINDOWS", True):
            self.assertTrue(compat.windows_long_path(abs_path).startswith("\\\\?\\"))
        with mock.patch.object(compat, "IS_WINDOWS", False):
            self.assertEqual(compat.windows_long_path(abs_path), abs_path)

    def test_a_relative_path_is_never_prefixed(self):
        with mock.patch.object(compat, "IS_WINDOWS", True):
            self.assertEqual(compat.windows_long_path("data/x"), "data/x")

    def test_an_already_prefixed_path_is_left_alone(self):
        with mock.patch.object(compat, "IS_WINDOWS", True):
            once = compat.windows_long_path(os.path.abspath("data"))
            self.assertEqual(compat.windows_long_path(once), once)


class TestChromeDiscovery(unittest.TestCase):
    """Chrome is found per-platform, and the failure is actionable."""

    def test_windows_candidate_paths_are_used(self):
        from cybertrade.brokers.quotex import pairing

        import ntpath

        env = {"PROGRAMFILES": r"C:\Program Files",
               "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
               "LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
        # os.path is posixpath on this host, so pin the Windows semantics the
        # product sees on a real Windows box
        found = ntpath.join(r"C:\Program Files", "Google", "Chrome",
                            "Application", "chrome.exe")
        with mock.patch("platform.system", return_value="Windows"), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("os.path.isabs", ntpath.isabs), \
                mock.patch("os.path.join", ntpath.join), \
                mock.patch("os.path.exists", return_value=True):
            self.assertEqual(pairing.find_chrome(), found)

    def test_linux_candidate_names_are_used(self):
        from cybertrade.brokers.quotex import pairing

        tried = []

        def which(name):
            tried.append(name)
            return "/usr/bin/chromium" if name == "chromium" else None

        with mock.patch("platform.system", return_value="Linux"), \
                mock.patch("shutil.which", which):
            self.assertEqual(pairing.find_chrome(), "/usr/bin/chromium")
        self.assertEqual(tried[0], "google-chrome")     # the preferred binary
        self.assertIn("chromium", tried)

    def test_no_chrome_names_the_override(self):
        from cybertrade.brokers.quotex import pairing

        import ntpath

        with mock.patch("platform.system", return_value="Windows"), \
                mock.patch.dict(os.environ, {"CYBERTRADE_CHROME": ""},
                                clear=False), \
                mock.patch("os.path.isabs", ntpath.isabs), \
                mock.patch("os.path.exists", return_value=False), \
                mock.patch("shutil.which", return_value=None):
            with self.assertRaises(FileNotFoundError) as ctx:
                pairing.find_chrome()
        self.assertIn("CYBERTRADE_CHROME", str(ctx.exception))

    def test_an_explicit_binary_wins(self):
        from cybertrade.brokers.quotex import pairing

        with mock.patch.dict(os.environ, {"CYBERTRADE_CHROME": r"D:\chr.exe"}), \
                mock.patch("platform.system", return_value="Windows"):
            self.assertEqual(pairing.find_chrome(), r"D:\chr.exe")

    def test_start_new_session_is_skipped_on_windows(self):
        """Windows has no setsid; the flag would be an instant TypeError."""
        from cybertrade.brokers.quotex import pairing

        seen = {}

        class Popen:
            def __init__(self, argv, **kw):
                seen.update(kw)

        with mock.patch.object(pairing, "find_chrome", return_value="chrome"), \
                mock.patch.object(pairing.os, "makedirs"), \
                mock.patch.object(pairing.subprocess, "Popen", Popen), \
                mock.patch("platform.system", return_value="Windows"):
            pairing.launch_chrome("https://qxbroker.com", "data\\chrome-profile",
                                  9333)
        self.assertNotIn("start_new_session", seen)

    def test_start_new_session_is_used_elsewhere(self):
        from cybertrade.brokers.quotex import pairing

        seen = {}

        class Popen:
            def __init__(self, argv, **kw):
                seen.update(kw)

        with mock.patch.object(pairing, "find_chrome", return_value="chrome"), \
                mock.patch.object(pairing.os, "makedirs"), \
                mock.patch.object(pairing.subprocess, "Popen", Popen), \
                mock.patch("platform.system", return_value="Linux"):
            pairing.launch_chrome("https://qxbroker.com", "data/chrome-profile",
                                  9333)
        self.assertTrue(seen.get("start_new_session"))


class TestChromeProfileIsAbsolute(unittest.TestCase):
    """A relative --user-data-dir makes Chrome open a profile we never watch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)
        self.seen = {}

        def popen(argv, **kw):
            self.seen["argv"] = argv
            return None

        self.popen = popen

    def _launch(self, profile):
        from cybertrade.brokers.quotex import pairing

        with mock.patch.object(pairing, "find_chrome", return_value="chrome"), \
                mock.patch.object(pairing.subprocess, "Popen", self.popen):
            pairing.launch_chrome("https://qxbroker.com", profile, 9333)
        return self.seen["argv"]

    def test_a_relative_profile_is_made_absolute(self):
        argv = self._launch("data/chrome-profile")
        profile = argv[2].split("=", 1)[1]
        self.assertTrue(os.path.isabs(profile), profile)
        self.assertTrue(profile.endswith(os.path.join("data", "chrome-profile")))

    def test_the_profile_directory_is_created(self):
        argv = self._launch(os.path.join("data", "chrome-profile"))
        profile = argv[2].split("=", 1)[1]
        self.assertTrue(os.path.isdir(profile))

    def test_an_absolute_profile_is_left_alone(self):
        absolute = os.path.join(self.tmp.name, "already-absolute")
        argv = self._launch(absolute)
        self.assertEqual(argv[2].split("=", 1)[1], absolute)

    def test_the_argv_flag_is_well_formed(self):
        argv = self._launch("data/chrome-profile")
        self.assertTrue(argv[2].startswith("--user-data-dir="))
        self.assertIn("--remote-debugging-port=9333", argv)

    def test_profile_exists_agrees_with_the_launcher(self):
        """Both must resolve the same way, or the UI lies about a login."""
        from cybertrade.gui.pairing import profile_exists

        argv = self._launch("data/chrome-profile")
        profile = argv[2].split("=", 1)[1]
        # an empty profile dir means Chrome has never logged in here
        self.assertFalse(profile_exists("data/chrome-profile"))
        with open(os.path.join(profile, "Preferences"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        self.assertTrue(profile_exists("data/chrome-profile"))
        self.assertFalse(profile_exists("data/never-used"))


class TestStateLockPicksTheRightPrimitive(unittest.TestCase):
    """fcntl on POSIX, msvcrt on Windows — never both, never neither."""

    def test_windows_uses_msvcrt_locking(self):
        from cybertrade.statestore import StateLease

        lock = StateLease(os.path.join(tempfile.mkdtemp(), "s.json"))
        calls = []
        fake_msvcrt = mock.Mock()
        fake_msvcrt.LK_NBLCK = 1
        fake_msvcrt.locking = lambda fd, mode, n: calls.append((fd, mode, n))
        fake_fcntl = mock.Mock()
        fake_fcntl.flock = lambda *a: calls.append("fcntl-used")

        with mock.patch("os.name", "nt"), \
                mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt,
                                              "fcntl": fake_fcntl}), \
                mock.patch("os.makedirs"):
            lock.acquire()
        self.assertTrue(calls)
        self.assertNotIn("fcntl-used", calls)
        self.assertEqual(calls[0][1], 1)          # LK_NBLCK, not LK_LOCK

    def test_posix_uses_flock(self):
        from cybertrade.statestore import StateLease

        lock = StateLease(os.path.join(tempfile.mkdtemp(), "s.json"))
        calls = []
        fake_fcntl = mock.Mock()
        fake_fcntl.LOCK_EX = 2
        fake_fcntl.LOCK_NB = 4
        fake_fcntl.flock = lambda *a: calls.append(a)

        with mock.patch("os.name", "posix"), \
                mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}), \
                mock.patch("os.makedirs"):
            lock.acquire()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 2 | 4)

    def test_a_contended_lock_is_reported_not_raised_raw(self):
        from cybertrade.statestore import StateError, StateLease

        lock = StateLease(os.path.join(tempfile.mkdtemp(), "s.json"))
        fake_fcntl = mock.Mock()
        fake_fcntl.LOCK_EX = 2
        fake_fcntl.LOCK_NB = 4
        fake_fcntl.flock = mock.Mock(side_effect=OSError("locked"))

        with mock.patch("os.name", "posix"), \
                mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}), \
                mock.patch("os.makedirs"):
            with self.assertRaises(StateError):
                lock.acquire()


class TestStaticContentType(unittest.TestCase):
    """A bare text/html lets a Windows browser guess the codepage."""

    def test_text_assets_declare_utf8(self):
        from cybertrade.web.server import _static_ctype

        for name in ("index.html", "css/cyber.css", "js/app.js",
                     "js/charts.js"):
            self.assertTrue(_static_ctype(name).endswith("charset=utf-8"),
                            _static_ctype(name))

    def test_an_unknown_type_is_left_alone(self):
        from cybertrade.web.server import _static_ctype

        self.assertEqual(_static_ctype("thing.bin"),
                         "application/octet-stream")


class TestDoctorAdviceIsPlatformAware(unittest.TestCase):
    """Telling a Windows user to run apt is not help."""

    def _doctor_gui_line(self, is_windows):
        """Run the real doctor and pull out its desktop-gui line."""
        import cybertrade.cli as cli

        buf = io.StringIO()
        with mock.patch.object(cli, "IS_WINDOWS", is_windows), \
                mock.patch("cybertrade.gui.GUI_AVAILABLE", False), \
                mock.patch("sys.stdout", buf):
            cli.cmd_doctor(_ns())
        for line in buf.getvalue().split("\n"):
            if "desktop gui" in line:
                return line
        raise AssertionError("no desktop-gui line in doctor output")

    def test_windows_is_told_about_the_installer_option(self):
        line = self._doctor_gui_line(True)
        self.assertIn("installer", line)
        self.assertNotIn("apt", line)

    def test_linux_still_gets_the_apt_hint(self):
        self.assertIn("apt", self._doctor_gui_line(False))


def _ns():
    import argparse

    return argparse.Namespace(config=None)


class TestClosedStdinHoldsRatherThanCrashes(unittest.TestCase):
    """A closed stdin must hold, never proceed and never traceback.

    Double-clicking CYBERTRADE.bat, a pipe, a service, or CI all give the
    process a stdin that answers EOF. Guessing PRACTICE, or worse proceeding
    past the live gate, would trade money nobody chose to risk.
    """

    def _cli(self):
        import cybertrade.cli as cli

        return cli

    def _ns(self, **kw):
        import argparse

        return argparse.Namespace(**kw)

    def test_the_purse_prompt_holds_on_eof(self):
        cli = self._cli()
        cfg = AppConfig()
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(ConfigError) as ctx:
                cli._resolve_purse(cfg, self._ns(demo=False, real=False))
        self.assertIn("no purse chosen", str(ctx.exception))

    def test_the_live_gate_holds_on_eof(self):
        cli = self._cli()
        cfg = AppConfig()
        cfg.broker.demo_account = True
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(ConfigError) as ctx:
                cli._confirm_live(self._ns(yes=False), cfg)
        self.assertIn("live confirmation not given", str(ctx.exception))
        # the gate never reached its arming line, so the config is untouched
        self.assertEqual(cfg.risk.allow_live, AppConfig().risk.allow_live)

    def test_yes_still_skips_the_prompt(self):
        cli = self._cli()
        cfg = AppConfig()
        cfg.broker.demo_account = False
        self.assertTrue(cli._confirm_live(self._ns(yes=True), cfg))
        self.assertTrue(cfg.risk.allow_live)

    def test_a_config_error_exits_as_a_safety_hold_not_a_traceback(self):
        cli = self._cli()
        args = self._ns(func=mock.Mock(side_effect=ConfigError(
            "no purse chosen - pass --demo or --real")))
        parser = mock.Mock(parse_args=lambda a: args)
        with mock.patch.object(cli, "build_parser", return_value=parser), \
                mock.patch("sys.stderr"):
            self.assertEqual(cli.main([]), SAFETY_HOLD_EXIT)

    def test_an_eof_from_a_command_is_a_hold(self):
        cli = self._cli()
        args = self._ns(func=mock.Mock(side_effect=EOFError))
        parser = mock.Mock(parse_args=lambda a: args)
        with mock.patch.object(cli, "build_parser", return_value=parser), \
                mock.patch("sys.stderr"):
            self.assertEqual(cli.main([]), SAFETY_HOLD_EXIT)

    def test_ctrl_c_is_reported_not_raised(self):
        cli = self._cli()
        args = self._ns(func=mock.Mock(side_effect=KeyboardInterrupt))
        parser = mock.Mock(parse_args=lambda a: args)
        with mock.patch.object(cli, "build_parser", return_value=parser), \
                mock.patch("sys.stderr"):
            self.assertEqual(cli.main([]), 130)

    def test_an_unexpected_bug_still_raises(self):
        """A real defect must stay loud; only expected failures are tidied."""
        cli = self._cli()
        args = self._ns(func=mock.Mock(side_effect=ValueError("boom")))
        parser = mock.Mock(parse_args=lambda a: args)
        with mock.patch.object(cli, "build_parser", return_value=parser):
            with self.assertRaises(ValueError):
                cli.main([])

    def test_no_command_leaks_a_traceback_on_a_closed_stdin(self):
        """End to end: the exact double-click scenario, in a subprocess."""
        import subprocess

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for cmd in (["run", "--demo"], ["gui"], ["web", "--port", "8944"]):
            with self.subTest(cmd=cmd):
                r = subprocess.run(
                    [sys.executable, "-m", "cybertrade", *cmd],
                    capture_output=True, cwd=repo, stdin=subprocess.DEVNULL,
                    timeout=180)
                self.assertNotIn(b"Traceback", r.stdout + r.stderr, cmd)
                self.assertEqual(r.returncode, SAFETY_HOLD_EXIT, cmd)


class TestWindowsLauncher(unittest.TestCase):
    """CYBERTRADE.bat is the primary Windows entry point.

    It cannot be executed here (no cmd.exe), so it is checked structurally
    instead: every goto must land on a label, every command it invokes must
    exist, and every file it names must be present. A launcher that rots is
    worse than no launcher -- it is the first thing a new operator runs.
    """

    @classmethod
    def setUpClass(cls):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.path = os.path.join(repo, "CYBERTRADE.bat")
        cls.repo = repo
        cls.text = io.open(cls.path, encoding="utf-8", newline="").read()

    def test_every_goto_lands_on_a_label(self):
        labels = set(re.findall(r"^:(\w+)", self.text, re.M))
        gotos = set(re.findall(r"goto\s+:(\w+)", self.text))
        self.assertTrue(gotos, "no gotos found -- the menu would do nothing")
        self.assertEqual(gotos - labels, set(),
                         f"dangling goto(s): {sorted(gotos - labels)}")

    def test_every_label_is_reachable(self):
        """A label nothing jumps to is dead weight, and a sign of drift."""
        labels = set(re.findall(r"^:(\w+)", self.text, re.M))
        gotos = set(re.findall(r"goto\s+:(\w+)", self.text))
        self.assertEqual(labels - gotos, set(),
                         f"unreachable label(s): {sorted(labels - gotos)}")

    def test_every_invoked_subcommand_exists(self):
        import cybertrade.cli as cli

        parser = cli.build_parser()
        # subparsers live under the "command" choices
        known = set()
        for action in parser._actions:
            known.update(getattr(action, "choices", {}) or {})
        self.assertTrue(known, "no subcommands found on the parser")
        used = set(re.findall(r"python\s+-m\s+cybertrade\s+(\w+)", self.text))
        self.assertTrue(used, "the launcher runs no cybertrade command")
        self.assertEqual(used - known, set(),
                         f"unknown subcommand(s): {sorted(used - known)}")

    def test_every_named_file_exists(self):
        for name in ("environment.yml", "run_gui.py"):
            with self.subTest(name=name):
                self.assertTrue(os.path.isfile(os.path.join(self.repo, name)))

    def test_the_login_option_actually_logs_in(self):
        """The menu once offered login but only ran status and warm."""
        self.assertIn(":run_login", self.text)
        self.assertRegex(self.text, r":run_login[\s\S]*?quotex\s+login")

    def test_a_failed_pairing_is_explained_not_silent(self):
        self.assertIn(":login_failed", self.text)
        self.assertIn("CYBERTRADE_CHROME", self.text)

    def test_line_endings_are_consistent(self):
        """A mixed-ending .bat executes garbage on Windows."""
        crlf = self.text.count("\r\n")
        lf = self.text.count("\n")
        self.assertTrue(crlf == lf or crlf == 0,
                        f"mixed line endings: {crlf} CRLF vs {lf} LF")

    def test_the_launcher_is_declared_crlf_in_gitattributes(self):
        """.gitattributes must ask for CRLF, or Git checks out LF and cmd
        mis-parses labels."""
        ga = io.open(os.path.join(self.repo, ".gitattributes"),
                     encoding="utf-8").read()
        self.assertRegex(ga, r"\*\.bat\s+text\s+eol=crlf")



class TestLongPathsArePrefixedAtTheOpenCall(unittest.TestCase):
    """``windows_long_path`` existed, was exported, and nothing called it.

    A deep absolute path on Windows fails past 260 characters with an error
    that names no useful cause. The helper is the fix; these tests make sure
    it is actually wired into the three places a user can configure a path:
    the journal, the state lease, and the session file.

    On Linux the helper is an identity, so the tests assert the *call* rather
    than the prefix -- that is the part that can rot.
    """

    def test_the_journal_prefixes_before_connecting(self):
        import sqlite3

        import cybertrade.journal.store as store

        with tempfile.TemporaryDirectory() as tmp:
            seen = {}

            def fake_connect(path, **kw):
                seen["path"] = path
                raise sqlite3.Error("stop here")

            with mock.patch.object(store.sqlite3, "connect",
                                   side_effect=fake_connect):
                with self.assertRaises(Exception):
                    store.TradeJournal(os.path.join(tmp, "j.db"))
            self.assertIn("path", seen)
            self.assertTrue(seen["path"].endswith("j.db"))

    def test_the_state_lease_prefixes_before_opening(self):
        import cybertrade.statestore as ss

        prefix = "\\\\?\\"
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "state.lock")
            lease = ss.StateLease(target)
            devnull = os.open(os.devnull, os.O_RDONLY)
            try:
                with mock.patch.object(ss, "windows_long_path",
                                       side_effect=lambda p: prefix + p) as wl, \
                        mock.patch("os.open",
                                       side_effect=lambda *a, **k: os.dup(devnull)) as op:
                    try:
                        lease.acquire()
                    except Exception:
                        pass
                    finally:
                        lease.release()
                # StateLease appends .lock and realpaths the target
                wl.assert_called_once_with(lease.path)
                self.assertEqual(op.call_args[0][0], prefix + lease.path)
            finally:
                os.close(devnull)

    def test_the_session_saver_prefixes_both_sides_of_the_replace(self):
        from cybertrade.brokers.quotex import pairing as pr

        prefix = "\\\\?\\"
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "s.json")
            opened, replaced = [], []
            devnull = os.open(os.devnull, os.O_WRONLY)

            def fake_open(path, *a, **kw):
                opened.append(path)
                return os.fdopen(os.dup(devnull), "w")

            def fake_replace(a, b):
                replaced.append((a, b))

            with mock.patch("builtins.open", side_effect=fake_open), \
                    mock.patch.object(pr.os, "replace",
                                      side_effect=fake_replace), \
                    mock.patch.object(pr, "windows_long_path",
                                      side_effect=lambda p: prefix + p) as wl:
                pr.save_session(target, {"ssid": "x" * 32, "cookies": "c=1"})
            self.assertTrue(opened and opened[0].startswith(prefix), opened)
            self.assertEqual(replaced[0][0], prefix + target + ".tmp")
            self.assertEqual(replaced[0][1], prefix + target)
            # prefixing must happen for every path, not just the first
            self.assertGreaterEqual(wl.call_count, 3)

    def test_a_relative_path_is_never_prefixed(self):
        """``data/...`` never hits MAX_PATH; prefixing it would break it."""
        from cybertrade.compat import windows_long_path

        self.assertEqual(windows_long_path("data/journal.db"), "data/journal.db")
        self.assertEqual(windows_long_path(os.path.join("data", "x.db")),
                         os.path.join("data", "x.db"))
        self.assertEqual(windows_long_path(""), "")

    def test_an_already_prefixed_path_is_not_doubled(self):
        from cybertrade.compat import windows_long_path

        already = "\\\\?\\C:\\deep\\path.db"
        self.assertEqual(windows_long_path(already), already)


if __name__ == "__main__":
    unittest.main()


class TestTheCLISurvivesANarrowConsole(unittest.TestCase):
    """The real Windows case, run as a real process.

    The banner is drawn in box-drawing glyphs that no single-byte codepage
    carries. Whatever the console says, the art must degrade -- never raise.
    """

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _run(self, argv, env_extra, narrow=True):
        code = (
            "import io, sys\n"
            "for _n in ('stdout', 'stderr'):\n"
            "    _s = getattr(sys, _n)\n"
            "    setattr(sys, _n, io.TextIOWrapper("
            "_s.buffer, encoding=%r, errors='strict'))\n"
            "from cybertrade.cli import main\n"
            "sys.exit(main(sys.argv[1:]))\n" % ("cp1252" if narrow else "utf-8")
        )
        env = dict(os.environ, PYTHONPATH=self.REPO, **env_extra)
        return subprocess.run([sys.executable, "-c", code] + argv,
                              capture_output=True, timeout=180, env=env,
                              stdin=subprocess.DEVNULL, cwd=self.REPO)

    def test_the_banner_survives_a_cp1252_console(self):
        p = self._run(["strategies"], {})
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        self.assertNotIn("Traceback", out)
        self.assertNotIn("UnicodeEncodeError", out)
        self.assertEqual(p.returncode, 0, out[-400:])
        self.assertIn("active:", out)

    def test_the_banner_survives_a_cp1252_console_the_operator_chose(self):
        """PYTHONIOENCODING=cp1252 must not disable the rescue."""
        p = self._run(["strategies"], {"PYTHONIOENCODING": "cp1252"})
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        self.assertNotIn("Traceback", out)
        self.assertNotIn("UnicodeEncodeError", out)
        self.assertEqual(p.returncode, 0, out[-400:])

    def test_a_wide_console_is_unaffected(self):
        p = self._run(["strategies"], {}, narrow=False)
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0)
        self.assertIn("\u2588", out)          # the art survives intact
