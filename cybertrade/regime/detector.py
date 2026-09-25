"""Market-regime detection: classify conditions so the ensemble can adapt.

The detector fuses trend, volatility, and microstructure evidence into a
:class:`~cybertrade.constants.MarketRegime` label plus a 0..1 confidence and a
stress score the survivor playbook consumes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..constants import MarketRegime
from ..data.models import Candle, candles_to_series
from ..indicators import (
    adx,
    atr,
    bollinger_bands,
    garch_11,
    historical_volatility,
    linear_regression_slope,
    rsi,
    vol_percentile,
    volatility_ratio,
)
from ..utils.mathx import clamp, nz, percentile, safe_div, stdev

log = logging.getLogger("cybertrade.regime")


@dataclass
class RegimeReading:
    """Full regime assessment for one asset."""

    regime: MarketRegime = MarketRegime.UNKNOWN
    confidence: float = 0.0
    stress: float = 0.0                 # 0..1 crisis pressure
    trend_strength: float = 0.0         # 0..1
    trend_direction: int = 0            # -1 / 0 / +1
    volatility_state: str = "normal"    # low | normal | high | extreme
    vol_percentile: float = 50.0
    range_score: float = 0.0
    gap_risk: float = 0.0
    details: Dict[str, float] = field(default_factory=dict)
    ts: float = 0.0

    @property
    def is_trend(self) -> bool:
        return self.regime in (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)

    @property
    def is_defensive(self) -> bool:
        return self.regime in (MarketRegime.CRISIS, MarketRegime.GAP) or self.stress > 0.6

    @property
    def label(self) -> str:
        return self.regime.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime.value,
            "confidence": round(self.confidence, 3),
            "stress": round(self.stress, 3),
            "trend_strength": round(self.trend_strength, 3),
            "trend_direction": self.trend_direction,
            "volatility_state": self.volatility_state,
            "vol_percentile": round(self.vol_percentile, 1),
            "range_score": round(self.range_score, 3),
            "gap_risk": round(self.gap_risk, 3),
            "details": {k: round(v, 4) for k, v in self.details.items()},
            "ts": self.ts,
        }


class RegimeDetector:
    """Classify up to ``lookback`` candles into a coarse market state.

    Evidence weights (defaults) favour a "when in doubt, stay out" posture:
    crisis and gap labels require only moderate evidence because their cost
    of missing is asymmetric.
    """

    def __init__(
        self,
        trend_period: int = 14,
        vol_period: int = 20,
        lookback: int = 120,
        crisis_z: float = 3.0,
    ) -> None:
        self.trend_period = trend_period
        self.vol_period = vol_period
        self.lookback = lookback
        self.crisis_z = crisis_z
        self._history: List[RegimeReading] = []

    # -- public ------------------------------------------------------------
    def assess(self, candles: Sequence[Candle]) -> RegimeReading:
        import time

        if len(candles) < max(30, self.trend_period * 2):
            return RegimeReading(regime=MarketRegime.UNKNOWN, ts=time.time())

        candles = list(candles)[-self.lookback :]
        data = candles_to_series(candles)
        high, low, close = data["high"], data["low"], data["close"]

        # --- trend evidence ------------------------------------------------
        slope = linear_regression_slope(close, self.trend_period)
        last_slope = nz(slope[-1], 0.0)
        norm_slope = safe_div(last_slope, close[-1], 0.0) * 100.0  # % per bar
        adx_line, plus_di, minus_di = adx(high, low, close, self.trend_period)
        adx_v = nz(adx_line[-1], 0.0)
        trend_strength = clamp(adx_v / 50.0, 0.0, 1.0)
        direction = 0
        if norm_slope > 0.01 and nz(plus_di[-1]) > nz(minus_di[-1]):
            direction = 1
        elif norm_slope < -0.01 and nz(minus_di[-1]) > nz(plus_di[-1]):
            direction = -1

        # --- volatility evidence -------------------------------------------
        rsi_v = nz(rsi(close, self.trend_period)[-1], 50.0)
        vol_pct = nz(vol_percentile(close, 5, self.lookback)[-1], 50.0)
        garch = nz(garch_11(close)[-1], 0.0)
        hist_vol = nz(historical_volatility(close, self.vol_period)[-1], 0.0)
        vratio = nz(volatility_ratio(high, low, close, 5, self.vol_period)[-1], 1.0)
        rets = _log_returns(close)
        window_rets = rets[-self.vol_period :]
        ret_sd = stdev(window_rets, ddof=1) if len(rets) > self.vol_period else 0.0
        mean_abs = (
            sum(abs(r) for r in window_rets) / len(window_rets) if window_rets else 0.0
        )
        # Materiality floor: on compressed-vol tapes every blip is "N-sigma".
        # A shock must be big in size AND in sigmas — never sigma alone.
        material = 2.5 * mean_abs
        last_ret = rets[-1] if rets else 0.0
        z_last = safe_div(abs(last_ret), ret_sd, 0.0)
        shock = bool(abs(last_ret) >= material and z_last >= self.crisis_z)
        z_stress = z_last if abs(last_ret) >= material else 0.0

        # --- crash echo ----------------------------------------------------
        # A catastrophic down-bar poisons the tape for several bars: the
        # reflexive bounce must NEVER be read as a fresh bull trend.
        echo_bars = max(5, self.lookback // 6)
        recent_rets = rets[-echo_bars:]
        crash_ret = min(recent_rets) if recent_rets else 0.0
        crash_echo = bool(
            ret_sd > 0
            and crash_ret <= -3.5 * ret_sd
            and abs(crash_ret) >= material
        )

        # --- range evidence ------------------------------------------------
        _, mid, _, _ = bollinger_bands(close, self.vol_period, 2.0)
        bandwidth = 0.0
        u, m, l, _ = bollinger_bands(close, self.vol_period, 2.0)
        if u[-1] is not None and m[-1]:
            bandwidth = 100.0 * ((u[-1] or 0.0) - (l[-1] or 0.0)) / (m[-1] or 1.0)
        range_score = clamp(1.0 - trend_strength, 0.0, 1.0) * clamp(1.0 - vol_pct / 100.0, 0.0, 1.0)

        # --- gap / jump evidence -------------------------------------------
        gaps = 0
        for i in range(1, len(close)):
            diff = abs(close[i] - close[i - 1])
            if (
                ret_sd > 0
                and diff > 3.0 * ret_sd * close[i - 1]
                and diff >= material * close[i - 1]
            ):
                gaps += 1
        gap_risk = clamp(gaps / 5.0, 0.0, 1.0)

        # --- stress --------------------------------------------------------
        stress = clamp(
            0.45 * clamp(z_stress / self.crisis_z, 0.0, 1.0)
            + 0.30 * clamp(vol_pct / 100.0, 0.0, 1.0)
            + 0.25 * gap_risk,
            0.0,
            1.0,
        )

        # --- decision ------------------------------------------------------
        regime, confidence = self._decide(
            direction=direction,
            trend_strength=trend_strength,
            vol_pct=vol_pct,
            vratio=vratio,
            stress=stress,
            z_last=z_last,
            gap_risk=gap_risk,
            range_score=range_score,
            crash_echo=crash_echo,
            last_ret=last_ret,
            shock=shock,
        )

        vol_state = "normal"
        if vol_pct >= 95 or z_last >= self.crisis_z:
            vol_state = "extreme"
        elif vol_pct >= 80 or vratio >= 1.6:
            vol_state = "high"
        elif vol_pct <= 25 and vratio <= 0.7:
            vol_state = "low"

        reading = RegimeReading(
            regime=regime,
            confidence=confidence,
            stress=stress,
            trend_strength=trend_strength,
            trend_direction=direction,
            volatility_state=vol_state,
            vol_percentile=vol_pct,
            range_score=range_score,
            gap_risk=gap_risk,
            details={
                "adx": adx_v,
                "rsi": rsi_v,
                "slope_pct": norm_slope,
                "z_last": z_last,
                "vol_ratio": vratio,
                "garch": garch,
                "hist_vol": hist_vol,
                "bandwidth": bandwidth,
                "crash_echo": crash_echo,
            },
            ts=candles[-1].close_ts,
        )
        self._history.append(reading)
        if len(self._history) > 256:
            del self._history[:-256]
        return reading

    def _decide(
        self,
        *,
        direction: int,
        trend_strength: float,
        vol_pct: float,
        vratio: float,
        stress: float,
        z_last: float,
        gap_risk: float,
        range_score: float,
        crash_echo: bool = False,
        last_ret: float = 0.0,
        shock: bool = True,
    ) -> tuple[MarketRegime, float]:
        # Crisis dominates everything.  ``shock`` = material AND extreme z
        # (sigma alone is meaningless on compressed-vol tapes).
        if shock or stress >= 0.75:
            return MarketRegime.CRISIS, clamp(stress, 0.6, 1.0)
        # Flash-crash echo: the crash bar and its reflexive bounce both live
        # here.  Calling the bounce a "bull trend" is how bots die.
        if crash_echo and (vratio >= 1.2 or vol_pct >= 65 or stress >= 0.45):
            base = max(stress, 0.55) if last_ret > 0 else max(stress, 0.6)
            return MarketRegime.CRISIS, clamp(base, 0.5, 1.0)
        if gap_risk >= 0.6 and vol_pct >= 70:
            return MarketRegime.GAP, clamp(0.5 + 0.4 * gap_risk, 0.0, 1.0)
        if vol_pct >= 90 or vratio >= 2.0:
            return MarketRegime.HIGH_VOL, clamp(0.4 + vol_pct / 250.0, 0.0, 0.95)
        if vol_pct <= 20 and vratio <= 0.6:
            return MarketRegime.LOW_VOL, clamp(0.5 + (20 - vol_pct) / 60.0, 0.0, 0.9)
        # Trends need a clean tape: no crash echo, no gap soup, no panic.
        trend_ok = not crash_echo and gap_risk < 0.5 and stress < 0.55
        if direction != 0 and trend_strength >= 0.45 and trend_ok:
            regime = (
                MarketRegime.BULL_TREND if direction > 0 else MarketRegime.BEAR_TREND
            )
            return regime, clamp(0.35 + trend_strength * 0.55, 0.0, 0.95)
        if range_score >= 0.5:
            return MarketRegime.RANGE, clamp(0.3 + 0.5 * range_score, 0.0, 0.9)
        if direction != 0 and trend_ok:
            regime = (
                MarketRegime.BULL_TREND if direction > 0 else MarketRegime.BEAR_TREND
            )
            return regime, 0.4
        if crash_echo:
            return MarketRegime.HIGH_VOL, 0.5
        return MarketRegime.RANGE, 0.3

    # -- history helpers ---------------------------------------------------
    def recent(self, limit: int = 10) -> List[RegimeReading]:
        return self._history[-limit:]

    def stability(self, window: int = 20) -> float:
        """0..1 — how stable regimes have been (low = whipsaw hell)."""
        if len(self._history) < 2:
            return 1.0
        labels = [r.regime for r in self._history[-window:]]
        switches = sum(1 for i in range(1, len(labels)) if labels[i] != labels[i - 1])
        return clamp(1.0 - switches / max(1.0, len(labels) - 1), 0.0, 1.0)

    def stress_average(self, window: int = 20) -> float:
        vals = [r.stress for r in self._history[-window:]]
        return sum(vals) / len(vals) if vals else 0.0


def _log_returns(close: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(close)):
        if close[i - 1] > 0 and close[i] > 0:
            out.append((close[i] - close[i - 1]) / close[i - 1])
    return out


__all__ = ["RegimeReading", "RegimeDetector"]
