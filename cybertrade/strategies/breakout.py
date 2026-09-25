"""Breakout strategies (compression release and range escapes)."""

from __future__ import annotations

from typing import Optional

from ..constants import MarketRegime, Side
from ..data.models import Signal
from ..indicators import (
    bandwidth,
    donchian,
    keltner,
    last_defined,
    squeeze_momentum,
)
from ..indicators.core import sma
from ..utils.mathx import clamp, nz
from .base import Strategy, StrategyContext


class DonchianBreak(Strategy):
    """Break above/below the Donchian channel."""

    name = "donchian_break"
    label = "Donchian Breakout"
    family = "breakout"
    min_bars = 35
    preferred_regimes = (MarketRegime.HIGH_VOL, MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)

    def __init__(self, period: int = 20, **kw) -> None:
        self.period = period
        super().__init__(period=period, **kw)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        upper, mid, lower = donchian(ctx.highs, ctx.lows, self.period)
        if upper[-2] is None:
            return None
        if ctx.last_price > nz(upper[-2]) and ctx.closes[-2] <= nz(upper[-2]):
            conf = clamp(0.55 + (ctx.last_price / max(nz(upper[-2]), 1e-12) - 1.0) * 250, 0.5, 0.82)
            return self._signal(ctx, Side.CALL, conf, "donchian upside break")
        if ctx.last_price < nz(lower[-2]) and ctx.closes[-2] >= nz(lower[-2]):
            conf = clamp(0.55 + (1.0 - ctx.last_price / max(nz(lower[-2]), 1e-12)) * 250, 0.5, 0.82)
            return self._signal(ctx, Side.PUT, conf, "donchian downside break")
        return None


class KeltnerRide(Strategy):
    """Close outside the Keltner channel after an EMA stack shift."""

    name = "keltner_ride"
    label = "Keltner Channel Ride"
    family = "breakout"
    min_bars = 40

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        upper, mid, lower = keltner(ctx.highs, ctx.lows, ctx.closes)
        if upper[-1] is None or mid[-1] is None:
            return None
        fast = last_defined(sma(ctx.closes, 8), ctx.last_price) or ctx.last_price
        if ctx.last_price > nz(upper[-1]) and fast > nz(mid[-1]):
            return self._signal(ctx, Side.CALL, 0.6, "keltner up expansion")
        if ctx.last_price < nz(lower[-1]) and fast < nz(mid[-1]):
            return self._signal(ctx, Side.PUT, 0.6, "keltner down expansion")
        return None


class SqueezePop(Strategy):
    """Bollinger-inside-Keltner squeeze resolving with momentum."""

    name = "squeeze_pop"
    label = "Squeeze Pop"
    family = "breakout"
    min_bars = 55
    preferred_regimes = (MarketRegime.HIGH_VOL, MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        squeeze, mom = squeeze_momentum(ctx.highs, ctx.lows, ctx.closes)
        if squeeze[-2] is None or mom[-1] is None:
            return None
        was_on = nz(squeeze[-3], 0.0) if len(squeeze) >= 3 else 0.0
        if nz(squeeze[-2]) < 1.0 and was_on >= 1.0 and nz(mom[-1]) != 0:
            side = Side.CALL if nz(mom[-1]) > 0 else Side.PUT
            conf = clamp(0.55 + abs(nz(mom[-1])) / max(ctx.last_price * 0.002, 1e-12) * 0.02, 0.5, 0.85)
            return self._signal(ctx, side, conf, "squeeze fired")
        return None


class BandwidthPierce(Strategy):
    """Low-bandwidth coil then directional close through the middle band."""

    name = "bandwidth_pierce"
    label = "Bandwidth Coil Pierce"
    family = "breakout"
    min_bars = 45

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        bw = bandwidth(ctx.closes, 20, 2.0)
        if bw[-1] is None:
            return None
        coil = nz(bw[-1]) < 1.2
        if not coil:
            return None
        prev_close = ctx.closes[-2]
        close_now = ctx.last_price
        mid = last_defined(sma(ctx.closes, 20), close_now) or close_now
        if prev_close <= mid and close_now > mid:
            return self._signal(ctx, Side.CALL, 0.57, "coil pierce up")
        if prev_close >= mid and close_now < mid:
            return self._signal(ctx, Side.PUT, 0.57, "coil pierce down")
        return None


class RangeEscape(Strategy):
    """Compression box breakout after N tight bars."""

    name = "range_escape"
    label = "Range Escape"
    family = "breakout"
    min_bars = 30

    def __init__(self, box_bars: int = 8, tight_frac: float = 0.0015, **kw) -> None:
        self.box_bars = box_bars
        self.tight_frac = tight_frac
        super().__init__(box_bars=box_bars, tight_frac=tight_frac, **kw)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        n = self.box_bars
        if len(ctx.closes) < n + 2:
            return None
        window = ctx.closes[-(n + 1) : -1]
        hi, lo = max(window), min(window)
        width = (hi - lo) / max(ctx.last_price, 1e-12)
        if width > self.tight_frac:
            return None
        if ctx.last_price > hi:
            return self._signal(ctx, Side.CALL, 0.6, "box escape up")
        if ctx.last_price < lo:
            return self._signal(ctx, Side.PUT, 0.6, "box escape down")
        return None


__all__ = [
    "DonchianBreak",
    "KeltnerRide",
    "SqueezePop",
    "BandwidthPierce",
    "RangeEscape",
]
