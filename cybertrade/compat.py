"""Cross-platform plumbing: the places Windows behaves differently.

This project is stdlib-only and runs on Windows, macOS and Linux, but the
three are not symmetric, and the asymmetries are all in the boring places:

- **console encoding.** The neon banner and the check glyphs are box-drawing
  characters. On Windows the console encoding is the *locale* codepage
  (cp1252 in most of the world), which has no code points for ``█`` or ``✓``.
  A real console gets away with it because CPython writes it through
  ``WriteConsoleW`` — but the moment output is **redirected** (``> log.txt``,
  a pipe, a service, CI), Python falls back to the locale codec with strict
  errors and the process dies with ``UnicodeEncodeError`` half way through
  printing its own banner. :func:`ensure_console_encoding` makes that a
  degraded glyph instead of a crash.
- **file locking.** POSIX has ``fcntl.flock``; Windows has ``msvcrt.locking``
  over a byte range. The state lock already picks between them.
- **file modes.** ``0600`` is meaningful on POSIX and advisory at best on
  Windows, where access is governed by ACLs. :func:`harden_path` does what it
  can and says so when it cannot.

Nothing here is imported at module scope by the trading path — it is called
once at process start, from the CLI entry point.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import IO, Optional

log = logging.getLogger("cybertrade.compat")

IS_WINDOWS = os.name == "nt"

# A glyph the terminal art leans on that no single-byte codepage can carry.
# If the stream cannot encode this, it cannot encode the banner either.
_PROBE = "\u2588"  # █


def _can_encode(stream: IO) -> bool:
    """False when the stream's codec has no code point for the banner art.

    Never raises: an odd wrapper (a test double, a stream shim) can expose a
    non-string ``encoding``, and a cosmetics check must not be the thing that
    takes the process down.
    """
    try:
        encoding = getattr(stream, "encoding", None) or ""
        if not encoding or not isinstance(encoding, str):
            return True
        _PROBE.encode(encoding)
    except Exception:  # noqa: BLE001 - see the docstring
        return False
    return True


def _reconfigure(stream: IO, errors: str) -> bool:
    """Best-effort ``errors=`` swap; True when the stream was changed."""
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):          # Python 3.7+
        try:
            reconfigure(errors=errors)
            return True
        except (ValueError, OSError):  # detached / already-closed stream
            return False
    return False


def ensure_console_encoding(errors: str = "replace") -> None:
    """Keep the terminal's glyphs from killing the process on a narrow codec.

    Idempotent, never raises, and a no-op on any stream that already encodes
    the art. Called once from :func:`cybertrade.cli.main` so every entry point
    — ``python -m cybertrade``, ``run_gui.py``, ``run_web.py`` and the
    installed ``cybertrade`` console script — is covered by the same fix.
    """
    # An earlier version bailed out when PYTHONIOENCODING was already set,
    # on the theory that "the operator already chose". It buys nothing and
    # costs a crash: PYTHONIOENCODING=cp1252 is the standard Windows fix for
    # mojibake, and with it set every banner-printing command died with a
    # UnicodeEncodeError instead of degrading to '?'. A stream that already
    # encodes the art is skipped by _can_encode below, so an operator who
    # chose a wide codec is unaffected either way.
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or _can_encode(stream):
            continue
        if not _reconfigure(stream, errors):
            # A stream without .reconfigure (an old wrapper, a test double):
            # re-wrap its buffer rather than giving up on the glyphs. Note the
            # explicit try: a stream whose .buffer *raises* must not take the
            # process down, and getattr's default only catches AttributeError.
            try:
                buffer = getattr(stream, "buffer", None)
            except Exception:  # noqa: BLE001 - cosmetics must never crash
                return
            if buffer is not None:
                try:
                    import io

                    setattr(sys, name, io.TextIOWrapper(
                        buffer, encoding=getattr(stream, "encoding", "utf-8"),
                        errors=errors, newline=""))
                except Exception:       # noqa: BLE001 - cosmetics must not crash
                    return


def harden_path(path: str, mode: int = 0o600) -> bool:
    """Restrict a file to its owner. Returns True when that is guaranteed.

    On POSIX this is a plain ``chmod``. On Windows the mode bits are mostly
    decorative — ``os.chmod`` honours only the read-only flag, and real
    privacy comes from ACLs — so the call is best-effort and says so instead
    of pretending the session file is private when it is not.
    """
    try:
        os.chmod(path, mode)
    except OSError as exc:
        log.debug("could not tighten %s: %s", path, exc)
        return False
    if IS_WINDOWS:
        log.warning(
            "%s is not private on Windows: the OS ignores 0600 and governs "
            "access with ACLs. Keep it out of shared folders.", path)
        return False
    return True


def windows_long_path(path: str) -> str:
    """Prefix a path so Windows APIs look past the 260-character MAX_PATH.

    Only needed for absolute paths that are already deep; relative paths (the
    default ``data/…`` layout) never hit the limit.
    """
    if not IS_WINDOWS or not os.path.isabs(path):
        return path
    if path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\" + path


def default_chrome_profile() -> str:
    """The persistent Chrome profile, in the platform's own spelling."""
    return os.path.join("data", "chrome-profile")


__all__ = ["IS_WINDOWS", "ensure_console_encoding", "harden_path",
           "windows_long_path", "default_chrome_profile"]
