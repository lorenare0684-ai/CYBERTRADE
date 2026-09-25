"""Chrome-assisted Quotex pairing for the interactive terminals.

``cybertrade quotex login`` does this on the command line. The desktop and
browser terminals need the same flow, but they cannot block their own UI
thread for the two-odd minutes a human takes to log in and solve a CAPTCHA —
a frozen window looks exactly like a hang.

So the flow lives here, free of any toolkit: :func:`run_pairing` launches the
worker thread and hands the result back through callbacks the caller marshals
onto its own UI thread (:meth:`tkinter.Misc.after` for Tk). :func:`pairing_form`
validates the operator's form input the same way the CLI validates its flags.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger("cybertrade.gui.pairing")

# Same defaults `cybertrade quotex login` uses — one source of truth for the
# numbers the operator sees in either place.
DEFAULT_PROFILE = "data/chrome-profile"
DEFAULT_CDP_PORT = 9333
DEFAULT_TIMEOUT = 240.0


def pairing_form(
    profile: str = "",
    port: Any = DEFAULT_CDP_PORT,
    timeout: Any = DEFAULT_TIMEOUT,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate a pairing form; returns ``(ok, error, values)``.

    Bad input is the operator's problem to fix, never a crash: a typo in the
    port or the wait budget comes back as a message they can act on.
    """
    values: Dict[str, Any] = {
        "profile": (profile or "").strip() or DEFAULT_PROFILE,
    }
    try:
        port_no = int(str(port).strip() or DEFAULT_CDP_PORT)
    except (TypeError, ValueError):
        return False, "cdp port must be a whole number", values
    if not 1 <= port_no <= 65535:
        return False, "cdp port must be between 1 and 65535", values
    values["port"] = port_no
    try:
        wait = float(str(timeout).strip() or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return False, "wait seconds must be a number", values
    if wait <= 0:
        return False, "wait seconds must be greater than zero", values
    values["timeout"] = wait
    return True, "", values


def run_pairing(
    session_path: str,
    profile: str,
    port: int,
    timeout: float,
    on_done: Callable[[Dict[str, str]], None],
    on_error: Callable[[Exception], None],
    pair: Optional[Callable[..., Dict[str, str]]] = None,
    chrome: str = "",
    on_poll: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> threading.Thread:
    """Pair on a worker thread; report through ``on_done`` / ``on_error``.

    Both callbacks fire **on the worker thread** — a Tk caller must wrap them
    in ``widget.after(0, ...)`` before touching any widget. The thread is a
    daemon so a closed window never waits on a human finishing a CAPTCHA.
    ``on_poll`` (also worker-thread) fires each DevTools round with
    ``{waited, cookies, target, token_seen}`` for live progress.
    """
    pair = pair or _pair_session

    def work() -> None:
        try:
            sess = pair(
                session_path=session_path,
                profile_dir=profile,
                port=port,
                timeout=timeout,
                chrome=chrome,
                on_poll=on_poll,
            )
        except Exception as exc:  # noqa: BLE001 — every failure is reportable
            log.warning("chrome pairing failed: %s", exc)
            on_error(exc)
            return
        on_done(sess or {})

    thread = threading.Thread(target=work, name="quotex-pairing", daemon=True)
    thread.start()
    return thread


def _pair_session(**kwargs: Any) -> Dict[str, str]:
    from ..brokers.quotex.pairing import pair_session

    return pair_session(**kwargs)


def session_status(session_path: str) -> str:
    """One-line description of the saved session, for a status label."""
    from ..brokers.quotex.pairing import load_session

    sess = load_session(session_path)
    if not sess.get("ssid"):
        return "no session yet"
    domain = sess.get("domain", "qxbroker.com")
    return f"session saved for {domain}"


def profile_exists(profile: str) -> bool:
    """True when the Chrome profile dir already holds a login."""
    path = os.path.expanduser(profile or DEFAULT_PROFILE)
    return os.path.isdir(path) and bool(os.listdir(path))


__all__ = [
    "DEFAULT_PROFILE", "DEFAULT_CDP_PORT", "DEFAULT_TIMEOUT",
    "pairing_form", "run_pairing", "session_status", "profile_exists",
]
