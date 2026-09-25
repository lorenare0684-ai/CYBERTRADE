"""Trend-following strategies (ride directional regimes)."""

from __future__ import annotations

from typing import Optional

from ..constants import MarketRegime, Side
from ..data.models import Signal
from ..indicators import (
    adx,
    crossover,
    ema,
    ichimoku,
    last_defined,
    macd,
    psar,
    supertrend,
    trend_intensity_index,
)
from ..utils.mathx import clamp, nz
from .base import Strategy, StrategyContext


class EMACrossTrend(Strategy):
    """Fast/slow EMA cross aligned with price position."""

    name = "ema_cross_trend"
    label = "EMA Cross Trend"
    family = "trend"
    min_bars = 60
    preferred_regimes = (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)

    def __init__(self, fast: int = 9, slow: int = 21, confirm: int = 3, **kw) -> None:
        self.fast = fast
        self.slow = slow
        self.confirm = confirm
        super().__init__(fast=fast, slow=slow, confirm=confirm, **kw)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        close = ctx.closes
        ef = ema(close, self.fast)
        es = ema(close, self.slow)
        cross = crossover(ef, es)
        if cross[-1] is None or cross[-2] is None:
            return None
        # require the cross to hold a few bars, avoid one-bar flips
        hold = cross[-self.confirm :] if self.confirm <= len(cross) else cross
        if not all(h == cross[-1] for h in hold):
            return None
        if cross[-1] == cross[-2]:
            return None  # only fire on the fresh flip
        side = Side.CALL if cross[-1] else Side.PUT
        spread = abs(nz(ef[-1]) - nz(es[-1])) / max(close[-1], 1e-12)
        conf = clamp(0.5 + spread * 400, 0.5, 0.85)
        return self._signal(ctx, side, conf, f"ema {self.fast}/{self.slow} flip")


class MacdTrendRider(Strategy):
    """MACD histogram expansion in the trend direction."""

    name = "macd_trend_rider"
    label = "MACD Trend Rider"
    family = "trend"
    min_bars = 60

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        close = ctx.closes
        line, sig, hist = macd(close)
        if hist[-1] is None or hist[-2] is None:
            return None
        expanding = abs(nz(hist[-1])) > abs(nz(hist[-2])) > 0
        if not expanding or nz(hist[-1]) == nz(hist[-2]):
            return None
        side = Side.CALL if nz(hist[-1]) > 0 else Side.PUT
        mom = nz(hist[-1]) - nz(hist[-2])
        conf = clamp(0.52 + abs(mom) / max(close[-1], 1e-12) * 800, 0.5, 0.8)
        return self._signal(ctx, side, conf, "macd hist expansion")


class SupertrendSurf(Strategy):
    """Follow SuperTrend direction flips."""

    name = "supertrend_surf"
    label = "SuperTrend Surf"
    family = "trend"
    min_bars = 40

    def __init__(self, period: int = 10, mult: float = 3.0, **kw) -> None:
        self.period = period
        self.mult = mult
        super().__init__(period=period, mult=mult, **kw)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        line, direction = supertrend(ctx.highs, ctx.lows, ctx.closes, self.period, self.mult)
        if len(direction) < 3 or direction[-1] == 0:
            return None
        if direction[-1] == direction[-2]:
            return None
        side = Side.CALL if direction[-1] > 0 else Side.PUT
        dist = abs(ctx.last_price - nz(line[-1], ctx.last_price)) / max(ctx.last_price, 1e-12)
        conf = clamp(0.55 + dist * 300, 0.5, 0.82)
        return self._signal(ctx, side, conf, "supertrend flip")


class IchimokuKumo(Strategy):
    """Trade Tenkan/Kijun crosses confirmed by the Kumo cloud."""

    name = "ichimoku_kumo"
    label = "Ichimoku Kumo Cross"
    family = "trend"
    min_bars = 80

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        t, k, span_a, span_b, _ = ichimoku(ctx.highs, ctx.lows, ctx.closes)
        if t[-1] is None or k[-1] is None or span_a[-1] is None or span_b[-1] is None:
            return None
        if t[-2] is None or k[-2] is None:
            return None
        cross_up = t[-1] > k[-1] and t[-2] <= k[-2]
        cross_dn = t[-1] < k[-1] and t[-2] >= k[-2]
        if not (cross_up or cross_dn):
            return None
        cloud_top = max(nz(span_a[-1]), nz(span_b[-1]))
        cloud_bot = min(nz(span_a[-1]), nz(span_b[-1]))
        price = ctx.last_price
        if cross_up and price > cloud_top:
            return self._signal(ctx, Side.CALL, 0.68, "tk cross above cloud")
        if cross_dn and price < cloud_bot:
            return self._signal(ctx, Side.PUT, 0.68, "tk cross below cloud")
        return None


class PSARFlip(Strategy):
    """Parabolic SAR flip with trend-intensity confirmation."""

    name = "psar_flip"
    label = "PSAR Flip"
    family = "trend"
    min_bars = 40

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        sar = psar(ctx.highs, ctx.lows)
        if sar[-1] is None or sar[-2] is None:
            return None
        price = ctx.last_price
        prev_price = ctx.closes[-2]
        was_above = prev_price > nz(sar[-2])
        now_above = price > nz(sar[-1])
        if was_above == now_above:
            return None
        tii = last_defined(trend_intensity_index(ctx.closes, 30), 50.0) or 50.0
        side = Side.CALL if now_above else Side.PUT
        conf = clamp(0.5 + tii / 250.0, 0.5, 0.75)
        return self._signal(ctx, side, conf, "psar flip", tii=tii)


class ADXTrendStorm(Strategy):
    """ADX-confirmed directional pressure via DI cross."""

    name = "adx_trend_storm"
    label = "ADX Trend Storm"
    family = "trend"
    min_bars = 50

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        adx_line, plus_di, minus_di = adx(ctx.highs, ctx.lows, ctx.closes, 14)
        if adx_line[-1] is None or plus_di[-1] is None:
            return None
        if nz(adx_line[-1]) < 25:
            return None  # no storm
        up = nz(plus_di[-1]) - nz(minus_di[-1])
        up_prev = nz(plus_di[-2]) - nz(minus_di[-2])
        if up * up_prev > 0 or abs(up) < 0.5:
            return None  # want a fresh dominant DI
        side = Side.CALL if up > 0 else Side.PUT
        conf = clamp(0.5 + nz(adx_line[-1]) / 140.0, 0.5, 0.85)
        return self._signal(ctx, side, conf, f"di storm adx={nz(adx_line[-1]):.0f}")


__all__ = [
    "EMACrossTrend",
    "MacdTrendRider",
    "SupertrendSurf",
    "IchimokuKumo",
    "PSARFlip",
    "ADXTrendStorm",
]
