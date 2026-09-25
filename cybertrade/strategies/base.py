"""Strategy base class and shared context plumbing."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..constants import MarketRegime, Side, Timeframe
from ..data.models import Candle, Signal, candles_to_series
from ..exceptions import StrategyError
from ..regime.detector import RegimeReading

log = logging.getLogger("cybertrade.strategy")


@dataclass
class StrategyContext:
    """Everything a strategy may look at for one decision point."""

    asset: str
    candles: List[Candle]
    regime: RegimeReading = field(default_factory=RegimeReading)
    timeframe_seconds: int = 60
    expiry_seconds: int = 60
    payout: float = 0.85
    ts: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def series(self) -> Dict[str, List[float]]:
        return candles_to_series(self.candles)

    @property
    def closes(self) -> List[float]:
        return [c.close for c in self.candles]

    @property
    def highs(self) -> List[float]:
        return [c.high for c in self.candles]

    @property
    def lows(self) -> List[float]:
        return [c.low for c in self.candles]

    @property
    def opens(self) -> List[float]:
        return [c.open for c in self.candles]

    @property
    def volumes(self) -> List[float]:
        return [c.volume for c in self.candles]

    @property
    def last(self) -> Optional[Candle]:
        return self.candles[-1] if self.candles else None

    @property
    def last_price(self) -> float:
        return self.candles[-1].close if self.candles else 0.0

    def require(self, bars: int) -> bool:
        return len(self.candles) >= bars


class Strategy:
    """Base class: one idea, one decision per closed candle.

    Subclasses implement :meth:`generate` and return ``None`` (no edge) or a
    :class:`~cybertrade.data.models.Signal`.  ``min_bars`` protects warmup.
    """

    name: str = "base"
    label: str = "Base Strategy"
    family: str = "core"
    min_bars: int = 30
    lookback: int = 150

    #: regimes where this strategy historically has edge (used by ensemble)
    preferred_regimes: Sequence[MarketRegime] = (
        MarketRegime.BULL_TREND,
        MarketRegime.BEAR_TREND,
        MarketRegime.RANGE,
        MarketRegime.LOW_VOL,
        MarketRegime.HIGH_VOL,
    )

    def __init__(self, **params: Any) -> None:
        self.params = params
        self.enabled = True
        self.wins = 0
        self.losses = 0
        self.pnl = 0.0
        self.signals_sent = 0
        self.weight = 1.0
        self._validate_params()

    def _validate_params(self) -> None:
        pass

    # -- decision ----------------------------------------------------------
    def generate(self, ctx: StrategyContext) -> Optional[Signal]:
        if not self.enabled:
            return None
        if not ctx.require(self.min_bars):
            return None
        try:
            signal = self.decide(ctx)
        except Exception:  # noqa: BLE001 - a bad strategy must not kill the bot
            log.exception("strategy crashed name=%s asset=%s", self.name, ctx.asset)
            return None
        if signal is None:
            return None
        if not signal.is_trade:
            return None
        self.signals_sent += 1
        return signal

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        raise NotImplementedError

    # -- outcome tracking --------------------------------------------------
    def record_result(self, won: bool, pnl: float) -> None:
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.pnl += pnl

    @property
    def attempts(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.attempts if self.attempts else 0.0

    def reset_stats(self) -> None:
        self.wins = 0
        self.losses = 0
        self.pnl = 0.0
        self.signals_sent = 0

    def score_for_regime(self, regime: MarketRegime) -> float:
        """0..1 suitability of this strategy for the current regime."""
        if regime in self.preferred_regimes:
            return 1.0
        if regime in (MarketRegime.CRISIS, MarketRegime.GAP):
            return 0.15
        return 0.5

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "family": self.family,
            "enabled": self.enabled,
            "win_rate": round(self.win_rate, 4),
            "attempts": self.attempts,
            "pnl": round(self.pnl, 2),
            "signals_sent": self.signals_sent,
            "weight": round(self.weight, 3),
            "params": self.params,
        }

    # -- helpers -----------------------------------------------------------
    def _signal(
        self,
        ctx: StrategyContext,
        side: Side,
        confidence: float,
        reason: str,
        **meta: Any,
    ) -> Signal:
        return Signal(
            asset=ctx.asset,
            side=side,
            confidence=confidence,
            strategy=self.name,
            timeframe_seconds=ctx.timeframe_seconds,
            ts=ctx.ts or (ctx.candles[-1].close_ts if ctx.candles else 0.0),
            reason=reason,
            expiry_seconds=ctx.expiry_seconds,
            price=ctx.last_price,
            meta=dict(meta),
        )


__all__ = ["Strategy", "StrategyContext"]
