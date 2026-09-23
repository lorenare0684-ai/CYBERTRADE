"""Confidence calibration: what a strategy *says* vs what it *does*.

Raw confidence is an opinion; the ledger is the fact.  The tracker buckets
per-strategy outcomes against claimed confidence and estimates the true
P(win) with Beta-Binomial shrinkage toward the no-knowledge prior (0.5),
blended with a shrunken raw confidence while evidence is thin.  The edge
gate then trades only when *calibrated* probability clears the payout hurdle.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..utils.mathx import clamp

BUCKET_EDGES: Tuple[float, ...] = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.01)
PRIOR_WEIGHT = 4.0        # Beta(2,2)-ish pseudo-observations at 0.5
RAW_BLEND = 20.0          # raw conf counts this much until a bucket matures
RAW_SHRINK = 0.5          # cold-start raw conf is pulled halfway to 0.5
REGIME_MIN_N = 4          # regime rows before regime evidence speaks
REGIME_PRIOR_N = 8.0      # weight of the strategy-level estimate as prior


def bucket_index(confidence: float) -> int:
    c = clamp(confidence, 0.0, 1.0)
    for i in range(len(BUCKET_EDGES) - 1):
        if BUCKET_EDGES[i] <= c < BUCKET_EDGES[i + 1]:
            return i
    return len(BUCKET_EDGES) - 2


@dataclass
class ReliabilityBucket:
    wins: int = 0
    total: int = 0

    def observe(self, won: bool) -> None:
        self.total += 1
        if won:
            self.wins += 1

    @property
    def hit_rate(self) -> float:
        return self.wins / self.total if self.total else 0.0

    def posterior(self) -> float:
        """Beta(2,2) shrinkage — a 1-0 bucket is NOT a 100% strategy."""
        return (self.wins + PRIOR_WEIGHT / 2) / (self.total + PRIOR_WEIGHT)

    def to_dict(self) -> Dict[str, Any]:
        return {"wins": self.wins, "total": self.total,
                "hit_rate": round(self.hit_rate, 4), "posterior": round(self.posterior(), 4)}


class CalibrationTracker:
    """Per-strategy reliability tables + blended P(win) estimator."""

    def __init__(self) -> None:
        self._by_strategy: Dict[str, List[ReliabilityBucket]] = {}
        self._by_regime: Dict[str, List[ReliabilityBucket]] = {}
        self._global = [ReliabilityBucket() for _ in range(len(BUCKET_EDGES) - 1)]
        self._lock = threading.RLock()
        self._observed = 0

    # -- learning ----------------------------------------------------------
    def observe(self, strategy: str, confidence: float, won: bool, regime: str = "") -> None:
        idx = bucket_index(confidence)
        with self._lock:
            table = self._by_strategy.setdefault(
                strategy, [ReliabilityBucket() for _ in range(len(BUCKET_EDGES) - 1)]
            )
            table[idx].observe(won)
            self._global[idx].observe(won)
            self._observed += 1
            if regime:
                rtable = self._by_regime.setdefault(
                    f"{strategy}|{regime}",
                    [ReliabilityBucket() for _ in range(len(BUCKET_EDGES) - 1)],
                )
                rtable[idx].observe(won)

    # -- estimation --------------------------------------------------------
    def p_for(self, strategy: str, confidence: float) -> float:
        """Blended P(win): bucket posterior weighted by evidence vs raw conf.

        Cold start (no rows): ``0.5 + RAW_SHRINK * (conf - 0.5)`` — opinions
        are worth something, but nothing like their face value.
        """
        raw_shrunk = 0.5 + RAW_SHRINK * (clamp(confidence, 0.0, 1.0) - 0.5)
        idx = bucket_index(confidence)
        with self._lock:
            own = self._by_strategy.get(strategy, [None] * (len(BUCKET_EDGES) - 1))[idx] \
                if strategy in self._by_strategy else None
            glob = self._global[idx]
        n_own = own.total if own else 0
        n_glob = glob.total
        if n_own == 0 and n_glob == 0:
            return clamp(raw_shrunk, 0.0, 1.0)
        # evidence blend: own table first, global prior as backup
        if n_own > 0:
            post = own.posterior()
            n = n_own
        else:
            post = glob.posterior()
            n = n_glob
        p = (n * post + RAW_BLEND * raw_shrunk) / (n + RAW_BLEND)
        return clamp(p, 0.0, 1.0)

    def observe_votes(self, votes, won: bool, regime: str = "") -> None:
        """Teach every voter behind a blended signal (shared outcome).

        Each subordinate strategy that voted 'called' this trade — its own
        reliability table deserves the datapoint, not just the ensemble blob.
        """
        if not votes:
            return
        for v in votes:
            try:
                strat = str(v.get("strategy", ""))
                conf = float(v.get("confidence", 0.5))
            except (TypeError, ValueError, AttributeError):
                continue
            if strat:
                self.observe(strat, conf, won, regime=regime)

    def p_regime(self, strategy: str, regime: str, confidence: float) -> float:
        """P(win) given the market regime — the WHEN matrix.

        A strategy can be lethal in ``bull_trend`` and toxic in ``range``;
        its unconditional record averages the two into a lie.  Regime rows
        shrink toward the strategy-level estimate (prior weight
        ``REGIME_PRIOR_N``) and stay silent below ``REGIME_MIN_N`` samples.
        """
        base = self.p_for(strategy, confidence)
        if not regime:
            return base
        idx = bucket_index(confidence)
        key = f"{strategy}|{regime}"
        with self._lock:
            table = self._by_regime.get(key)
            bucket = table[idx] if table else None
        n = bucket.total if bucket else 0
        if n < REGIME_MIN_N:
            return base
        p = (n * bucket.posterior() + REGIME_PRIOR_N * base) / (n + REGIME_PRIOR_N)
        return clamp(p, 0.0, 1.0)

    def regime_rows(self) -> int:
        """How many (strategy, regime) tables hold any evidence."""
        with self._lock:
            return sum(
                1 for t in self._by_regime.values() if any(b.total for b in t)
            )

    def evidence(self):
        """Aggregate (wins, losses) across all strategy tables."""
        wins = total = 0
        with self._lock:
            for b in self._global:
                wins += b.wins
                total += b.total
        return wins, total - wins

    def evidence_for(self, strategy: str):
        """Aggregate (wins, losses) for one strategy's own table."""
        with self._lock:
            table = self._by_strategy.get(strategy)
        if not table:
            return 0, 0
        wins = total = 0
        for b in table:
            wins += b.wins
            total += b.total
        return wins, total - wins

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "observed": self._observed,
                "global": [{"wins": b.wins, "total": b.total} for b in self._global],
                "by_strategy": {
                    k: [{"wins": b.wins, "total": b.total} for b in t]
                    for k, t in self._by_strategy.items()
                },
                "by_regime": {
                    k: [{"wins": b.wins, "total": b.total} for b in t]
                    for k, t in self._by_regime.items()
                },
            }

    def update_from(self, data: Dict[str, Any]) -> None:
        """Replace tracker state from a :meth:`to_dict` payload."""

        def _buckets(rows) -> List[ReliabilityBucket]:
            out: List[ReliabilityBucket] = []
            for r in rows or []:
                try:
                    out.append(ReliabilityBucket(
                        wins=max(0, int(r.get("wins", 0))),
                        total=max(0, int(r.get("total", 0))),
                    ))
                except (TypeError, ValueError, AttributeError):
                    out.append(ReliabilityBucket())
            need = len(BUCKET_EDGES) - 1
            out = out[:need] + [ReliabilityBucket() for _ in range(max(0, need - len(out)))]
            return out

        with self._lock:
            self._global = _buckets(data.get("global"))
            self._by_strategy = {
                str(k): _buckets(v)
                for k, v in (data.get("by_strategy") or {}).items()
            }
            self._by_regime = {
                str(k): _buckets(v)
                for k, v in (data.get("by_regime") or {}).items()
            }
            self._observed = int(data.get("observed", 0)) or sum(
                b.total for b in self._global
            )

    def save(self, path: str) -> bool:
        """Persist the ledger atomically.  Refuses to clobber real data with
        an empty session (a crashed boot must not erase the lessons)."""
        import json
        import os

        with self._lock:
            empty = self._observed == 0
        if empty and os.path.exists(path):
            return False
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True

    def load(self, path: str) -> bool:
        """Replace state from a saved ledger.  ``False`` = file unreadable."""
        import json

        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        self.update_from(data)
        return True

    # -- honesty ledger ----------------------------------------------------
    def honesty(self, payout: float = 0.85, runs: int = 800, seed: int = 1337):
        """Per-strategy liar detection: posterior mass below breakeven.

        Each row samples ``P(win) ~ Beta(wins+2, losses+2)`` and measures how
        much of that mass sits under ``1/(1+payout)``.  ``liar`` fires past
        50% — the record is more likely a losing strategy in a winner's
        clothes.  Rows sort worst-first; zero-evidence rows share one draw.
        """
        import random as _random

        breakeven = 1.0 / (1.0 + payout) if payout > -1.0 else 1.0
        with self._lock:
            items = list(self._by_strategy.items())
        n_draw = max(50, int(runs))

        def _p_edge(wins: int, losses: int, label: str) -> float:
            rng = _random.Random(f"{seed}:{label}:{wins}:{losses}:{payout}")
            hits = 0
            for _ in range(n_draw):
                if rng.betavariate(wins + 2.0, losses + 2.0) < breakeven:
                    hits += 1
            return hits / n_draw

        rows: List[Dict[str, Any]] = []
        prior_p: Dict[tuple, float] = {}
        for name, table in items:
            wins = sum(b.wins for b in table)
            total = sum(b.total for b in table)
            losses = total - wins
            if total == 0:
                key = (0, 0)
                if key not in prior_p:
                    prior_p[key] = _p_edge(0, 0, "prior")
                p_edge = prior_p[key]
            else:
                p_edge = _p_edge(wins, losses, name)
            rows.append({
                "strategy": name,
                "wins": wins,
                "losses": losses,
                "n": total,
                "hit_rate": round(wins / total, 4) if total else 0.0,
                "p_edge_negative": round(p_edge, 4),
                "liar": p_edge > 0.5,
            })
        rows.sort(key=lambda r: (-r["p_edge_negative"], -r["n"]))
        return rows

    def p_win_for(
        self, strategy: str, confidence: float, votes=None, regime: str = ""
    ) -> float:
        """Evidence-based P(win) for a blended signal.

        Mean of per-voter posteriors, shrunk toward the blob-level estimate
        (weight 2) so a lone opinionated voter cannot hijack the gate — then
        taken as the MINIMUM of that blend and the blob estimate: voter
        evidence may only *lower* the gate, never paper over a strategy the
        ensemble's own record has discredited.  When ``regime`` is set, every
        estimate goes through :meth:`p_regime` (the WHEN matrix).
        Falls back to :meth:`p_for` when no votes exist.
        """
        est = self.p_regime if regime else self.p_for

        def _one(name: str, conf: float) -> float:
            return est(name, regime, conf) if regime else self.p_for(name, conf)

        base = _one(strategy, confidence)
        if not votes:
            return base
        ps: List[float] = []
        for v in votes:
            try:
                strat = str(v.get("strategy", ""))
                conf = float(v.get("confidence", 0.5))
            except (TypeError, ValueError, AttributeError):
                continue
            if strat:
                ps.append(_one(strat, conf))
        if not ps:
            return base
        voter_p = sum(ps) / len(ps)
        blend = (len(ps) * voter_p + 2.0 * base) / (len(ps) + 2.0)
        return clamp(min(base, blend), 0.0, 1.0)

    def calibration_gap(self) -> float:
        """Mean |claimed - actual| across mature buckets (0 = perfectly honest)."""
        gaps: List[float] = []
        with self._lock:
            for i, b in enumerate(self._global):
                if b.total >= 8:
                    claimed = 0.5 * (BUCKET_EDGES[i] + BUCKET_EDGES[i + 1])
                    gaps.append(abs(claimed - b.hit_rate))
        return sum(gaps) / len(gaps) if gaps else 0.0

    # -- introspection -----------------------------------------------------
    def summary(self, payout: float = 0.85) -> Dict[str, Any]:
        with self._lock:
            rows = []
            for name, table in sorted(self._by_strategy.items()):
                total = sum(b.total for b in table)
                wins = sum(b.wins for b in table)
                rows.append({
                    "strategy": name,
                    "n": total,
                    "hit_rate": round(wins / total, 4) if total else 0.0,
                    "best_bucket": max(
                        (b.posterior() for b in table if b.total), default=0.5
                    ),
                })
            g_wins = sum(b.wins for b in self._global)
            g_total = sum(b.total for b in self._global)
            return {
                "observed": self._observed,
                "calibration_gap": round(self.calibration_gap(), 4),
                "evidence": [g_wins, g_total - g_wins],
                "honesty": self.honesty(payout),
                "strategies": rows,
            }

    @property
    def observations(self) -> int:
        return self._observed


__all__ = ["CalibrationTracker", "ReliabilityBucket", "bucket_index", "BUCKET_EDGES"]
