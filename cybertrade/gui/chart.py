"""Canvas candlestick chart with indicator overlays for the desktop GUI."""

from __future__ import annotations

import tkinter as tk
from typing import List, Optional, Sequence, Tuple

from ..indicators import bollinger_bands, ema
from .theme import MONO_SMALL, Theme, dim, draw_grid, glow


class CandleChart(tk.Canvas):
    """Neon candlestick renderer: candles + EMA9/21 + Bollinger + last price."""

    def __init__(self, master, theme: Theme, **kw) -> None:
        super().__init__(master, bg="#03040a", highlightthickness=0, **kw)
        self.theme = theme
        self.candles: List[dict] = []
        self.asset = ""
        self.show_bands = True
        self.bind("<Configure>", lambda e: self.render())

    def set_data(self, candles: Sequence[dict], asset: str = "") -> None:
        self.candles = list(candles)
        if asset:
            self.asset = asset
        self.render()

    def render(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40 or h < 40:
            return
        t = self.theme
        draw_grid(self, w, h, dim(t["cyan"], 0.85), step=32)
        if not self.candles:
            self.create_text(w / 2, h / 2, text="◈ AWAITING MARKET DATA",
                             fill=t["dim"], font=MONO_SMALL)
            return

        pad_r, pad_l, pad_t, pad_b = 70, 8, 18, 16
        closes = [c["c"] for c in self.candles]
        lo = min(c["l"] for c in self.candles)
        hi = max(c["h"] for c in self.candles)
        margin = (hi - lo) * 0.08 or 1e-6
        lo, hi = lo - margin, hi + margin

        def x_of(i: int) -> float:
            step = (w - pad_l - pad_r) / max(len(self.candles), 1)
            return pad_l + i * step + step / 2

        def y_of(price: float) -> float:
            return pad_t + (hi - price) / (hi - lo) * (h - pad_t - pad_b)

        # price axis
        for k in range(6):
            price = hi - (hi - lo) * k / 5
            y = y_of(price)
            self.create_text(w - pad_r + 6, y, anchor="w",
                             text=f"{price:.5f}", fill=t["dim"], font=MONO_SMALL)

        if self.show_bands:
            up, mid, low, _ = bollinger_bands(closes, 20, 2.0)
            for series, color in ((up, dim(t["cyan"], 0.35)), (mid, dim("#ffffff", 0.55)),
                                  (low, dim(t["cyan"], 0.35))):
                self._polyline(series, x_of, y_of, color, 1)

        e9 = ema(closes, 9)
        e21 = ema(closes, 21)
        self._polyline(e9, x_of, y_of, t["cyan"], 2)
        self._polyline(e21, x_of, y_of, t["magenta"], 2)

        # candles
        n = len(self.candles)
        step = (w - pad_l - pad_r) / max(n, 1)
        bw = max(2, min(14, step * 0.6))
        for i, c in enumerate(self.candles):
            x = x_of(i)
            up_candle = c["c"] >= c["o"]
            color = t["green"] if up_candle else t["red"]
            self.create_line(x, y_of(c["h"]), x, y_of(c["l"]), fill=color)
            y0, y1 = y_of(c["o"]), y_of(c["c"])
            self.create_rectangle(x - bw / 2, min(y0, y1), x + bw / 2, max(y0, y1),
                                  fill=color, outline=color)

        # last price marker
        last = self.candles[-1]["c"]
        y = y_of(last)
        self.create_line(0, y, w - pad_r, y, fill=t["yellow"], dash=(5, 3))
        self.create_text(w - pad_r + 6, y, anchor="w", text=f"{last:.5f}",
                         fill=glow(t["yellow"], 0.3), font=MONO_SMALL)
        if self.asset:
            self.create_text(8, 8, anchor="w",
                             text=f"◈ {self.asset}",
                             fill=glow(t["cyan"], 0.3), font=MONO_SMALL)

    def _polyline(self, values: Sequence[Optional[float]], x_of, y_of, color: str, width: int) -> None:
        points: List[float] = []
        started = False
        for i, v in enumerate(values):
            if v is None:
                continue
            points += [x_of(i), y_of(v)]
            started = True
        if started and len(points) >= 4:
            self.create_line(*points, fill=color, width=width, smooth=True)


__all__ = ["CandleChart"]
