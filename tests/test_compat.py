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
import sys
import tempfile
import unittest
import unittest.mock as mock

from cybertrade import compat


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

    def test_an_explicit_operator_choice_is_respected(self):
        out = _FakeStream()
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.dict(os.environ, {"PYTHONUTF8": "1"}):
            compat.ensure_console_encoding()
        self.assertIsNone(out.reconfigured)     # untouched: they already chose

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


if __name__ == "__main__":
    unittest.main()
