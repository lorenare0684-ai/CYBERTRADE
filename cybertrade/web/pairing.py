"""Chrome-assisted Quotex pairing for the browser terminal.

The desktop GUI pairs from a Tk window. The browser terminal pairs from an
HTTP endpoint, but the hard part is identical and already solved in
:mod:`cybertrade.gui.pairing` — a flow that is free of any toolkit, so it
can be driven from a request handler just as easily as from a widget.

This module owns the *state machine* around that flow, because HTTP is
stateless and pairing is not: a human takes minutes to log in and solve a
CAPTCHA, across several requests. The states are therefore visible to the
browser through ``/api/pair/status`` so it can show what is actually
happening instead of a spinner that might be lying.

Two entry points matter:

- **before an engine exists** — ``cmd_web`` cannot build an engine without
  venue candles, which need a session. So it starts the terminal with no
  engine, serves a pairing screen, and attaches the engine when a cookie
  lands.
- **after an engine exists** — a session that dies mid-run gets re-paired
  in place: ``set_ssid`` + ``connect`` on the live api, no restart.

Nothing here bypasses the CAPTCHA: a human solves it in a real Chrome
profile, and this only reads the session (cookie or page token) over
localhost DevTools afterwards.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from ..config import AppConfig
from ..gui.pairing import pairing_form, run_pairing, session_status

log = logging.getLogger("cybertrade.web.pairing")

IDLE = "idle"
LAUNCHING = "launching"
WAITING = "waiting"
ADOPTING = "adopting"
READY = "ready"
FAILED = "failed"

STATES = (IDLE, LAUNCHING, WAITING, ADOPTING, READY, FAILED)


class PairingController:
    """Drives one pairing attempt at a time for the browser terminal.

    ``on_ready(ssid, purse, cookies)`` is called exactly once per
    successful pairing, on the worker thread. The caller decides what
    "ready" means — build an engine, or re-seat an existing one.
    """

    def __init__(self, config: AppConfig,
                 on_ready: Callable[[str, bool, str], None],
                 probe: Optional[Callable[[], bool]] = None,
                 adopted: Optional[Callable[[], bool]] = None) -> None:
        self.config = config
        self.on_ready = on_ready
        # "has the terminal picked the captured session up yet?" -- while
        # an engine is still booting on it, a second pairing would race the
        # first for the state lock.  Defaults to the probe.
        self.adopted = adopted
        # "is a working session in hand right now?" -- the saved file is only
        # half the story: --ssid and QX_SSID never touch the disk, and a
        # running engine may hold a cookie no file describes.
        self.probe = probe
        self._lock = threading.RLock()
        self._state = IDLE
        self._message = session_status(getattr(config, "qx_session_path", ""))
        self._error = ""
        self._ssid = ""
        self._purse: Optional[bool] = None
        self._thread: Optional[threading.Thread] = None
        self._started = 0.0
        self._profile = ""
        self._port = 9333        # DevTools port of the current/past attempt
        self._epoch = 0          # bumped on start/cancel: stale results die
        self._ready_at = 0.0     # when the last cookie landed (adoption guard)

    # -- queries ------------------------------------------------------------
    def state(self) -> str:
        with self._lock:
            return self._state

    def busy(self) -> bool:
        with self._lock:
            return self._state in (LAUNCHING, WAITING)

    def _idle_message(self) -> str:
        """What "nothing is happening" looks like, honestly.

        A terminal that already holds a session must not claim it has none --
        that is the message an operator reads when they open RE-PAIR VENUE on
        a running engine.
        """
        if self._state == IDLE and self._active():
            return ("a venue session is live — re-pair only if it dies")
        return session_status(getattr(self.config, "qx_session_path", ""))

    def status(self) -> Dict[str, Any]:
        with self._lock:
            if self._state == IDLE and not self._message.startswith("pairing"):
                self._message = self._idle_message()
            live = self._active()
            # The saved-file status is not the whole truth: --ssid and
            # QX_SSID never touch disk, so a running engine would report
            # "no session yet" out of the same payload that says the
            # session is live. Both keys must agree.
            saved = session_status(
                getattr(self.config, "qx_session_path", ""))
            # READY means the cookie landed — but the terminal is only live
            # once the engine holds it.  Reporting "ready" during the
            # minute-long engine boot makes the veil flap open/closed, so a
            # captured-but-unadopted session reports "adopting" instead.
            # (Without a probe there is no engine to wait for: ready as ever.)
            reported = self._state
            message = self._message
            if self._state == READY and self.probe is not None and not live:
                reported = ADOPTING
                message = ("session captured — starting the terminal "
                           "(first boot pulls history, up to a minute)…")
            out = {
                "state": reported,
                "busy": (self._state in (LAUNCHING, WAITING) or reported == ADOPTING),
                "message": message,
                "error": self._error,
                "ssid_present": bool(self._ssid),
                "purse": self._purse,
                "profile": self._profile,
                "elapsed": round(time.time() - self._started, 1)
                if self._started else 0.0,
                "session": ("venue session live (held in memory)"
                            if live else saved),
                "saved": saved,
                "session_path": getattr(self.config, "qx_session_path", ""),
            }
        out["active"] = live
        return out

    ADOPT_GRACE = 180.0  # seconds a captured session may take to go live

    def _adopting_locked(self) -> bool:
        """True while a captured session is still booting the engine.

        Caller holds the lock.  A boot that never goes live must not wedge
        the button forever, so the guard lapses after ``ADOPT_GRACE``.
        """
        if self._state != READY or (self.probe is None and self.adopted is None):
            return False
        check = self.adopted or self._active
        try:
            if check():
                return False
        except Exception:  # noqa: BLE001 - a probe must never block pairing
            return False
        return (time.time() - self._ready_at) < self.ADOPT_GRACE

    def _active(self) -> bool:
        if self.probe is not None:
            try:
                return bool(self.probe())
            except Exception:  # noqa: BLE001 - a probe must never break status
                return False
        return False

    # -- actions ------------------------------------------------------------
    def start(self, profile: str, port: Any, timeout: Any,
              purse: Any) -> Dict[str, Any]:
        """Launch Chrome and wait for a cookie. Never blocks the caller."""
        if not isinstance(purse, bool):
            return {"ok": False, "error":
                    "pick a purse: PRACTICE or REAL MONEY"}
        ok, error, values = pairing_form(profile=profile, port=port,
                                         timeout=timeout)
        if not ok:
            return {"ok": False, "error": error}
        with self._lock:
            if self._state in (LAUNCHING, WAITING):
                return {"ok": False, "error":
                        "a pairing is already running — finish it or cancel"}
            if self._adopting_locked():
                return {"ok": False, "error":
                        "the terminal is still starting on the last session "
                        "(history warm-up, up to a minute) — wait for it; a "
                        "second boot would fight the first over the state lock"}
            self._message = ("launching Chrome — log in and solve the "
                             "CAPTCHA in the window that opens")
            self._error = ""
            self._ssid = ""
            self._purse = purse
            self._profile = values["profile"]
            self._port = values["port"]
            self._started = time.time()
            self._epoch += 1
            epoch = self._epoch
            # WAITING *before* the launch, so a worker that finishes first
            # (a synchronous launcher, an instant failure) is never clobbered
            # by this method's own bookkeeping.
            self._state = WAITING

        log.warning("chrome pairing started profile=%s port=%s purse=%s",
                    values["profile"], values["port"], purse)

        def _progress(info: Dict[str, Any]) -> None:
            """Live DevTools progress for the pairing veil (worker thread)."""
            with self._lock:
                if self._state not in (LAUNCHING, WAITING):
                    return
                names = sorted(info.get("cookies") or [])
                if names:
                    self._message = (
                        "log in and solve the CAPTCHA in the Chrome window "
                        f"(cookies so far: {', '.join(names)})")

        self._thread = run_pairing(
            session_path=self.config.qx_session_path,
            profile=values["profile"],
            port=values["port"],
            timeout=values["timeout"],
            on_done=lambda sess: self._done(sess, epoch),
            on_error=lambda exc: self._failed(exc, epoch),
            on_poll=_progress,
        )
        return {"ok": True, **self.status()}

    def cancel(self) -> Dict[str, Any]:
        """Give up on the current attempt; the worker thread is a daemon."""
        with self._lock:
            if self._state not in (LAUNCHING, WAITING):
                return {"ok": False, "error": "no pairing is running"}
            self._state = IDLE
            self._message = "pairing cancelled"
            self._thread = None
            self._epoch += 1        # a cookie landing after this is ignored
        log.warning("chrome pairing cancelled by the operator")
        return {"ok": True, **self.status()}

    def note_error(self, message: str) -> Dict[str, Any]:
        """Fail the attempt from the adopting thread (engine refused it).

        The cookie landed but the terminal could not start on it — the veil
        must say so with a way back (Start again), not spin forever.
        """
        with self._lock:
            self._state = FAILED
            self._error = message
            self._message = f"pairing failed: {message}"
            self._thread = None
            self._epoch += 1
        log.warning("pairing adopted badly: %s", message)
        return {"ok": False, **self.status()}

    # -- worker callbacks (on the worker thread) ----------------------------
    def _live(self, epoch: int) -> bool:
        """False once the attempt was cancelled or superseded by a new one.

        The epoch alone decides. Gating on the state as well looks harmless but
        is not: after a *successful* pairing the state is READY, and the next
        legitimate attempt would then be rejected as stale — which is exactly
        the re-pair path that keeps a live terminal alive.
        """
        with self._lock:
            return epoch == self._epoch

    def _done(self, sess: Dict[str, str], epoch: int) -> None:
        ssid = str(sess.get("ssid", "")) if sess else ""
        if not self._live(epoch):
            return
        if not ssid:
            with self._lock:
                self._state = FAILED
                self._message = ("Chrome closed without a session cookie — "
                                 "start again")
                self._error = "no session cookie seen"
            return
        with self._lock:
            purse = self._purse
        try:
            self.on_ready(ssid, purse, str(sess.get("cookies", "")))
        except Exception as exc:  # noqa: BLE001 — report, never crash a thread
            log.exception("pairing succeeded but the engine refused it")
            with self._lock:
                self._state = FAILED
                self._error = str(exc)
                self._message = f"session captured, but the terminal could not use it: {exc}"
            return
        with self._lock:
            self._state = READY
            self._ready_at = time.time()
            self._ssid = ssid
            self._message = "session captured — the terminal is live"

    def _failed(self, exc: Exception, epoch: int) -> None:
        if not self._live(epoch):
            return
        with self._lock:
            self._state = FAILED
            self._error = str(exc) or exc.__class__.__name__
            self._message = f"pairing failed: {self._error}"


__all__ = ["PairingController", "STATES", "IDLE", "LAUNCHING", "WAITING",
           "ADOPTING", "READY", "FAILED"]
