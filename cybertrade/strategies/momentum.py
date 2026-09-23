"""Momentum strategies (join acceleration early)."""

from __future__ import annotations

from typing import Optional

from ..constants import MarketRegime, Side
from ..data.models import Signal
from ..indicators import awesome_oscillator, last_defined, roc, rsi
from ..indicators.core import hma, momentum as momentum_line, sma
from ..indicators.momentum import accel_decel_oscillator, elder_ray
from ..utils.mathx import clamp, nz
from .base import Strategy, StrategyContext


class ROCLaunch(Strategy):
    """Rate-of-change ignition with trend filter."""

    name = "roc_launch"
    label = "ROC Ignition"
    family = "momentum"
    min_bars = 40

    def __init__(self, period: int = 9, threshold: float = 0.05, **kw) -> None:
        self.period = period
        self.threshold = threshold
        super().__init__(period=period, threshold=threshold, **kw)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        r = roc(ctx.closes, self.period)
        if r[-1] is None or r[-2] is None:
            return None
        if nz(r[-2]) == 0:
            return None
        # ignition: ROC crosses the threshold from below/above
        if nz(r[-2]) < self.threshold <= nz(r[-1]):
            return self._signal(ctx, Side.CALL, 0.6, f"roc up {r[-1]:.2f}%")
        if nz(r[-2]) > -self.threshold >= nz(r[-1]):
            return self._signal(ctx, Side.PUT, 0.6, f"roc down {r[-1]:.2f}%")
        return None


class MomentumBurst(Strategy):
    """Two-bar momentum acceleration (the classic binary burst entry)."""

    name = "momentum_burst"
    label = "Two-Bar Momentum Burst"
    family = "momentum"
    min_bars = 25

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        m = momentum_line(ctx.closes, 2)
        if m[-1] is None or m[-2] is None or m[-3] is None:
            return None
        acc = nz(m[-1]) - nz(m[-2])
        prev_acc = nz(m[-2]) - nz(m[-3])
        scale = max(ctx.last_price * 0.0005, 1e-12)
        if acc > scale and prev_acc > 0 and nz(m[-1]) > 0:
            conf = clamp(0.55 + acc / scale * 0.02, 0.5, 0.8)
            return self._signal(ctx, Side.CALL, conf, "bull momentum burst")
        if acc < -scale and prev_acc < 0 and nz(m[-1]) < 0:
            conf = clamp(0.55 + abs(acc) / scale * 0.02, 0.5, 0.8)
            return self._signal(ctx, Side.PUT, conf, "bear momentum burst")
        return None


class AOFlip(Strategy):
    """Awesome oscillator zero-line/zero-cross flips."""

    name = "ao_flip"
    label = "Awesome Oscillator Flip"
    family = "momentum"
    min_bars = 45

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        ao = awesome_oscillator(ctx.highs, ctx.lows)
        if ao[-1] is None or ao[-2] is None:
            return None
        if nz(ao[-2]) <= 0 < nz(ao[-1]):
            return self._signal(ctx, Side.CALL, 0.58, "ao bull flip")
        if nz(ao[-2]) >= 0 > nz(ao[-1]):
            return self._signal(ctx, Side.PUT, 0.58, "ao bear flip")
        return None


class ACShove(Strategy):
    """Accelerator decelerator color shove."""

    name = "ac_shove"
    label = "Accelerator Shove"
    family = "momentum"
    min_bars = 50

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        ac, ao = accel_decel_oscillator(ctx.highs, ctx.lows)
        if ac[-1] is None or ac[-2] is None:
            return None
        if nz(ac[-2]) <= 0 < nz(ac[-1]) and nz(ao[-1]) > 0:
            return self._signal(ctx, Side.CALL, 0.57, "ac shove up")
        if nz(ac[-2]) >= 0 > nz(ac[-1]) and nz(ao[-1]) < 0:
            return self._signal(ctx, Side.PUT, 0.57, "ac shove down")
        return None


class ElderImpulse(Strategy):
    """Elder Ray bull/bear power confirmation with HMA slope."""

    name = "elder_impulse"
    label = "Elder Ray Impulse"
    family = "momentum"
    min_bars = 45

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        bull, bear, ema_line = elder_ray(ctx.highs, ctx.lows, ctx.closes)
        slope = hma(ctx.closes, 20)
        if bull[-1] is None or slope[-1] is None or slope[-2] is None:
            return None
        if nz(bear[-1]) > 0 and nz(slope[-1]) > nz(slope[-2]):
            return self._signal(ctx, Side.CALL, 0.6, "bull power dominance")
        if nz(bull[-1]) < 0 and nz(slope[-1]) < nz(slope[-2]):
            return self._signal(ctx, Side.PUT, 0.6, "bear power dominance")
        return None


class RSIMomentum(Strategy):
    """RSI leaving neutral with room to run (trend-friendly RSI)."""

    name = "rsi_momentum"
    label = "RSI Momentum Exit"
    family = "momentum"
    min_bars = 30

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        r = rsi(ctx.closes, 14)
        if r[-1] is None or r[-2] is None:
            return None
        if nz(r[-2]) <= 55 < nz(r[-1]) and nz(r[-1]) < 78:
            return self._signal(ctx, Side.CALL, 0.55, "rsi bull push")
        if nz(r[-2]) >= 45 > nz(r[-1]) and nz(r[-1]) > 22:
            return self._signal(ctx, Side.PUT, 0.55, "rsi bear push")
        return None


__all__ = [
    "ROCLaunch",
    "MomentumBurst",
    "AOFlip",
    "ACShove",
    "ElderImpulse",
    "RSIMomentum",
]
