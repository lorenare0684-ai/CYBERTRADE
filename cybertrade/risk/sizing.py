"""Position sizing models for fixed-payout binaries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ..utils.mathx import clamp, kelly_fraction


@dataclass
class SizingDecision:
    stake: float
    model: str
    raw: float
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "stake": round(self.stake, 2),
            "model": self.model,
            "raw": round(self.raw, 4),
            "notes": self.notes,
        }


def fixed_fraction(
    balance: float, fraction: float, min_stake: float, max_stake: float
) -> SizingDecision:
    raw = balance * fraction
    stake = clamp(raw, min_stake, min(max_stake, max(balance * 0.5, min_stake)))
    return SizingDecision(stake=stake, model="fixed_fraction", raw=raw)


def vol_scaled(
    balance: float,
    fraction: float,
    realized_vol: float,
    target_vol: float,
    min_stake: float,
    max_stake: float,
    floor_scale: float = 0.35,
    cap_scale: float = 1.25,
) -> SizingDecision:
    """Shrink size when realized vol exceeds the target; never zero."""
    raw = balance * fraction
    if realized_vol <= 0 or target_vol <= 0:
        scale = 1.0
    else:
        scale = clamp(target_vol / realized_vol, floor_scale, cap_scale)
    stake = clamp(raw * scale, min_stake, max_stake)
    return SizingDecision(
        stake=stake,
        model="vol_scaled",
        raw=raw * scale,
        notes=f"scale={scale:.2f} rv={realized_vol:.4f}",
    )


def kelly_scaled(
    balance: float,
    win_rate: float,
    payout: float,
    min_stake: float,
    max_stake: float,
    kelly_mult: float = 0.25,
) -> SizingDecision:
    """Fractional Kelly on the binary payout structure."""
    full = kelly_fraction(win_rate, payout)
    raw = balance * full * kelly_mult
    stake = clamp(raw, min_stake, max_stake)
    return SizingDecision(
        stake=stake,
        model="kelly",
        raw=raw,
        notes=f"kelly={full:.3f} mult={kelly_mult:.2f}",
    )


def confidence_scaled(
    base: float, confidence: float, min_scale: float = 0.5, max_scale: float = 1.5
) -> float:
    """Scale a stake by signal confidence around 0.6 as neutral."""
    scale = clamp(min_scale + (confidence - 0.4) * (max_scale - min_scale) / 0.5, min_scale, max_scale)
    return max(0.0, base * scale)


def drawdown_scaled(base: float, drawdown: float, cap: float) -> float:
    """Linearly shrink stake as drawdown approaches its cap."""
    if cap <= 0:
        return base
    pressure = clamp(drawdown / cap, 0.0, 1.0)
    return max(0.0, base * (1.0 - 0.7 * pressure))


def stake_round(stake: float, step: float = 1.0) -> float:
    if step <= 0:
        return stake
    return math.floor(stake / step) * step


__all__ = [
    "SizingDecision",
    "fixed_fraction",
    "vol_scaled",
    "kelly_scaled",
    "confidence_scaled",
    "drawdown_scaled",
    "stake_round",
]
