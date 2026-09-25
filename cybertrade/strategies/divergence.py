"""Divergence strategies: RSI / MACD / CCI exhaustion edges + MTF confluence.

Each reads price structure through the shared :class:`StrategyContext` and
emits binary-option signals at *confirmed* pivot points (right-bar lag, so the
strategies can never peek at the future).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..constants import MarketRegime, Side
from ..data.models import Signal
from ..data.history import resample
from ..indicators.core import ema, macd, rsi
from ..indicators.momentum import cci
from ..indicators.divergence import last_divergence
from .base import Strategy, StrategyContext


def _prep(ctx: StrategyContext) -> Tuple[List[float], List[float], List[float], List[float]]:
    return ctx.closes, ctx.highs, ctx.lows, ctx.opens


class RSIDivergenceEdge(Strategy):
    """Classic RSI divergence at swing points, trend-context filtered.

    Regular divergences may fade a trend (reversal); hidden divergences are
    left to :class:`MacdHiddenDivergence` (continuation).
    """

    name = "rsi_divergence"
    label = "RSI Divergence Edge"
    family = "divergence"
    min_bars = 40
    lookback = 120
    preferred_regimes = (
        MarketRegime.BEAR_TREND,
        MarketRegime.BULL_TREND,
        MarketRegime.HIGH_VOL,
        MarketRegime.RANGE,
    )

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        closes, highs, lows, opens = _prep(ctx)
        rsi_v = rsi(closes, 14)
        div = last_divergence(closes, rsi_v, left=3, right=3, within=4)
        if div is None or not div.is_regular:
            return None
        score = div.strength
        if score < 0.35:
            return None
        trend = ema(closes, 50)
        up_trend = closes[-1] > (trend[-1] or closes[-1])
        # counter-trend fades only when the signal is strong enough to matter
        if div.is_bull and up_trend and score < 0.7:
            return None
        if not div.is_bull and not up_trend and score < 0.7:
            return None
        side = Side.CALL if div.is_bull else Side.PUT
        return self._signal(
            ctx,
            side=side,
            confidence=round(0.4 + score * 0.45, 4),
            reason=f"RSI {div.kind} strength={score:.2f}",
            score=score,
            kind=div.kind,
            regime=ctx.regime.regime.value,
        )


class MacdHiddenDivergence(Strategy):
    """Hidden MACD divergence: trend continuation after a pullback."""

    name = "macd_hidden_divergence"
    label = "MACD Hidden Divergence"
    family = "divergence"
    min_bars = 50
    lookback = 140
    preferred_regimes = (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        closes, highs, lows, opens = _prep(ctx)
        line, _sig, _hist = macd(closes, 12, 26, 9)
        div = last_divergence(closes, line, left=3, right=3, within=3)
        if div is None or div.is_regular:
            return None  # hidden only
        score = div.strength
        if score < 0.3:
            return None
        regime = ctx.regime.regime
        if div.is_bull and regime is not MarketRegime.BULL_TREND:
            return None
        if not div.is_bull and regime is not MarketRegime.BEAR_TREND:
            return None
        side = Side.CALL if div.is_bull else Side.PUT
        return self._signal(
            ctx,
            side=side,
            confidence=round(0.45 + score * 0.4, 4),
            reason=f"MACD {div.kind} strength={score:.2f}",
            score=score,
            kind=div.kind,
            regime=regime.value,
        )


class CCIDivergenceFade(Strategy):
    """CCI divergence fade in ranging/low-vol regimes."""

    name = "cci_divergence_fade"
    label = "CCI Divergence Fade"
    family = "divergence"
    min_bars = 45
    lookback = 120
    preferred_regimes = (MarketRegime.RANGE, MarketRegime.LOW_VOL)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        closes, highs, lows, opens = _prep(ctx)
        cci_v = cci(highs, lows, closes, 20)
        div = last_divergence(closes, cci_v, left=3, right=3, within=4)
        if div is None or not div.is_regular:
            return None
        score = div.strength
        if score < 0.3:
            return None
        regime = ctx.regime.regime
        if regime not in (MarketRegime.RANGE, MarketRegime.LOW_VOL, MarketRegime.UNKNOWN):
            return None
        side = Side.CALL if div.is_bull else Side.PUT
        return self._signal(
            ctx,
            side=side,
            confidence=round(0.35 + score * 0.4, 4),
            reason=f"CCI {div.kind} fade strength={score:.2f}",
            score=score,
            kind=div.kind,
            regime=regime.value,
        )


class MultiTimeframeConfluence(Strategy):
    """Base-TF RSI divergence confirmed by a 5× HTF trend filter.

    HTF candles are resampled from the context book (5× the base timeframe),
    keeping the strategy self-contained — no feed coupling.
    """

    name = "mtf_confluence"
    label = "Multi-Timeframe Confluence"
    family = "divergence"
    min_bars = 80
    lookback = 200
    preferred_regimes = (
        MarketRegime.BULL_TREND,
        MarketRegime.BEAR_TREND,
        MarketRegime.HIGH_VOL,
    )

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        closes, highs, lows, opens = _prep(ctx)
        target_tf = ctx.timeframe_seconds * 5
        htf = resample(ctx.candles, target_tf)
        if len(htf) < 15:
            return None
        htf_close = [c.close for c in htf]
        htf_trend = ema(htf_close, 20)
        htf_bias = 1 if htf_close[-1] > (htf_trend[-1] or htf_close[-1]) else -1

        rsi_v = rsi(closes, 14)
        div = last_divergence(closes, rsi_v, left=3, right=3, within=4)
        if div is None:
            return None
        want = 1 if div.is_bull else -1
        if want != htf_bias:
            return None  # no confluence
        score = div.strength
        side = Side.CALL if want == 1 else Side.PUT
        return self._signal(
            ctx,
            side=side,
            confidence=round(0.5 + score * 0.4, 4),
            reason=f"MTF {div.kind} aligned with {target_tf}s bias",
            score=score,
            kind=div.kind,
            htf_bias=htf_bias,
            regime=ctx.regime.regime.value,
        )


__all__ = [
    "RSIDivergenceEdge",
    "MacdHiddenDivergence",
    "CCIDivergenceFade",
    "MultiTimeframeConfluence",
]
