"""Cross-asset correlation monitoring for cluster-aware risk.

Tracks rolling return correlations between every watched asset and groups
them into correlation clusters (union-find at |corr| >= threshold).  The OMS
uses the cluster id for exposure caps so two "different" EUR pairs cannot
triple the same underlying risk.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Sequence, Set, Tuple

from ..utils.mathx import correlation


class CorrelationMonitor:
    """Rolling pairwise correlation + cluster labels."""

    def __init__(self, window: int = 120, cluster_threshold: float = 0.6) -> None:
        self.window = window
        self.cluster_threshold = cluster_threshold
        self._prices: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=window + 1))
        self._returns: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self._clusters: Dict[str, str] = {}
        self._corr_cache: Dict[Tuple[str, str], float] = {}
        self._lock = threading.RLock()

    # -- data --------------------------------------------------------------
    def on_price(self, asset: str, price: float) -> None:
        if price <= 0:
            return
        with self._lock:
            dq = self._prices[asset]
            if dq:
                prev = dq[-1]
                if prev > 0 and price != prev:
                    self._returns[asset].append((price - prev) / prev)
            dq.append(price)

    def on_closes(self, asset: str, closes: Sequence[float]) -> None:
        """Bulk-load closes (warmup/backtest)."""
        for px in closes:
            self.on_price(asset, px)

    # -- math --------------------------------------------------------------
    def correlation(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        with self._lock:
            ra = list(self._returns.get(a, ()))
            rb = list(self._returns.get(b, ()))
        n = min(len(ra), len(rb))
        if n < 10:
            return 0.0
        return correlation(ra[-n:], rb[-n:])

    def matrix(self, assets: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, float]]:
        assets = list(assets or sorted(self._returns.keys()))
        return {a: {b: round(self.correlation(a, b), 3) for b in assets} for a in assets}

    # -- clustering --------------------------------------------------------
    def clusters(self, assets: Optional[Sequence[str]] = None,
                 threshold: Optional[float] = None) -> Dict[str, str]:
        """Union-find clusters: {asset: cluster_id} where cluster_id is the
        lexicographically smallest member (assets ride alone when uncorrelated)."""
        thr = self.cluster_threshold if threshold is None else threshold
        assets = list(assets or sorted(self._returns.keys()))
        parent = {a: a for a in assets}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: str, y: str) -> None:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[max(rx, ry)] = min(rx, ry)

        for i, a in enumerate(assets):
            for b in assets[i + 1 :]:
                if abs(self.correlation(a, b)) >= thr:
                    union(a, b)

        with self._lock:
            self._clusters = {a: find(a) for a in assets}
        return dict(self._clusters)

    def cluster_of(self, asset: str) -> str:
        """Current cluster label for one asset (currency-style fallback)."""
        with self._lock:
            if asset in self._clusters:
                return self._clusters[asset]
        # fallback: quote-currency bucket until enough data accrues
        name = asset.replace("_otc", "")
        for quote in ("USD", "EUR", "GBP", "JPY"):
            if name.endswith(quote):
                return quote
        return name[:3]

    def cluster_exposure(self, cluster: str, open_by_asset: Dict[str, int]) -> int:
        total = 0
        for asset, n in open_by_asset.items():
            if self.cluster_of(asset) == cluster:
                total += n
        return total

    def hottest_pair(self) -> Optional[Tuple[str, str, float]]:
        """The most correlated pair currently tracked."""
        assets = sorted(self._returns.keys())
        best: Optional[Tuple[str, str, float]] = None
        for i, a in enumerate(assets):
            for b in assets[i + 1 :]:
                c = self.correlation(a, b)
                if best is None or abs(c) > abs(best[2]):
                    best = (a, b, c)
        return best

    def summary(self) -> Dict[str, object]:
        with self._lock:
            clusters = dict(self._clusters)
        hot = self.hottest_pair()
        return {
            "assets": sorted(self._returns.keys()),
            "clusters": clusters,
            "hottest_pair": (
                {"pair": f"{hot[0]}/{hot[1]}", "corr": round(hot[2], 3)} if hot else None
            ),
            "threshold": self.cluster_threshold,
            "window": self.window,
        }


__all__ = ["CorrelationMonitor"]
