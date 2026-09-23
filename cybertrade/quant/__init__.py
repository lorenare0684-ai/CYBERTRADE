"""Quant core: binary-option edge math + probability calibration.

The payout hurdle is the master law of binary options: at payout ``b`` a
trade is break-even only when ``P(win) >= 1/(1+b)`` (54.05% at 0.85).  Every
gate, size, and strategy in the terminal ultimately answers to that number —
this package keeps the arithmetic honest and in one place.
"""

from __future__ import annotations

from .binary import (
    breakeven_winrate,
    edge_of,
    expiry_edge_profile,
    kelly_fraction_for,
    kelly_stake,
    probability_itm,
    required_winrate,
)
from .calibration import CalibrationTracker, ReliabilityBucket

__all__ = [
    "breakeven_winrate",
    "required_winrate",
    "edge_of",
    "kelly_fraction_for",
    "kelly_stake",
    "probability_itm",
    "expiry_edge_profile",
    "CalibrationTracker",
    "ReliabilityBucket",
]
