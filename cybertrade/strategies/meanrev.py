"""Mean-reversion strategies (fade extremes in ranging/quiet regimes)."""

from __future__ import annotations

from typing import Optional

from ..constants import MarketRegime, Side
from ..data.models import Signal
from ..indicators import (
    bollinger_bands,
    cci,
    last_defined,
    rsi,
    stochastic,
    williams_r,
)
from ..indicators.patterns import pin_bar
from ..utils.mathx import clamp, nz
from .base import Strategy, StrategyContext


class BollingerRevert(Strategy):
    """Fade tags of the outer Bollinger band inside range regimes."""

    name = "bollinger_revert"
    label = "Bollinger Reversion"
    family = "meanrev"
    min_bars = 30
    preferred_regimes = (MarketRegime.RANGE, MarketRegime.LOW_VOL)

    def __init__(self, period: int = 20, dev: float = 2.0, **kw) -> None:
        self.period = period
        self.dev = dev
        super().__init__(period=period, dev=dev, **kw)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        close = ctx.closes
        upper, mid, lower, pct_b = bollinger_bands(close, self.period, self.dev)
        if pct_b[-1] is None:
            return None
        price = ctx.last_price
        if pct_b[-1] <= 0.05 and close[-2] >= nz(lower[-2], close[-2]):
            conf = clamp(0.55 + (0.05 - pct_b[-1]) * 4, 0.5, 0.8)
            return self._signal(ctx, Side.CALL, conf, f"%b={pct_b[-1]:.2f} lower tag")
        if pct_b[-1] >= 0.95 and close[-2] <= nz(upper[-2], close[-2]):
            conf = clamp(0.55 + (pct_b[-1] - 0.95) * 4, 0.5, 0.8)
            return self._signal(ctx, Side.PUT, conf, f"%b={pct_b[-1]:.2f} upper tag")
        return None


class RSIStretch(Strategy):
    """Buy oversold pops / sell overbought drops on RSI extremes."""

    name = "rsi_stretch"
    label = "RSI Stretch Fade"
    family = "meanrev"
    min_bars = 30
    preferred_regimes = (MarketRegime.RANGE, MarketRegime.LOW_VOL)

    def __init__(self, period: int = 14, oversold: float = 28.0, overbought: float = 72.0, **kw) -> None:
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        super().__init__(
            period=period, oversold=oversold, overbought=overbought, **kw
        )

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        r = rsi(ctx.closes, self.period)
        if r[-1] is None or r[-2] is None:
            return None
        if nz(r[-2]) <= self.oversold and nz(r[-1]) > self.oversold:
            conf = clamp(0.55 + (self.oversold - nz(r[-2])) / 60.0, 0.5, 0.8)
            return self._signal(ctx, Side.CALL, conf, f"rsi exit oversold {r[-1]:.0f}")
        if nz(r[-2]) >= self.overbought and nz(r[-1]) < self.overbought:
            conf = clamp(0.55 + (nz(r[-2]) - self.overbought) / 60.0, 0.5, 0.8)
            return self._signal(ctx, Side.PUT, conf, f"rsi exit overbought {r[-1]:.0f}")
        return None


class StochHook(Strategy):
    """Stochastic %K/%D hook out of the extreme zone."""

    name = "stoch_hook"
    label = "Stochastic Hook"
    family = "meanrev"
    min_bars = 30

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        k, d = stochastic(ctx.highs, ctx.lows, ctx.closes)
        if k[-1] is None or d[-1] is None or k[-2] is None:
            return None
        if nz(k[-2]) < 25 and nz(k[-1]) > 25 and nz(k[-1]) > nz(d[-1]):
            return self._signal(ctx, Side.CALL, 0.62, "stoch bull hook")
        if nz(k[-2]) > 75 and nz(k[-1]) < 75 and nz(k[-1]) < nz(d[-1]):
            return self._signal(ctx, Side.PUT, 0.62, "stoch bear hook")
        return None


class CCIRevert(Strategy):
    """CCI leaving the +-200 disaster zone back toward normal."""

    name = "cci_revert"
    label = "CCI Snapback"
    family = "meanrev"
    min_bars = 30

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        c = cci(ctx.highs, ctx.lows, ctx.closes, 20)
        if c[-1] is None or c[-2] is None:
            return None
        if nz(c[-2]) < -200 and nz(c[-1]) >= -200:
            return self._signal(ctx, Side.CALL, 0.6, "cci snap up")
        if nz(c[-2]) > 200 and nz(c[-1]) <= 200:
            return self._signal(ctx, Side.PUT, 0.6, "cci snap down")
        return None


class WilliamsFade(Strategy):
    """Williams %R hammer-zone rejection."""

    name = "williams_fade"
    label = "Williams %R Fade"
    family = "meanrev"
    min_bars = 30

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        w = williams_r(ctx.highs, ctx.lows, ctx.closes, 14)
        if w[-1] is None or w[-2] is None:
            return None
        if nz(w[-2]) <= -85 and nz(w[-1]) > -85:
            return self._signal(ctx, Side.CALL, 0.58, "williams exit deep")
        if nz(w[-2]) >= -15 and nz(w[-1]) < -15:
            return self._signal(ctx, Side.PUT, 0.58, "williams exit high")
        return None


class PinBarRevert(Strategy):
    """Rejection pin bar against stretched price."""

    name = "pin_bar_revert"
    label = "Pin Bar Rejection"
    family = "meanrev"
    min_bars = 25

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        marks = pin_bar(ctx.opens, ctx.highs, ctx.lows, ctx.closes)
        if marks[-1] is None or marks[-1] == 0:
            return None
        r = rsi(ctx.closes, 14)
        rsi_v = nz(last_defined(r, 50.0), 50.0)
        if marks[-1] == 1 and rsi_v < 55:
            return self._signal(ctx, Side.CALL, 0.6, "bull pin rejection")
        if marks[-1] == -1 and rsi_v > 45:
            return self._signal(ctx, Side.PUT, 0.6, "bear pin rejection")
        return None


__all__ = [
    "BollingerRevert",
    "RSIStretch",
    "StochHook",
    "CCIRevert",
    "WilliamsFade",
    "PinBarRevert",
]
