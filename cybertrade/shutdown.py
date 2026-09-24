"""Graceful shutdown plumbing for the live runtime.

The process supervisor that used to live here was PAPER-ONLY by design (it
never re-armed live trading on a crash) and was removed with paper mode.
What survives is the part every runtime needs: a SIGTERM that follows the
same cleanup path as Ctrl+C, and the exit code the CLI uses to tell a
human "a safety hold stopped this, look before you restart".
"""

from __future__ import annotations

import signal
import threading
from contextlib import contextmanager

SAFETY_HOLD_EXIT = 78


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


__all__ = ["SAFETY_HOLD_EXIT", "stop_on_sigterm"]
