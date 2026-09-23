"""Custom Tkinter widgets with neon cyberpunk chrome."""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .theme import (
    BIG_NUM,
    DISPLAY_SM,
    MONO,
    MONO_SMALL,
    Theme,
    dim,
    draw_grid,
    draw_scanlines,
    glow,
)


class NeonPanel(tk.Canvas):
    """A bordered container with corner brackets and a title strip."""

    def __init__(self, master, theme: Theme, title: str = "", **kw) -> None:
        super().__init__(
            master,
            bg=theme["bg2"],
            highlightthickness=1,
            highlightbackground=dim(theme["cyan"], 0.4),
            **kw,
        )
        self.theme = theme
        self.panel_title = title
        self.bind("<Configure>", lambda e: self._redraw())
        self._redraw()

    def _redraw(self) -> None:
        self.delete("chrome")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        t = self.theme
        c = dim(t["cyan"], 0.35)
        m = t["magenta"]
        # corner brackets
        s = 14
        for x0, y0, dx, dy in ((0, 0, 1, 1), (w, 0, -1, 1), (0, h, 1, -1), (w, h, -1, -1)):
            self.create_line(x0, y0, x0 + dx * s, y0, fill=m, tags="chrome", width=2)
            self.create_line(x0, y0, x0, y0 + dy * s, fill=m, tags="chrome", width=2)
        if self.panel_title:
            self.create_text(
                10, 12, anchor="w", text=f"◈ {self.panel_title}",
                fill=glow(t["cyan"], 0.3), font=DISPLAY_SM, tags="chrome",
            )
        self.create_line(0, 26, w, 26, fill=dim(t["cyan"], 0.55), tags="chrome")


class NeonButton(tk.Canvas):
    """A push-button drawn entirely on canvas (border glow + label)."""

    def __init__(
        self,
        master,
        theme: Theme,
        text: str,
        command: Optional[Callable[[], None]] = None,
        color: Optional[str] = None,
        width: int = 130,
        height: int = 34,
        **kw,
    ) -> None:
        super().__init__(
            master, width=width, height=height, bg=theme["bg2"],
            highlightthickness=0, cursor="hand2", **kw,
        )
        self.theme = theme
        self.label = text
        self.command = command
        self.color = color or theme["cyan"]
        self.width = width
        self.height = height
        self._armed = False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw(hover=False))
        self._draw()

    def _draw(self, hover: bool = False) -> None:
        self.delete("all")
        w, h = self.width, self.height
        fill = self.color if hover else dim(self.color, 0.65)
        outline = glow(self.color, 0.6) if hover else self.color
        self.create_rectangle(2, 2, w - 2, h - 2, outline=outline, fill=dim(self.color, 0.82), width=2)
        self.create_text(
            w / 2, h / 2, text=self.label, fill=fill,
            font=MONO, justify="center",
        )

    def _click(self, _event=None) -> None:
        if self.command:
            self.command()

    def resize(self, width: int, height: int) -> None:
        """Phase-31: re-fit the button to the layout plan."""
        self.width = max(48, int(width))
        self.height = max(22, int(height))
        self.config(width=self.width, height=self.height)
        self._draw()

    def set_label(self, text: str) -> None:
        self.label = text
        self._draw()

    def set_color(self, color: str) -> None:
        self.color = color
        self._draw()


class Meter(tk.Canvas):
    """Horizontal limit meter with neon fill and a label/value pair."""

    def __init__(self, master, theme: Theme, label: str, color: str = "cyan", width: int = 280,
                 height: int = 34) -> None:
        super().__init__(master, width=width, height=height, bg=theme["bg2"],
                         highlightthickness=0, **kw_if())
        self.theme = theme
        self.label = label
        self.color = color
        self.width = width
        self.height = height
        self._value = 0.0
        self._max = 1.0
        self._draw()

    def set(self, value: float, maximum: float = 1.0, suffix: str = "") -> None:
        self._value = value
        self._max = maximum if maximum > 0 else 1.0
        self._suffix = suffix
        self._draw()

    def resize(self, width: int) -> None:
        """Phase-31: plan-driven width (label track scales with it)."""
        self.width = max(140, int(width))
        self.config(width=self.width)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        t = self.theme
        col = t[self.color] if self.color in t.colors else self.color
        self.create_text(2, self.height / 2, anchor="w", text=self.label.upper(),
                         fill=t["dim"], font=MONO_SMALL)
        x0 = max(64, int(self.width * 0.27))
        x1 = self.width - 52
        y0, y1 = 10, self.height - 10
        self.create_rectangle(x0, y0, x1, y1, outline=dim(t["cyan"], 0.5))
        frac = max(0.0, min(1.0, self._value / self._max))
        if frac > 0:
            self.create_rectangle(
                x0 + 1, y0 + 1, x0 + 1 + (x1 - x0 - 2) * frac, y1 - 1,
                fill=col, outline="",
            )
        suffix = getattr(self, "_suffix", "")
        self.create_text(self.width - 48, self.height / 2, anchor="w",
                         text=f"{self._value:.1f}{suffix or '%'}", fill=col, font=MONO)


def kw_if() -> dict:
    return {}


class StatBox(tk.Frame):
    """Two-line label/value readout."""

    def __init__(self, master, theme: Theme, label: str, color: str = "text", **kw) -> None:
        super().__init__(master, bg=theme["bg2"], **kw)
        self.theme = theme
        self.caption = tk.Label(self, text=label.upper(), bg=theme["bg2"],
                                fg=theme["dim"], font=MONO_SMALL)
        self.caption.pack(anchor="w")
        self.value = tk.Label(self, text="—", bg=theme["bg2"],
                              fg=theme[color], font=BIG_NUM)
        self.value.pack(anchor="w")

    def set(self, text: str, color: Optional[str] = None) -> None:
        self.value.config(text=text)
        if color:
            self.value.config(fg=self.theme[color] if color in self.theme.colors else color)


class LogConsole(tk.Frame):
    """Scrolling neon log console."""

    LEVEL_COLORS = {
        "DEBUG": "dim", "INFO": "cyan", "WARNING": "yellow",
        "ERROR": "red", "CRITICAL": "magenta",
    }

    def __init__(self, master, theme: Theme, height: int = 12, **kw) -> None:
        super().__init__(master, bg=theme["bg2"], **kw)
        self.theme = theme
        self.text = tk.Text(
            self, height=height, bg="#03040a", fg=theme["text"],
            insertbackground=theme["cyan"], font=MONO_SMALL,
            relief="flat", state="disabled", wrap="none",
        )
        scroll = tk.Scrollbar(self, command=self.text.yview, width=8)
        self.text.config(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for level, color in self.LEVEL_COLORS.items():
            self.text.tag_config(level, foreground=theme[color])

    def set_height(self, rows: int) -> None:
        """Phase-31: plan-driven console line count."""
        self.text.config(height=max(4, int(rows)))

    def append(self, msg: str, level: str = "INFO") -> None:
        self.text.config(state="normal")
        self.text.insert("end", msg + "\n", level.upper() if level.upper() in self.LEVEL_COLORS else "INFO")
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 800:
            self.text.delete("1.0", f"{lines - 600}.0")
        self.text.see("end")
        self.text.config(state="disabled")


class DataTable(tk.Frame):
    """Monospace themed table for trade blotters."""

    def __init__(self, master, theme: Theme, columns: Sequence[str], height: int = 10, **kw) -> None:
        super().__init__(master, bg=theme["bg2"], **kw)
        self.theme = theme
        self.columns = list(columns)
        self.text = tk.Text(
            self, height=height, bg="#03040a", fg=theme["text"],
            font=MONO_SMALL, relief="flat", state="disabled", wrap="none",
        )
        scroll = tk.Scrollbar(self, command=self.text.yview, width=8)
        self.text.config(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.text.tag_config("head", foreground=theme["magenta"])
        self.text.tag_config("won", foreground=theme["green"])
        self.text.tag_config("lost", foreground=theme["red"])
        self.text.tag_config("refund", foreground=theme["yellow"])
        self._header = " | ".join(c[:10].ljust(10) for c in self.columns)

    def set_height(self, rows: int) -> None:
        """Phase-31: plan-driven visible row count."""
        self.text.config(height=max(4, int(rows)))

    def rows(self, data: List[Tuple[str, ...]], tag_fn: Optional[Callable] = None) -> None:
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", self._header + "\n", "head")
        self.text.insert("end", "─" * (len(self._header) + 4) + "\n", "head")
        for row in data:
            line = " | ".join(str(cell)[:10].ljust(10) for cell in row)
            tag = tag_fn(row) if tag_fn else "info"
            self.text.insert("end", line + "\n", tag)
        self.text.config(state="disabled")


class Gauge(tk.Canvas):
    """Semi-circular dial gauge (drawdown, win rate, stress)."""

    def __init__(self, master, theme: Theme, label: str, color: str = "cyan",
                 size: int = 130, **kw) -> None:
        super().__init__(master, width=size, height=size * 0.7, bg=theme["bg2"],
                         highlightthickness=0, **kw)
        self.theme = theme
        self.label = label
        self.color = color
        self.size = size
        self._frac = 0.0
        self._draw()

    def set(self, frac: float, text: Optional[str] = None) -> None:
        self._frac = max(0.0, min(1.0, frac))
        self._text = text or f"{frac * 100:.0f}%"
        self._draw()

    def resize(self, size: int) -> None:
        """Phase-31: plan-driven dial size."""
        self.size = max(70, int(size))
        self.config(width=self.size, height=int(self.size * 0.7))
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        t = self.theme
        col = t[self.color] if self.color in t.colors else self.color
        s = self.size
        cx, cy, r = s / 2, s * 0.62, s * 0.42
        self.create_arc(cx - r, cy - r, cx + r, cy + r,
                        start=0, extent=180, style="arc",
                        outline=dim(col, 0.7), width=6)
        self.create_arc(cx - r, cy - r, cx + r, cy + r,
                        start=180 - 180 * self._frac, extent=180 * self._frac,
                        style="arc", outline=col, width=6)
        self.create_text(cx, cy - 8, text=getattr(self, "_text", "0%"),
                         fill=glow(col, 0.35), font=MONO_BOLD)
        self.create_text(cx, cy + 12, text=self.label.upper(), fill=t["dim"], font=MONO_SMALL)


__all__ = [
    "NeonPanel",
    "NeonButton",
    "Meter",
    "StatBox",
    "LogConsole",
    "DataTable",
    "Gauge",
]
