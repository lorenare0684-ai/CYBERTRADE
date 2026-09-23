"""Order-flow microstructure: tick imbalance, cumulative delta, volume profile.

Raw ticks carry information candles erase.  These tools rebuild the tape:
who is aggressing (imbalance), which side is absorbing (delta vs price), and
where value actually traded (volume profile with POC/VAH/VAL).  Pure
functions operate on plain sequences; :class:`TickFlow` is the streaming
tracker the engine maintains per asset.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from ..utils.mathx import clamp, safe_div


def tick_imbalance(prices: Sequence[float], window: int = 50) -> float:
    """Signed up/down tick ratio in [-1, 1] over the last ``window`` prints."""
    pts = list(prices)[-window - 1 :]
    if len(pts) < 2:
        return 0.0
    up = down = 0
    for i in range(1, len(pts)):
        if pts[i] > pts[i - 1]:
            up += 1
        elif pts[i] < pts[i - 1]:
            down += 1
    return clamp(safe_div(up - down, up + down, 0.0), -1.0, 1.0)


def cumulative_delta(prices: Sequence[float], sizes: Optional[Sequence[float]] = None) -> List[float]:
    """Running signed volume: +size on up-ticks, -size on down-ticks."""
    pts = list(prices)
    szs = list(sizes) if sizes is not None else [1.0] * len(pts)
    out: List[float] = []
    acc = 0.0
    for i in range(1, len(pts)):
        d = pts[i] - pts[i - 1]
        acc += szs[i] if d > 0 else (-szs[i] if d < 0 else 0.0)
        out.append(acc)
    return out


def delta_divergence(prices: Sequence[float], sizes: Optional[Sequence[float]] = None,
                     window: int = 30) -> float:
    """Price-extremes vs delta-extremes disagreement in [-1, 1].

    Price presses a new high while delta fails to confirm → negative value
    (absorption / seller ambush); the mirror for lows.  0 = agreement.
    """
    pts = list(prices)[-window - 1 :]
    if len(pts) < 4:
        return 0.0
    szs = list(sizes)[-len(pts):] if sizes is not None else None
    delta = cumulative_delta(pts, szs)
    if not delta:
        return 0.0
    half = len(pts) // 2
    px_first, px_last = max(pts[:half]), max(pts[half:])
    d_first, d_last = max(delta[: len(delta) // 2]), max(delta[len(delta) // 2:])
    bear_div = px_last > px_first and d_last < d_first
    px_first_lo, px_last_lo = min(pts[:half]), min(pts[half:])
    d_first_lo, d_last_lo = min(delta[: len(delta) // 2]), min(delta[len(delta) // 2:])
    bull_div = px_last_lo < px_first_lo and d_last_lo > d_first_lo
    if bear_div and not bull_div:
        return -1.0
    if bull_div and not bear_div:
        return 1.0
    return 0.0


@dataclass
class VolumeProfile:
    """Histogram of traded volume by price bucket."""

    buckets: Dict[float, float] = field(default_factory=lambda: defaultdict(float))
    bucket_size: float = 0.0005

    def add(self, price: float, size: float = 1.0) -> None:
        key = round(price / self.bucket_size) * self.bucket_size
        self.buckets[key] += size

    @property
    def poc(self) -> float:
        """Point of control — the price bucket with the most volume."""
        if not self.buckets:
            return 0.0
        return max(self.buckets.items(), key=lambda kv: kv[1])[0]

    def value_area(self, coverage: float = 0.70) -> Tuple[float, float]:
        """(VAL, VAH) — the bucket range covering ``coverage`` of volume."""
        if not self.buckets:
            return 0.0, 0.0
        total = sum(self.buckets.values())
        target = total * clamp(coverage, 0.0, 1.0)
        ordered = sorted(self.buckets.items(), key=lambda kv: -kv[1])
        chosen = []
        acc = 0.0
        for price, vol in ordered:
            chosen.append(price)
            acc += vol
            if acc >= target:
                break
        return min(chosen), max(chosen)

    def stretch(self, price: float, coverage: float = 0.70) -> float:
        """How far price is from POC in units of the value-area width (signed)."""
        val, vah = self.value_area(coverage)
        poc = self.poc
        if poc == 0.0 or vah <= val:
            return 0.0
        half = max((vah - val) / 2.0, self.bucket_size)
        return clamp((price - poc) / half, -3.0, 3.0)


class TickFlow:
    """Streaming flow tracker for one asset (thread-safe)."""

    def __init__(self, max_ticks: int = 2000, profile_buckets: int = 120) -> None:
        self._prices: Deque[float] = deque(maxlen=max_ticks)
        self._sizes: Deque[float] = deque(maxlen=max_ticks)
        self._lock = threading.RLock()
        self.profile = VolumeProfile()
        self._delta = 0.0

    def on_tick(self, price: float, size: float = 1.0) -> None:
        with self._lock:
            if self._prices:
                d = price - self._prices[-1]
                self._delta += size if d > 0 else (-size if d < 0 else 0.0)
            self._prices.append(price)
            self._sizes.append(size)
            self.profile.add(price, size)

    def imbalance(self, window: int = 50) -> float:
        with self._lock:
            return tick_imbalance(list(self._prices), window)

    @property
    def delta(self) -> float:
        return self._delta

    def divergence(self, window: int = 30) -> float:
        with self._lock:
            return delta_divergence(list(self._prices), list(self._sizes), window)

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            price = self._prices[-1] if self._prices else 0.0
            val, vah = self.profile.value_area()
            return {
                "imbalance": round(self.imbalance(), 3),
                "delta": round(self._delta, 2),
                "divergence": round(self.divergence(), 3),
                "poc": self.profile.poc,
                "val": val,
                "vah": vah,
                "stretch": round(self.profile.stretch(price), 3),
            }


__all__ = [
    "tick_imbalance",
    "cumulative_delta",
    "delta_divergence",
    "VolumeProfile",
    "TickFlow",
]
