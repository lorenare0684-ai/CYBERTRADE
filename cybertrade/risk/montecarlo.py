"""Monte Carlo risk lab: risk-of-ruin and equity confidence bands.

Resamples a trade P&L sequence with replacement thousands of times to answer
the only question that matters before going live: *what is the probability
this account dies, and how deep is the water at the 5th percentile?*
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..utils.mathx import clamp, max_drawdown, percentile


@dataclass
class MonteCarloReport:
    runs: int
    horizon: int
    starting_balance: float
    ruin_level: float
    risk_of_ruin: float
    risk_of_halving: float
    p05_terminal: float
    p50_terminal: float
    p95_terminal: float
    mean_terminal: float
    p05_min_equity: float
    median_max_drawdown: float
    p95_max_drawdown: float
    expectancy_per_trade: float
    p_edge_negative: float = 0.0
    bands: List[Dict[str, float]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runs": self.runs,
            "horizon": self.horizon,
            "starting_balance": round(self.starting_balance, 2),
            "ruin_level": round(self.ruin_level, 2),
            "risk_of_ruin": round(self.risk_of_ruin, 5),
            "risk_of_halving": round(self.risk_of_halving, 5),
            "p05_terminal": round(self.p05_terminal, 2),
            "p50_terminal": round(self.p50_terminal, 2),
            "p95_terminal": round(self.p95_terminal, 2),
            "mean_terminal": round(self.mean_terminal, 2),
            "p05_min_equity": round(self.p05_min_equity, 2),
            "median_max_drawdown": round(self.median_max_drawdown, 4),
            "p95_max_drawdown": round(self.p95_max_drawdown, 4),
            "expectancy_per_trade": round(self.expectancy_per_trade, 4),
            "p_edge_negative": round(self.p_edge_negative, 5),
            "bands": self.bands,
            "notes": self.notes,
        }

    def verdict(self) -> str:
        if self.p_edge_negative > 0.5:
            return "RUIN LIKELY — do not trade these stats live"
        if self.risk_of_ruin <= 0.01 and self.p05_terminal >= self.starting_balance * 0.8:
            return "SURVIVABLE"
        if self.risk_of_ruin <= 0.05:
            return "CAUTION"
        if self.risk_of_ruin <= 0.15:
            return "DANGEROUS"
        return "RUIN LIKELY — do not trade these stats live"

    def summary_text(self) -> str:
        return (
            f"runs {self.runs}×{self.horizon} trades | ruin {self.risk_of_ruin * 100:.2f}% "
            f"| halve {self.risk_of_halving * 100:.1f}% | P5/P50/P95 terminal "
            f"{self.p05_terminal:.0f}/{self.p50_terminal:.0f}/{self.p95_terminal:.0f} "
            f"| median DD {self.median_max_drawdown * 100:.1f}%"
            + (f" | P(edge<0) {self.p_edge_negative * 100:.1f}%"
               if self.p_edge_negative > 0 else "")
            + f" | {self.verdict()}"
        )


def simulate_posterior(
    wins: int,
    losses: int,
    payout: float,
    starting_balance: float = 1000.0,
    runs: int = 2000,
    horizon: int = 200,
    ruin_frac: float = 0.5,
    seed: int = 1337,
    stake_frac: float = 0.01,
) -> MonteCarloReport:
    """Forward-simulate the calibrated Beta posterior over P(win).

    Each run draws its own ``p_true ~ Beta(wins+2, losses+2)`` ONCE —
    parameter uncertainty dominates small samples — then plays ``horizon``
    constant-stake binary trades at ``payout``.  The headline number is
    ``p_edge_negative``: posterior mass below breakeven, i.e. the honest
    probability that this record is a losing strategy wearing a winner's
    clothes.  Fan bands are elided here (stats row is the substance).
    """
    wins = max(0, int(wins))
    losses = max(0, int(losses))
    if payout <= -1.0:
        raise ValueError("payout must be > -1")
    alpha = wins + 2.0
    beta = losses + 2.0
    breakeven = 1.0 / (1.0 + payout)
    rng = random.Random(seed)
    stake = max(1e-9, starting_balance * stake_frac)
    ruin_level = starting_balance * ruin_frac

    terminals: List[float] = []
    min_equities: List[float] = []
    drawdowns: List[float] = []
    edge_neg = 0
    expectancy = 0.0
    for _ in range(max(1, runs)):
        p_true = rng.betavariate(alpha, beta)
        if p_true < breakeven:
            edge_neg += 1
        expectancy += stake * (p_true * (1.0 + payout) - 1.0)
        equity = starting_balance
        peak = equity
        min_eq = equity
        max_dd = 0.0
        for _t in range(max(1, horizon)):
            equity += stake * payout if rng.random() < p_true else -stake
            if equity > peak:
                peak = equity
            if equity < min_eq:
                min_eq = equity
            dd = (peak - equity) / peak if peak > 0 else 1.0
            if dd > max_dd:
                max_dd = dd
        terminals.append(equity)
        min_equities.append(min_eq)
        drawdowns.append(max_dd)

    def pct(values: List[float], q: float) -> float:
        s = sorted(values)
        idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        return s[idx]

    n = max(1, runs)
    return MonteCarloReport(
        runs=n,
        horizon=max(1, horizon),
        starting_balance=starting_balance,
        ruin_level=ruin_level,
        risk_of_ruin=sum(1 for e in min_equities if e <= ruin_level) / n,
        risk_of_halving=sum(1 for e in terminals if e <= ruin_level) / n,
        p05_terminal=pct(terminals, 0.05),
        p50_terminal=pct(terminals, 0.50),
        p95_terminal=pct(terminals, 0.95),
        mean_terminal=sum(terminals) / n,
        p05_min_equity=pct(min_equities, 0.05),
        median_max_drawdown=pct(drawdowns, 0.50),
        p95_max_drawdown=pct(drawdowns, 0.95),
        expectancy_per_trade=expectancy / n,
        p_edge_negative=edge_neg / n,
        notes=[
            f"Beta({alpha:.0f},{beta:.0f}) posterior | breakeven {breakeven:.4f} "
            f"at payout {payout:.2f} | constant {stake_frac:.0%} stake | bands elided",
        ],
    )


def simulate(
    pnls: Sequence[float],
    starting_balance: float = 1000.0,
    runs: int = 2000,
    horizon: Optional[int] = None,
    ruin_frac: float = 0.5,
    band_points: int = 40,
    seed: int = 1337,
) -> MonteCarloReport:
    """Bootstrap the empirical P&L distribution forward ``horizon`` trades.

    ``ruin_frac`` sets the death line (default 50% of start — with fixed-fraction
    sizing, true zero is unreachable; halving is the practical ruin event).
    """
    pnls = [float(p) for p in pnls]
    n_obs = len(pnls)
    if n_obs == 0:
        return MonteCarloReport(
            runs=0, horizon=0, starting_balance=starting_balance,
            ruin_level=starting_balance * ruin_frac,
            risk_of_ruin=0.0, risk_of_halving=0.0,
            p05_terminal=starting_balance, p50_terminal=starting_balance,
            p95_terminal=starting_balance, mean_terminal=starting_balance,
            p05_min_equity=starting_balance,
            median_max_drawdown=0.0, p95_max_drawdown=0.0,
            expectancy_per_trade=0.0,
            notes=["no trades to resample — run a backtest or trade first"],
        )
    horizon = horizon or max(50, n_obs)
    ruin_level = starting_balance * ruin_frac
    rng = random.Random(seed)
    expectancy = sum(pnls) / n_obs

    terminals: List[float] = []
    min_equities: List[float] = []
    drawdowns: List[float] = []
    ruin = 0
    halved = 0
    stride = max(1, horizon // band_points)
    band_idx = list(range(0, horizon, stride))
    band_traces: List[List[float]] = [[] for _ in band_idx]

    for _ in range(max(1, runs)):
        equity = starting_balance
        peak = equity
        min_eq = equity
        max_dd = 0.0
        dead = False
        half = False
        for t in range(horizon):
            equity += pnls[rng.randrange(n_obs)]
            if equity < min_eq:
                min_eq = equity
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd
            if not half and equity <= starting_balance * 0.5:
                half = True
            if not dead and equity <= ruin_level:
                dead = True
            if t in _index_map(band_idx, t):
                pass
        # capture band snapshots in one pass below instead
        terminals.append(equity)
        min_equities.append(min_eq)
        drawdowns.append(max_dd)
        ruin += 1 if dead else 0
        halved += 1 if half else 0

    # second light pass for percentile bands (cheap: reuse rng stream ordering)
    rng2 = random.Random(seed)
    snapshots: List[List[float]] = [[] for _ in band_idx]
    for _ in range(max(1, min(runs, 400))):
        equity = starting_balance
        idx_map = {t: i for i, t in enumerate(band_idx)}
        for t in range(horizon):
            equity += pnls[rng2.randrange(n_obs)]
            if t in idx_map:
                snapshots[idx_map[t]].append(equity)

    bands: List[Dict[str, float]] = []
    for i, t in enumerate(band_idx):
        col = snapshots[i] or [starting_balance]
        bands.append({
            "t": t,
            "p05": percentile(col, 5),
            "p50": percentile(col, 50),
            "p95": percentile(col, 95),
        })

    notes: List[str] = []
    if n_obs < 30:
        notes.append(f"small sample ({n_obs} trades) — bands are wide on purpose")
    if expectancy < 0:
        notes.append("negative expectancy — Monte Carlo cannot save a losing edge")
    if ruin / max(1, runs) > 0.05:
        notes.append("risk of ruin above 5% — cut size or fix the edge before live")

    return MonteCarloReport(
        runs=runs,
        horizon=horizon,
        starting_balance=starting_balance,
        ruin_level=ruin_level,
        risk_of_ruin=ruin / max(1, runs),
        risk_of_halving=halved / max(1, runs),
        p05_terminal=percentile(terminals, 5),
        p50_terminal=percentile(terminals, 50),
        p95_terminal=percentile(terminals, 95),
        mean_terminal=sum(terminals) / len(terminals),
        p05_min_equity=percentile(min_equities, 5),
        median_max_drawdown=percentile(drawdowns, 50),
        p95_max_drawdown=percentile(drawdowns, 95),
        expectancy_per_trade=expectancy,
        bands=bands,
        notes=notes,
    )


def simulate_from_records(
    trades: Sequence[Any],
    starting_balance: float = 1000.0,
    **kw,
) -> MonteCarloReport:
    """Accept TradeRecord objects or dicts with a 'pnl' key."""
    pnls = []
    for t in trades:
        if isinstance(t, dict):
            pnls.append(float(t.get("pnl", 0.0)))
        else:
            pnls.append(float(getattr(t, "pnl", 0.0)))
    return simulate(pnls, starting_balance=starting_balance, **kw)


def _index_map(band_idx: List[int], t: int) -> List[int]:
    return [t] if t in band_idx else []


__all__ = ["MonteCarloReport", "simulate", "simulate_from_records"]
