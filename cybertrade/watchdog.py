"""Bounded process supervision, distinct from bot.watchdog's market guard.

The CLI only supervises PAPER execution. Children get a unique run ID;
only their PID + run ID + advancing completed-cycle counter count as
progress. Monotonic deadlines ignore wall-clock jumps and stale files.
A reader drains stdout concurrently into a bounded tail, so verbose children
cannot deadlock on a full pipe. Clean exits and safety holds never respawn.
"""
from __future__ import annotations

import logging
import math
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Sequence

from .statestore import finite, integer, read_heartbeat

log = logging.getLogger("cybertrade.supervisor")
WATCH_SLICE = 1.0
SAFETY_HOLD_EXIT = 78
MAX_OUTPUT = 64_000


def default_child_cmd() -> List[str]:
    return [sys.executable, "-u", "-m", "cybertrade", "run", "--supervised"]


def backoff_seconds(attempt: int, *, base: float = 2.0, factor: float = 2.0,
                    cap: float = 300.0, jitter: float = 0.2,
                    rnd: Optional[random.Random] = None) -> float:
    """Bounded even for huge attempt counts; jitter never exceeds the cap."""
    finite(base, "base", 0.1)
    finite(factor, "factor", 1.0)
    finite(cap, "cap", 0.1)
    finite(jitter, "jitter")
    if jitter > 1:
        raise ValueError("jitter must be <= 1")
    try:
        exponent = math.log(base) + max(0, int(attempt) - 1) * math.log(factor)
    except OverflowError:
        exponent = math.log(cap)
    raw = math.exp(min(math.log(cap), exponent))
    return min(cap, max(0.1, raw * (1 + (rnd or random).uniform(-jitter, jitter))))


class RestartBudget:
    def __init__(self, max_restarts: int = 5, window_seconds: float = 600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_restarts = integer(max_restarts, "max_restarts")
        self.window_seconds = finite(window_seconds, "window_seconds", 0.1)
        self._clock = clock
        self._stamps: List[float] = []

    @property
    def used(self) -> int:
        horizon = self._clock() - self.window_seconds
        self._stamps = [ts for ts in self._stamps if ts > horizon]
        return len(self._stamps)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_restarts

    def allow(self) -> bool:
        if self.exhausted:
            return False
        self._stamps.append(self._clock())
        return True

    def status(self) -> dict:
        used = self.used
        return {"used": used, "max": self.max_restarts, "window": self.window_seconds,
                "exhausted": used >= self.max_restarts}


def _redact(value: str) -> str:
    value = re.sub(r"(?im)\b(authorization|cookie|set-cookie)\s*:\s*[^\n]+",
                   r"\1: [REDACTED]", value)
    return re.sub(
        r'''(?i)(["']?(?:sessionid|session|ssid|password|token)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''',
        r"\1[REDACTED]", value,
    )


def _safe_command(cmd: Sequence[str]) -> str:
    safe, hide = [], False
    for arg in cmd:
        if hide:
            safe.append("[REDACTED]")
            hide = False
            continue
        safe.append(_redact(str(arg)))
        hide = str(arg).lower() in ("--ssid", "--password", "--token", "--cookie")
    return shlex.join(safe)


def write_crash_report(crash_dir: str, *, cmd: Sequence[str], exit_code: Any,
                       output: str = "", ts: Optional[float] = None,
                       reason: str = "crash", max_output: int = MAX_OUTPUT,
                       keep: int = 20) -> Optional[str]:
    """Private atomic reports, unique names, bounded output and retention."""
    if not crash_dir:
        return None
    tmp = None
    try:
        os.makedirs(crash_dir, exist_ok=True)
        ts = time.time() if ts is None else ts
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(ts))
        reason = re.sub(r"[^a-zA-Z0-9_-]", "_", reason)[:40]
        fd, tmp = tempfile.mkstemp(prefix=f"crash-{stamp}-{reason}-", suffix=".tmp",
                                   dir=crash_dir)
        path = tmp[:-4] + ".txt"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"time: {ts}\nreason: {reason}\nexit_code: {exit_code}\n"
                     f"cmd: {_safe_command(cmd)}\n--- child output (bounded tail) ---\n")
            fh.write(_redact(output or "")[-max(1, int(max_output)):] + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
        reports = sorted((os.path.join(crash_dir, n) for n in os.listdir(crash_dir)
                          if n.startswith("crash-") and n.endswith(".txt")
                          and os.path.join(crash_dir, n) != path),
                         key=lambda p: os.stat(p).st_mtime_ns, reverse=True)
        # Coarse/tied mtimes must never prune the report we just returned.
        for old in reports[max(1, keep) - 1:]:
            os.unlink(old)
        log.warning("child %s (code=%s); report: %s", reason, exit_code, path)
        return path
    except OSError:
        log.exception("cannot write crash report")
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _OutputTail:
    def __init__(self, stream, limit: int = MAX_OUTPUT) -> None:
        self.stream, self.limit = stream, limit
        self.data = b""
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._read, daemon=True, name="child-output")
        self.thread.start()

    def _read(self) -> None:
        try:
            while self.stream is not None:
                chunk = self.stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                with self.lock:
                    self.data = (self.data + chunk)[-self.limit:]
        except (OSError, ValueError):
            pass

    def finish(self) -> str:
        self.thread.join(timeout=2.0)
        # Do not block on close while a descendant still holds the pipe open.
        if not self.thread.is_alive() and self.stream is not None:
            self.stream.close()
        with self.lock:
            return self.data.decode("utf-8", "replace")


@contextmanager
def stop_on_sigterm():
    """SIGTERM follows the same cleanup path as Ctrl+C (main thread only)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = signal.getsignal(signal.SIGTERM)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def _default_spawn(cmd, **kw):
    proc = subprocess.Popen(cmd, start_new_session=(os.name == "posix"), **kw)
    proc._cybertrade_group = os.name == "posix"
    return proc


class Supervisor:
    def __init__(self, cmd: Optional[Sequence[str]] = None, *,
                 heartbeat_path: str = "data/heartbeat.json", crash_dir: str = "data/crashes",
                 budget: Optional[RestartBudget] = None, max_stale: float = 45.0,
                 startup_grace: float = 120.0, backoff: Callable[[int], float] = backoff_seconds,
                 backoff_cap: float = 300.0, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 spawn: Optional[Callable[..., Any]] = None, watch_slice: float = WATCH_SLICE,
                 read_beat: Callable[[str], dict] = read_heartbeat) -> None:
        self.cmd = list(cmd) if cmd else default_child_cmd()
        if not heartbeat_path:
            raise ValueError("a heartbeat path is required")
        self.heartbeat_path, self.crash_dir = heartbeat_path, crash_dir
        self.budget = budget if budget is not None else RestartBudget(clock=clock)
        self.max_stale = finite(max_stale, "max_stale", 0.1)
        self.startup_grace = finite(startup_grace, "startup_grace", 0.1)
        self.backoff_cap = finite(backoff_cap, "backoff_cap", 0.1)
        self._backoff, self._clock, self._wall, self._sleep = backoff, clock, wall, sleep
        self._spawn = spawn or _default_spawn
        self._read_beat = read_beat
        self.watch_slice = finite(watch_slice, "watch_slice", 0.01)
        self.restarts = self.stops = self.hangs = self._attempt = 0
        self.last_report = None

    @staticmethod
    def _signal(proc, hard: bool = False) -> None:
        if getattr(proc, "_cybertrade_group", False):
            try:
                os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            (proc.kill if hard else proc.terminate)()

    def _terminate(self, proc) -> None:
        self._signal(proc)
        try:
            proc.wait(timeout=5.0)
        except (subprocess.TimeoutExpired, TimeoutError):
            self._signal(proc, hard=True)
            proc.wait(timeout=2.0)  # if reaping fails, do NOT spawn a duplicate
        finally:
            # Reap descendants' pipe writers even if their leader exited first.
            if getattr(proc, "_cybertrade_group", False):
                self._signal(proc, hard=True)

    def step(self) -> str:
        started = last_progress = self._clock()
        run_id = uuid.uuid4().hex
        env = dict(os.environ, CYBERTRADE_RUN_ID=run_id)
        proc = collector = None
        last_seq = -1
        reason, exit_code, output = "", None, ""
        try:
            proc = self._spawn(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, bufsize=0, env=env)
            collector = _OutputTail(getattr(proc, "stdout", None))
            while True:
                exit_code = proc.poll()
                if exit_code is not None:
                    break
                beat = self._read_beat(self.heartbeat_path)
                if (isinstance(beat, dict) and beat.get("run_id") == run_id
                        and type(beat.get("pid")) is int and beat["pid"] == proc.pid
                        and type(beat.get("cycle")) is int and beat["cycle"] >= 0):
                    if beat.get("state") == "kill":
                        reason, exit_code = "safety-hold", SAFETY_HOLD_EXIT
                        break
                    if beat["cycle"] > last_seq:
                        last_progress, last_seq = self._clock(), beat["cycle"]
                deadline = self.startup_grace if last_seq < 0 else self.max_stale
                if self._clock() - last_progress >= deadline:
                    reason = "stale-heartbeat"
                    self.hangs += 1
                    break
                self._sleep(self.watch_slice)
        except OSError as exc:
            reason, output = "spawn-or-watch-error", f"{type(exc).__name__}: {exc}"
        finally:
            if proc is not None:
                self._terminate(proc)
                if exit_code is None:
                    exit_code = proc.poll()
            if collector is not None:
                output += collector.finish()

        if not reason and exit_code == 0:
            self.stops += 1
            return "stopped"
        if exit_code == SAFETY_HOLD_EXIT:
            reason = "safety-hold"
        self.last_report = write_crash_report(
            self.crash_dir, cmd=self.cmd, exit_code=exit_code, output=output,
            ts=self._wall(), reason=reason or "crash",
        )
        if reason == "safety-hold":
            log.error("child held for operator review; NOT restarting")
            return "held"
        if not self.budget.allow():
            log.critical("restart budget exhausted: %s", self.budget.status())
            return "exhausted"
        self.restarts += 1
        self._attempt = 1 if self._clock() - started >= self.budget.window_seconds else self._attempt + 1
        delay = min(self.backoff_cap, finite(self._backoff(self._attempt), "backoff"))
        log.warning("restart #%d in %.1fs", self.restarts, delay)
        self._sleep(delay)
        return "respawn"

    def run(self, *, max_steps: Optional[int] = None) -> int:
        try:
            with stop_on_sigterm():
                steps = 0
                while max_steps is None or steps < max_steps:
                    steps += 1
                    status = self.step()
                    if status == "stopped":
                        return 0
                    if status == "held":
                        return SAFETY_HOLD_EXIT
                    if status == "exhausted":
                        return 1
        except KeyboardInterrupt:
            log.info("watchdog stopped by operator; child reaped")
        return 0

    def status(self) -> Dict[str, Any]:
        return {"cmd": _safe_command(self.cmd), "restarts": self.restarts,
                "stops": self.stops, "hangs": self.hangs, "budget": self.budget.status(),
                "max_stale": self.max_stale, "startup_grace": self.startup_grace}
