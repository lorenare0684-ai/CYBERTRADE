"""Volatility-regime strategies (trade the expansion/contraction cycle)."""

from __future__ import annotations

from typing import Optional

from ..constants import MarketRegime, Side
from ..data.models import Signal
from ..indicators import (
    bollinger_bands,
    last_defined,
    volatility_ratio,
)
from ..indicators.volatility import (
    ewma_volatility,
    garch_11,
    historical_volatility,
)
from ..utils.mathx import clamp, nz
from .base import Strategy, StrategyContext


class VolExpansionRider(Strategy):
    """Volatility ratio expansion in the direction of the first thrust."""

    name = "vol_expansion_rider"
    label = "Vol Expansion Rider"
    family = "volatility"
    min_bars = 45
    preferred_regimes = (MarketRegime.HIGH_VOL, MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        vr = volatility_ratio(ctx.highs, ctx.lows, ctx.closes, 5, 20)
        if vr[-1] is None:
            return None
        if not (1.4 <= nz(vr[-1]) <= 3.5):
            return None
        body = ctx.last_price - ctx.closes[-2]
        scale = max(ctx.last_price * 0.0004, 1e-12)
        if body > scale:
            return self._signal(ctx, Side.CALL, 0.58, f"vol expand {vr[-1]:.2f}")
        if body < -scale:
            return self._signal(ctx, Side.PUT, 0.58, f"vol expand {vr[-1]:.2f}")
        return None


class SqueezeRelease(Strategy):
    """Bandwidth percentile compression releasing after a volatility floor."""

    name = "squeeze_release"
    label = "Vol Squeeze Release"
    family = "volatility"
    min_bars = 55

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        ewv = ewma_volatility(ctx.closes, 20)
        bb_u, bb_m, bb_l, _ = bollinger_bands(ctx.closes)
        if ewv[-1] is None or ewv[-2] is None or bb_m[-1] is None:
            return None
        compressing = nz(ewv[-3] if len(ewv) > 2 else ewv[-1]) > nz(ewv[-2]) > nz(ewv[-1])
        if not compressing:
            return None
        if ctx.last_price > nz(bb_u[-1]):
            return self._signal(ctx, Side.CALL, 0.57, "squeeze release up")
        if ctx.last_price < nz(bb_l[-1]):
            return self._signal(ctx, Side.PUT, 0.57, "squeeze release down")
        return None


class GarchMeanRev(Strategy):
    """After GARCH vol spikes, fade the overshoot back to VWAP-ish mean."""

    name = "garch_meanrev"
    label = "GARCH Spike Fade"
    family = "volatility"
    min_bars = 50
    preferred_regimes = (MarketRegime.RANGE, MarketRegime.HIGH_VOL)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        g = garch_11(ctx.closes)
        if g[-1] is None or g[-2] is None or g[-6] is None:
            return None
        base = nz(g[-6])
        if base <= 0:
            return None
        spike = nz(g[-1]) / base
        if spike < 1.8:
            return None
        move = ctx.last_price - ctx.closes[-5]
        pct = move / max(ctx.last_price, 1e-12)
        if pct < -0.004:
            return self._signal(ctx, Side.CALL, 0.56, "post-spike snap up")
        if pct > 0.004:
            return self._signal(ctx, Side.PUT, 0.56, "post-spike snap down")
        return None


class VolTargetStretch:
    """Placeholder container kept for registry introspection (disabled)."""

    name = "vol_target_stretch"
    label = "Vol Target Stretch (reserved)"


class HVContrarianFade(Strategy):
    """Historical-vol percentile extremes with exhaustion bar confirmation."""

    name = "hv_contrarian_fade"
    label = "HV Percentile Fade"
    family = "volatility"
    min_bars = 55

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        hv = historical_volatility(ctx.closes, 20)
        if hv[-1] is None:
            return None
        level = nz(last_defined(hv, 50.0), 50.0)
        closes = ctx.closes
        exhaustion_up = closes[-1] > closes[-2] < closes[-3] if len(closes) > 2 else False
        exhaustion_dn = closes[-1] < closes[-2] > closes[-3] if len(closes) > 2 else False
        if level > 90 and exhaustion_dn:
            return self._signal(ctx, Side.CALL, 0.55, "hv extreme fade up")
        if level > 90 and exhaustion_up:
            return self._signal(ctx, Side.PUT, 0.55, "hv extreme fade down")
        return None


__all__ = [
    "VolExpansionRider",
    "SqueezeRelease",
    "GarchMeanRev",
    "HVContrarianFade",
]
