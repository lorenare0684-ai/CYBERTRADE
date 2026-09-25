"""Order-flow strategies: imbalance momentum, absorption fades, POC reversion.

These read the streaming :class:`~cybertrade.indicators.orderflow.TickFlow`
handed in via ``ctx.extra["flow"]`` (the engine maintains one per asset).  No
flow data → no signal — they never guess.
"""

from __future__ import annotations

from typing import Optional

from ..constants import MarketRegime, Side
from ..data.models import Signal
from .base import Strategy, StrategyContext


def _flow(ctx: StrategyContext):
    flow = (ctx.extra or {}).get("flow")
    return flow if flow is not None and hasattr(flow, "snapshot") else None


class ImbalanceMomentum(Strategy):
    """Persistent one-sided aggression continues the short-term move."""

    name = "imbalance_momentum"
    label = "Tick Imbalance Momentum"
    family = "orderflow"
    min_bars = 35
    lookback = 80
    preferred_regimes = (
        MarketRegime.BULL_TREND,
        MarketRegime.BEAR_TREND,
        MarketRegime.HIGH_VOL,
    )

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        flow = _flow(ctx)
        if flow is None:
            return None
        imb = flow.imbalance(window=60)
        if abs(imb) < 0.35:
            return None          # two-sided tape — no aggression edge
        if ctx.regime.stress >= 0.7:
            return None          # panic prints lie
        side = Side.CALL if imb > 0 else Side.PUT
        conf = 0.5 + 0.25 * abs(imb)
        return self._signal(
            ctx, side=side, confidence=round(conf, 4),
            reason=f"tick imbalance {imb:+.2f}",
            imbalance=round(imb, 3),
        )


class AbsorptionFade(Strategy):
    """Price presses an extreme while delta fails — fade the fake breakout."""

    name = "absorption_fade"
    label = "Delta Absorption Fade"
    family = "orderflow"
    min_bars = 35
    lookback = 80
    preferred_regimes = (MarketRegime.RANGE, MarketRegime.LOW_VOL)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        flow = _flow(ctx)
        if flow is None:
            return None
        div = flow.divergence(window=40)
        if div == 0.0:
            return None
        if ctx.regime.regime not in (MarketRegime.RANGE, MarketRegime.LOW_VOL,
                                     MarketRegime.UNKNOWN):
            return None          # absorption only fades in non-trending tape
        side = Side.CALL if div > 0 else Side.PUT   # bull div → sellers absorbed
        return self._signal(
            ctx, side=side, confidence=0.62,
            reason=f"delta absorption {div:+.0f}",
            divergence=div,
        )


class POCReversion(Strategy):
    """Stretch from the point of control snaps back inside the value area."""

    name = "poc_reversion"
    label = "POC Value Reversion"
    family = "orderflow"
    min_bars = 35
    lookback = 80
    preferred_regimes = (MarketRegime.RANGE, MarketRegime.LOW_VOL)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        flow = _flow(ctx)
        if flow is None:
            return None
        snap = flow.snapshot()
        stretch = snap.get("stretch", 0.0)
        if abs(stretch) < 1.4:
            return None          # inside value — fair price
        if ctx.regime.regime not in (MarketRegime.RANGE, MarketRegime.LOW_VOL):
            return None
        side = Side.PUT if stretch > 0 else Side.CALL
        conf = 0.5 + 0.1 * min(abs(stretch) - 1.4, 1.0)
        return self._signal(
            ctx, side=side, confidence=round(conf, 4),
            reason=f"POC stretch {stretch:+.2f} -> value",
            stretch=round(stretch, 3), poc=snap.get("poc", 0.0),
        )


__all__ = ["ImbalanceMomentum", "AbsorptionFade", "POCReversion"]
