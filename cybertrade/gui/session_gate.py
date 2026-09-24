"""Pre-flight session gate: get a Quotex session before the terminal opens.

The engine refuses to boot without venue candles, and venue candles need a
session — so on a first run (or an expired cookie) the desktop terminal used
to die with ``no Quotex session — run cybertrade quotex login`` and send the
operator to a shell. That is a dead end for the one person who most needs the
button: someone who has never logged in.

This window is the way in. It offers exactly one useful action — Chrome
pairing — and hands the captured cookie to a callback that builds the engine
and opens the real terminal. Nothing trades here, and nothing is simulated:
no engine exists until a real session does.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from .pairing import (
    DEFAULT_CDP_PORT,
    DEFAULT_PROFILE,
    DEFAULT_TIMEOUT,
    pairing_form,
    run_pairing,
    session_status,
)
from .theme import MONO, MONO_BOLD, MONO_SMALL, Theme
from .widgets import NeonButton

# glyphs, kept as names so the source stays ASCII-clean
BANNER_TOP = "\u2554" + "\u2550" * 46 + "\u2557"
BANNER_BOT = "\u255a" + "\u2550" * 46 + "\u255d"
WARN = "\u26a0"
FLAG = "\u2691"
ELLIPSIS = "\u2026"


class SessionGate(tk.Tk):
    """The pairing window shown when no venue session exists yet."""

    def __init__(
        self,
        config,
        theme: Optional[Theme] = None,
        on_ready: Optional[Callable[[str, bool], None]] = None,
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.theme = theme or Theme(getattr(config.display, "theme", "neon_abyss"))
        self.on_ready = on_ready
        self.on_cancel = on_cancel
        self._pairing = False

        t = self.theme
        self.title("CYBERTRADE — venue session required")
        self.configure(bg=t["bg"])
        self.minsize(660, 460)
        self.resizable(False, False)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        # a purse already resolved from --demo/--real or the config is not a
        # guess, so carry it into the window instead of asking again
        chosen = getattr(config.broker, "demo_account", None)
        if chosen is not None:
            self.purse.set("practice" if chosen else "real")
        self._refresh_status()

    # -- layout ------------------------------------------------------------
    def _build(self) -> None:
        t = self.theme
        pad = tk.Frame(self, bg=t["bg"])
        pad.pack(fill="both", expand=True, padx=22, pady=18)

        tk.Label(pad, text=BANNER_TOP, bg=t["bg"], fg=t["cyan"],
                 font=MONO_SMALL).pack(anchor="w")
        tk.Label(pad, text="  CYBERTRADE // NEON PROTOCOL — VENUE SESSION",
                 bg=t["bg"], fg=t["cyan"], font=MONO_BOLD).pack(anchor="w")
        tk.Label(pad, text=BANNER_BOT, bg=t["bg"], fg=t["cyan"],
                 font=MONO_SMALL).pack(anchor="w")

        tk.Label(
            pad,
            text="This build trades LIVE at Quotex and needs a venue session\n"
                 "before anything can boot. Pair one with Chrome: sign in and\n"
                 "solve the CAPTCHA yourself — we only read the sessionid\n"
                 "cookie Chrome hands back over localhost DevTools.",
            bg=t["bg"], fg=t["text"], font=MONO_SMALL, justify="left",
        ).pack(anchor="w", pady=(14, 6))

        self.status = tk.Label(pad, text="checking…", bg=t["bg"], fg=t["dim"],
                               font=MONO_SMALL, anchor="w", justify="left")
        self.status.pack(fill="x", pady=(0, 12))

        form = tk.Frame(pad, bg=t["bg"])
        form.pack(fill="x")

        # -- purse: never defaulted ----------------------------------------
        self.purse = tk.StringVar(value="")
        tk.Label(form, text="WHICH PURSE?", bg=t["bg"], fg=t["dim"],
                 font=MONO_SMALL).grid(row=0, column=0, sticky="w", pady=(4, 2))
        tk.Radiobutton(form, text="PRACTICE BALANCE — real orders, demo money",
                       variable=self.purse, value="practice", bg=t["bg"],
                       fg=t["cyan"], selectcolor=t["bg2"],
                       activebackground=t["bg"], font=MONO,
                       ).grid(row=1, column=0, columnspan=2, sticky="w")
        tk.Radiobutton(form, text="REAL MONEY — real orders, real balance",
                       variable=self.purse, value="real", bg=t["bg"],
                       fg=t["red"], selectcolor=t["bg2"],
                       activebackground=t["bg"], font=MONO,
                       ).grid(row=2, column=0, columnspan=2, sticky="w")

        # -- chrome pairing knobs ------------------------------------------
        tk.Label(form, text="CHROME PAIRING", bg=t["bg"], fg=t["yellow"],
                 font=MONO_SMALL).grid(row=3, column=0, sticky="w", pady=(16, 2))
        self.profile_var = tk.StringVar(value=DEFAULT_PROFILE)
        self.port_var = tk.StringVar(value=str(DEFAULT_CDP_PORT))
        self.timeout_var = tk.StringVar(value=str(int(DEFAULT_TIMEOUT)))
        for row, (label, var, width) in enumerate((
                ("profile dir", self.profile_var, 26),
                ("cdp port", self.port_var, 8),
                ("wait seconds", self.timeout_var, 8)), start=4):
            tk.Label(form, text=label, bg=t["bg"], fg=t["dim"],
                     font=MONO_SMALL).grid(row=row, column=0, sticky="w", pady=3)
            tk.Entry(form, textvariable=var, width=width, font=MONO,
                     bg=t["bg2"], fg=t["text"],
                     insertbackground=t["cyan"]).grid(row=row, column=1,
                                                      sticky="w", pady=3)

        buttons = tk.Frame(pad, bg=t["bg"])
        buttons.pack(fill="x", pady=(18, 6))
        self.login_btn = NeonButton(buttons, self.theme, FLAG + " CHROME LOGIN",
                                    color=t["yellow"], command=self._login,
                                    width=210, height=40)
        self.login_btn.pack(side="left", padx=(0, 12))
        self.cancel_btn = NeonButton(buttons, self.theme, "✕ QUIT",
                                     color=t["red"], command=self._cancel,
                                     width=110, height=40)
        self.cancel_btn.pack(side="left")

        tk.Label(
            pad,
            text=WARN + " Unofficial integration. Automation may violate Quotex's\n"
                 "Terms of Service. LIVE ONLY build — every order is real.\n"
                 "See DISCLAIMER.md.",
            bg=t["bg"], fg=t["yellow"], font=MONO_SMALL, justify="left",
        ).pack(anchor="w", pady=(10, 0))

    # -- actions ------------------------------------------------------------
    def _purse(self) -> Optional[bool]:
        purse = self.purse.get()
        if purse not in ("practice", "real"):
            self._say("pick a purse: PRACTICE or REAL MONEY", "yellow")
            return None
        return purse == "practice"

    def _login(self) -> None:
        if self._pairing:
            self._say("a Chrome pairing is already running", "yellow")
            return
        purse = self._purse()
        if purse is None:
            return
        ok, error, values = pairing_form(
            profile=self.profile_var.get(),
            port=self.port_var.get(),
            timeout=self.timeout_var.get(),
        )
        if not ok:
            self._say(error, "yellow")
            return
        self._set_busy(True)
        self._say("launching Chrome — log in and solve the CAPTCHA…", "yellow")
        run_pairing(
            session_path=self.config.qx_session_path,
            profile=values["profile"],
            port=values["port"],
            timeout=values["timeout"],
            on_done=lambda sess: self.after(
                0, lambda: self._done(sess, purse)),
            on_error=lambda exc: self.after(
                0, lambda: self._failed(exc)),
        )

    def _done(self, sess: dict, purse: bool) -> None:
        self._set_busy(False)
        ssid = str(sess.get("ssid", ""))
        if not ssid:
            self._say("Chrome closed without a sessionid cookie — try again",
                      "red")
            return
        self._say("session captured — starting the terminal…", "green")
        if self.on_ready is not None:
            self.on_ready(ssid, purse)

    def _failed(self, exc: Exception) -> None:
        self._set_busy(False)
        reason = str(exc) or exc.__class__.__name__
        self._say(f"pairing failed: {reason}", "red")

    def _cancel(self) -> None:
        if self.on_cancel is not None:
            self.on_cancel()
        self.destroy()

    # -- views --------------------------------------------------------------
    def _say(self, text: str, color: str = "dim") -> None:
        self.status.config(text=text, fg=self.theme[color])

    def _set_busy(self, busy: bool) -> None:
        self._pairing = busy
        self.login_btn.set_label(ELLIPSIS + " PAIRING" if busy
                                 else FLAG + " CHROME LOGIN")
        self.login_btn.set_color(self.theme["dim"] if busy else self.theme["yellow"])
        self.cancel_btn.set_color(self.theme["red"])

    def _refresh_status(self) -> None:
        path = getattr(self.config, "qx_session_path", "")
        if session_status(path) == "no session yet":
            self._say("no venue session yet — pair one below", "yellow")
        else:
            self._say("a saved session exists, but the terminal could not use "
                      "it — pair again below", "yellow")


def run_gate(config, on_ready, on_cancel=None) -> None:
    """Open the pre-flight pairing window and block until it closes."""
    gate = SessionGate(config, on_ready=on_ready, on_cancel=on_cancel)
    gate.mainloop()


__all__ = ["SessionGate", "run_gate"]
