"""Price-action / pattern strategies (pure candle geometry)."""

from __future__ import annotations

from typing import Optional

from ..constants import MarketRegime, Side
from ..data.models import Signal
from ..indicators import last_defined
from ..indicators.core import ema, sma
from ..indicators.patterns import engulfing, hammer, three_soldiers
from ..utils.mathx import clamp, nz
from .base import Strategy, StrategyContext


class EngulfingEdge(Strategy):
    """Engulfing bar in the direction of the local drift."""

    name = "engulfing_edge"
    label = "Engulfing Edge"
    family = "pattern"
    min_bars = 25
    preferred_regimes = (MarketRegime.RANGE, MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        marks = engulfing(ctx.opens, ctx.highs, ctx.lows, ctx.closes)
        if marks[-1] is None or marks[-1] == 0:
            return None
        drift = last_defined(sma(ctx.closes, 20), ctx.last_price) or ctx.last_price
        if marks[-1] == 1 and ctx.last_price > drift * 0.998:
            return self._signal(ctx, Side.CALL, 0.6, "bull engulf")
        if marks[-1] == -1 and ctx.last_price < drift * 1.002:
            return self._signal(ctx, Side.PUT, 0.6, "bear engulf")
        return None


class HammerHook(Strategy):
    """Hammer reversal with EMA location filter."""

    name = "hammer_hook"
    label = "Hammer Hook"
    family = "pattern"
    min_bars = 25

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        marks = hammer(ctx.opens, ctx.highs, ctx.lows, ctx.closes)
        if marks[-1] is None or marks[-1] == 0:
            return None
        e = last_defined(ema(ctx.closes, 20), ctx.last_price) or ctx.last_price
        if marks[-1] == 1 and ctx.last_price <= e * 1.002:
            return self._signal(ctx, Side.CALL, 0.58, "hammer at mean")
        if marks[-1] == -1 and ctx.last_price >= e * 0.998:
            return self._signal(ctx, Side.PUT, 0.58, "hanger at mean")
        return None


class SoldiersCrows(Strategy):
    """Three soldiers / crows continuation."""

    name = "soldiers_crows"
    label = "Three Soldiers & Crows"
    family = "pattern"
    min_bars = 25

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        marks = three_soldiers(ctx.opens, ctx.highs, ctx.lows, ctx.closes)
        if marks[-1] is None or marks[-1] == 0:
            return None
        side = Side.CALL if marks[-1] == 1 else Side.PUT
        return self._signal(ctx, side, 0.63, "three-bar march")


class InsideBreakoutBar(Strategy):
    """Inside bar coil resolving into the mother-bar direction."""

    name = "inside_breakout"
    label = "Inside-Bar Breakout"
    family = "pattern"
    min_bars = 20

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        if len(ctx.closes) < 3:
            return None
        h1, l1 = ctx.highs[-2], ctx.lows[-2]
        h0, l0 = ctx.highs[-3], ctx.lows[-3]
        inside = h1 <= h0 and l1 >= l0
        if not inside:
            return None
        if ctx.last_price > h1:
            return self._signal(ctx, Side.CALL, 0.56, "inside bar up-break")
        if ctx.last_price < l1:
            return self._signal(ctx, Side.PUT, 0.56, "inside bar down-break")
        return None


class DoubleWickRejection(Strategy):
    """Two consecutive same-side rejections at a swing level."""

    name = "double_wick"
    label = "Double Wick Rejection"
    family = "pattern"
    min_bars = 25

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        if len(ctx.closes) < 2:
            return None

        def lower_wick(i: int) -> float:
            return min(ctx.opens[i], ctx.closes[i]) - ctx.lows[i]

        def upper_wick(i: int) -> float:
            return ctx.highs[i] - max(ctx.opens[i], ctx.closes[i])

        r1 = ctx.highs[-1] - ctx.lows[-1]
        r2 = ctx.highs[-2] - ctx.lows[-2]
        if min(r1, r2) <= 0:
            return None
        both_lower = lower_wick(-1) > 0.5 * r1 and lower_wick(-2) > 0.5 * r2
        both_upper = upper_wick(-1) > 0.5 * r1 and upper_wick(-2) > 0.5 * r2
        lows_close = abs(ctx.lows[-1] - ctx.lows[-2]) <= 0.25 * r1
        highs_close = abs(ctx.highs[-1] - ctx.highs[-2]) <= 0.25 * r1
        if both_lower and lows_close:
            return self._signal(ctx, Side.CALL, 0.6, "double floor wick")
        if both_upper and highs_close:
            return self._signal(ctx, Side.PUT, 0.6, "double ceiling wick")
        return None


__all__ = [
    "EngulfingEdge",
    "HammerHook",
    "SoldiersCrows",
    "InsideBreakoutBar",
    "DoubleWickRejection",
]
