"""Logging with a terminal-flavoured neon formatter.

Works on plain streams and keeps ANSI color optional so redirected logs stay
clean.  A memory handler feeds the GUI / web consoles through the event bus.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
from typing import Optional

from .events import Topic, default_bus

# Neon palette (ANSI) --------------------------------------------------------
CYAN = "\x1b[38;5;51m"
MAGENTA = "\x1b[38;5;201m"
YELLOW = "\x1b[38;5;226m"
RED = "\x1b[38;5;196m"
GREEN = "\x1b[38;5;46m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"

_LEVEL_COLORS = {
    logging.DEBUG: DIM,
    logging.INFO: CYAN,
    logging.WARNING: YELLOW,
    logging.ERROR: RED,
    logging.CRITICAL: MAGENTA,
}

_LOCK = threading.Lock()
_INSTALLED = False


class NeonFormatter(logging.Formatter):
    """``[HH:MM:SS.mmm] LEVEL name │ message`` with optional ANSI glow."""

    def __init__(self, use_color: Optional[bool] = None) -> None:
        super().__init__()
        if use_color is None:
            use_color = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
        self.use_color = bool(use_color)

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        ms = int(record.msecs)
        level = record.levelname[:4]
        name = record.name.replace("cybertrade.", "")
        msg = record.getMessage()
        if record.exc_info:
            msg = f"{msg}\n{self.formatException(record.exc_info)}"
        if self.use_color:
            color = _LEVEL_COLORS.get(record.levelno, CYAN)
            return (
                f"{DIM}[{ts}.{ms:03d}]{RESET} {color}{level:<4}{RESET} "
                f"{MAGENTA}{name}{RESET} {DIM}│{RESET} {msg}"
            )
        return f"[{ts}.{ms:03d}] {level:<4} {name} │ {msg}"


class BusLogHandler(logging.Handler):
    """Forward log records onto the event bus so GUIs can tail them."""

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._emitted = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            default_bus.publish(
                Topic.LOG,
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "name": record.name,
                    "msg": record.getMessage(),
                },
                source="log",
            )
            self._emitted += 1
        except Exception:  # noqa: BLE001
            pass

    @property
    def emitted(self) -> int:
        return self._emitted


def setup_logging(
    level: str | int = "INFO",
    log_file: Optional[str] = None,
    use_color: Optional[bool] = None,
    bus_handler: bool = True,
) -> logging.Logger:
    """Install package-wide logging.  Idempotent."""
    global _INSTALLED
    root = logging.getLogger("cybertrade")
    with _LOCK:
        if isinstance(level, str):
            level = getattr(logging, level.upper(), logging.INFO)
        root.setLevel(level)

        if not _INSTALLED:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(NeonFormatter(use_color=use_color))
            root.addHandler(stream)
            if bus_handler:
                root.addHandler(BusLogHandler(level=logging.INFO))
            _INSTALLED = True

        if log_file:
            # A log file is diagnostics, not a dependency. If it cannot be
            # opened -- a protected install directory, a full disk, a synced
            # folder -- the program still runs and still reports on stderr.
            # Losing the log is never a reason to lose the process.
            try:
                parent = os.path.dirname(os.path.abspath(log_file)) or "."
                os.makedirs(parent, exist_ok=True)
                fh = logging.handlers.RotatingFileHandler(
                    log_file, maxBytes=4 * 1024 * 1024, backupCount=5,
                    encoding="utf-8"
                )
                fh.setFormatter(
                    logging.Formatter(
                        "%(asctime)s %(levelname)-5s %(name)s | %(message)s")
                )
                root.addHandler(fh)
            except OSError as exc:
                root.warning("cannot write the log file %s: %s "
                             "(continuing without it)", log_file, exc)

        # Avoid double logging through the root logger.
        root.propagate = False
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under ``cybertrade``."""
    if name.startswith("cybertrade"):
        return logging.getLogger(name)
    return logging.getLogger(f"cybertrade.{name}")


__all__ = ["NeonFormatter", "BusLogHandler", "setup_logging", "get_logger"]
