"""Adaptive expiry selection for binary contracts.

The horizon you pick IS part of your edge: too short and micro-noise owns
you, too long and drift assumptions rot.  This module estimates the recent
volatility regime from closes and picks the candidate expiry with the best
modeled probability of finishing in the money — the curve math lives in
:func:`cybertrade.quant.binary.best_expiry`.

Strategy conviction enters as *drift* (expected log-moves per bar), never as
fake certainty: a confidence of 0.5 produces a driftless model.  Overstated
confidence gets punished twice — the calibrator shrinks the entry gate, and
here it stretches the horizon into noise.
"""

from __future__ import annotations

import math
import statistics
from typing import Optional, Sequence

from .binary import best_expiry


def est_vol_per_bar(prices: Sequence[float], window: int = 60) -> float:
    """Sample stdev of one-bar log returns over the tail of ``prices``.

    Matches the ``vol_per_bar`` unit used by :func:`probability_itm`.
    Returns 0.0 for flat or tiny series.
    """
    tail = [float(p) for p in list(prices)[-window:]]
    if len(tail) < 3:
        return 0.0
    rets = [
        math.log(max(tail[i], 1e-12) / max(tail[i - 1], 1e-12))
        for i in range(1, len(tail))
    ]
    return max(0.0, statistics.pstdev(rets))


def choose_expiry(
    side: str,
    spot: float,
    payout: float,
    closes: Sequence[float],
    candidates: Sequence[float],
    confidence: float = 0.6,
    default: Optional[float] = None,
) -> float:
    """Pick the candidate expiry with the best modeled edge.

    Falls back to ``default`` (or the first candidate) when the data is too
    thin to model or the side is unknown.  Deterministic — no RNG.
    """
    cands = [float(c) for c in candidates]
    fallback = float(default) if default is not None else (cands[0] if cands else 60.0)
    if side not in ("call", "put") or not cands or len(closes) < 3:
        return fallback
    vol = max(est_vol_per_bar(closes), 1e-9)
    conf = max(0.0, min(1.0, float(confidence)))
    drift = (conf - 0.5) * 2.0 * vol
    return best_expiry(side, spot, payout, cands, vol, drift, default=fallback)
