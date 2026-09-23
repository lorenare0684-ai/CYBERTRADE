"""Boot sequence animation: the terminal waking up in neon."""

from __future__ import annotations

import tkinter as tk
from typing import List

from .theme import MONO_SMALL, Theme, glow

BOOT_LINES: List[str] = [
    "CYBERTRADE BIOS v1.0 — NEON PROTOCOL",
    "POST ......................... OK",
    "loading indicator cores [48] . OK",
    "spinning strategy matrix [33]  OK",
    "wiring risk governor .......... ARMED",
    "survivor playbook ............. ALL-WEATHER",
    "regime detector ............... ONLINE",
    "paper venue ................... SIM",
    "quotex bridge ................. STANDBY (unofficial)",
    "warning: no trading system survives every market condition",
    "warning: paper mode by default — live mode is your funeral",
    "BOOT COMPLETE — WELCOME, OPERATOR",
]


class BootScreen(tk.Canvas):
    """Full-window canvas that types the boot sequence then calls on_done."""

    def __init__(self, master, theme: Theme, on_done, **kw) -> None:
        super().__init__(master, bg=theme["bg"], highlightthickness=0, **kw)
        self.theme = theme
        self.on_done = on_done
        self._index = 0
        self.bind("<Configure>", lambda e: self._draw())
        self.after(120, self._tick)

    def _tick(self) -> None:
        self._index += 1
        self._draw()
        if self._index >= len(BOOT_LINES):
            self.after(350, self.on_done)
            return
        self.after(70 + (80 if "warning" in BOOT_LINES[self._index] else 0), self._tick)

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        t = self.theme
        self.create_text(
            w / 2, 60, text="◈ CYBERTRADE ◈",
            fill=glow(t["cyan"], 0.4), font=("Impact", 28),
        )
        y = 110
        for i, line in enumerate(BOOT_LINES[: self._index]):
            color = t["yellow"] if "warning" in line else (
                t["green"] if line.endswith("OK") or "COMPLETE" in line else t["text"]
            )
            self.create_text(40, y, anchor="w", text=line, fill=color, font=MONO_SMALL)
            y += 20
        if self._index < len(BOOT_LINES):
            self.create_text(40, y, anchor="w", text="█", fill=t["cyan"], font=MONO_SMALL)


__all__ = ["BootScreen", "BOOT_LINES"]
