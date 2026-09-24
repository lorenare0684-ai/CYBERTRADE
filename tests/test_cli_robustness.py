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



if __name__ == "__main__":
    unittest.main()
