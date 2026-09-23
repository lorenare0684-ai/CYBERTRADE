"""Divergence detection: price vs oscillator disagreement at swing pivots.

Regular bullish: price lower low + oscillator higher low  → upside reversal.
Regular bearish: price higher high + oscillator lower high → downside reversal.
Hidden bullish:  price higher low  + oscillator lower low  → trend continuation up.
Hidden bearish:  price lower high  + oscillator higher high → trend continuation down.

Pivot confirmation needs ``right`` bars, so detections lag the pivot by design
(no lookahead — signals can never peek at the future).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from ..utils.mathx import clamp, safe_div

BULL_REG = "regular_bull"
BEAR_REG = "regular_bear"
BULL_HID = "hidden_bull"
BEAR_HID = "hidden_bear"


@dataclass(frozen=True)
class Pivot:
    index: int
    price: float
    is_high: bool


@dataclass(frozen=True)
class Divergence:
    kind: str                 # one of the four constants
    confirm_index: int        # bar where the divergence became confirmable
    price_index: int          # pivot bar on the price series
    osc_index: int            # pivot bar on the oscillator series
    strength: float           # 0..1

    @property
    def is_bull(self) -> bool:
        return self.kind in (BULL_REG, BULL_HID)

    @property
    def is_regular(self) -> bool:
        return self.kind in (BULL_REG, BEAR_REG)

    @property
    def side(self) -> str:
        return "call" if self.is_bull else "put"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "confirm_index": self.confirm_index,
            "price_index": self.price_index,
            "osc_index": self.osc_index,
            "strength": round(self.strength, 3),
            "side": self.side,
        }


def find_pivots(
    values: Sequence[float],
    left: int = 3,
    right: int = 3,
    *,
    is_high: bool,
) -> List[Pivot]:
    """Locate confirmed swing highs (or lows).

    A pivot at ``i`` requires ``left`` lower highs (for highs) before it and
    ``right`` after it; it is only *known* at ``i + right``.
    """
    n = len(values)
    out: List[Pivot] = []
    for i in range(left, n - right):
        window = values[i - left : i + right + 1]
        center = values[i]
        others = window[:left] + window[left + 1 :]
        if is_high and center > max(others):
            out.append(Pivot(index=i, price=center, is_high=True))
        elif not is_high and center < min(others):
            out.append(Pivot(index=i, price=center, is_high=False))
    return out


def detect_divergence(
    close: Sequence[float],
    oscillator: Sequence[Optional[float]],
    left: int = 3,
    right: int = 3,
    max_gap: int = 8,
    lookback: int = 120,
) -> List[Divergence]:
    """Find divergence events between price pivots and oscillator pivots.

    ``oscillator`` may contain warmup ``None``s.  Returns events in
    chronological order of *confirmation* (pivot + right).
    """
    n = min(len(close), len(oscillator))
    if n < left + right + 2:
        return []
    start = max(0, n - lookback)
    closes = list(close[start:n])
    osc_raw = list(oscillator[start:n])
    base = start

    # oscillator pivots need numeric values — treat None as skipped
    osc_num = [0.0 if v is None else float(v) for v in osc_raw]
    valid = [i for i, v in enumerate(osc_raw) if v is not None]

    price_highs = find_pivots(closes, left, right, is_high=True)
    price_lows = find_pivots(closes, left, right, is_high=False)
    osc_highs = [p for p in find_pivots(osc_num, left, right, is_high=True) if p.index in set(valid)]
    osc_lows = [p for p in find_pivots(osc_num, left, right, is_high=False) if p.index in set(valid)]

    events: List[Divergence] = []

    def pair(p_list: List[Pivot], o_list: List[Pivot]) -> List[Tuple[Pivot, Pivot]]:
        pairs = []
        for pp in p_list:
            # nearest oscillator pivot of the same kind within max_gap bars
            candidates = [oo for oo in o_list if abs(oo.index - pp.index) <= max_gap]
            if not candidates:
                continue
            best = min(candidates, key=lambda oo: abs(oo.index - pp.index))
            pairs.append((pp, best))
        return pairs

    for pp, oo in pair(price_lows, osc_lows):
        price_down = pp.price < _prev_pivot_price(price_lows, pp)
        osc_up = oo.price > _prev_pivot_price(osc_lows, oo)
        price_up = pp.price > _prev_pivot_price(price_lows, pp)
        osc_down = oo.price < _prev_pivot_price(osc_lows, oo)
        if price_down and osc_up:
            events.append(
                Divergence(BULL_REG, pp.index + right, pp.index, oo.index,
                           _strength(pp, oo, price_lows, osc_lows, lower=True))
            )
        elif price_up and osc_down:
            events.append(
                Divergence(BULL_HID, pp.index + right, pp.index, oo.index,
                           _strength(pp, oo, price_lows, osc_lows, lower=True))
            )

    for pp, oo in pair(price_highs, osc_highs):
        price_up = pp.price > _prev_pivot_price(price_highs, pp)
        osc_down = oo.price < _prev_pivot_price(osc_highs, oo)
        price_down = pp.price < _prev_pivot_price(price_highs, pp)
        osc_up = oo.price > _prev_pivot_price(osc_highs, oo)
        if price_up and osc_down:
            events.append(
                Divergence(BEAR_REG, pp.index + right, pp.index, oo.index,
                           _strength(pp, oo, price_highs, osc_highs, lower=False))
            )
        elif price_down and osc_up:
            events.append(
                Divergence(BEAR_HID, pp.index + right, pp.index, oo.index,
                           _strength(pp, oo, price_highs, osc_highs, lower=False))
            )

    events.sort(key=lambda d: d.confirm_index)
    # convert indices back to caller space
    return [
        Divergence(d.kind, d.confirm_index + base, d.price_index + base,
                   d.osc_index + base, d.strength)
        for d in events
    ]


def last_divergence(
    close: Sequence[float],
    oscillator: Sequence[Optional[float]],
    left: int = 3,
    right: int = 3,
    within: int = 10,
) -> Optional[Divergence]:
    """Most recent divergence confirmed within ``within`` bars of the end."""
    events = detect_divergence(close, oscillator, left, right)
    if not events:
        return None
    end = len(close) - 1
    recent = [e for e in events if end - e.confirm_index <= within]
    return recent[-1] if recent else None


def divergence_score(
    close: Sequence[float],
    oscillator: Sequence[Optional[float]],
    left: int = 3,
    right: int = 3,
    within: int = 10,
) -> float:
    """Signed blend in [-1, 1]: positive = bullish disagreement."""
    d = last_divergence(close, oscillator, left, right, within)
    if d is None:
        return 0.0
    sign = 1.0 if d.is_bull else -1.0
    # regular divergences outweigh hidden ones
    weight = 1.0 if d.is_regular else 0.6
    return clamp(sign * weight * d.strength, -1.0, 1.0)


def _prev_pivot_price(pivots: List[Pivot], current: Pivot) -> float:
    prior = [p for p in pivots if p.index < current.index]
    return prior[-1].price if prior else current.price


def _strength(
    pp: Pivot,
    oo: Pivot,
    price_pivots: List[Pivot],
    osc_pivots: List[Pivot],
    lower: bool,
) -> float:
    """Score the disagreement: slope divergence + proximity of pivot pair."""
    prev_p = _prev_pivot_price(price_pivots, pp)
    prev_o = _prev_pivot_price(osc_pivots, oo)
    dp = abs(pp.price - prev_p) / max(abs(prev_p), 1e-12)
    do = abs(oo.price - prev_o) / max(abs(prev_o), 1e-12)
    slope_score = clamp((dp + do) * 40.0, 0.0, 1.0)
    gap_score = clamp(1.0 - abs(pp.index - oo.index) / 8.0, 0.0, 1.0)
    return clamp(0.5 * slope_score + 0.5 * gap_score, 0.05, 1.0)


__all__ = [
    "Pivot",
    "Divergence",
    "BULL_REG",
    "BEAR_REG",
    "BULL_HID",
    "BEAR_HID",
    "find_pivots",
    "detect_divergence",
    "last_divergence",
    "divergence_score",
]
