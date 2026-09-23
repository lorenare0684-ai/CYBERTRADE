"""Journal analytics: streaks, regime breakdowns, decay detection."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from ..utils.mathx import max_drawdown, mean
from .store import TradeJournal


def regime_breakdown(journal: TradeJournal) -> List[Dict[str, Any]]:
    rows = journal.trades(limit=10000)
    buckets: Dict[str, Dict[str, float]] = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for row in rows:
        b = buckets[row.get("regime") or "unknown"]
        b["n"] += 1
        b["w"] += 1 if row["won"] else 0
        b["pnl"] += float(row["pnl"])
    out = []
    for regime, b in buckets.items():
        out.append(
            {
                "regime": regime,
                "trades": int(b["n"]),
                "win_rate": (b["w"] / b["n"]) if b["n"] else 0.0,
                "pnl": round(b["pnl"], 2),
            }
        )
    out.sort(key=lambda r: r["pnl"], reverse=True)
    return out


def asset_breakdown(journal: TradeJournal) -> List[Dict[str, Any]]:
    rows = journal.trades(limit=10000)
    buckets: Dict[str, Dict[str, float]] = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for row in rows:
        b = buckets[row["asset"]]
        b["n"] += 1
        b["w"] += 1 if row["won"] else 0
        b["pnl"] += float(row["pnl"])
    return [
        {
            "asset": asset,
            "trades": int(b["n"]),
            "win_rate": (b["w"] / b["n"]) if b["n"] else 0.0,
            "pnl": round(b["pnl"], 2),
        }
        for asset, b in sorted(buckets.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
    ]


def streaks(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    best_win = best_loss = cur_win = cur_loss = 0
    for row in reversed(rows):  # chronological
        if row["won"]:
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        best_win = max(best_win, cur_win)
        best_loss = max(best_loss, cur_loss)
    return {"best_win_streak": best_win, "worst_loss_streak": best_loss}


def decay_check(journal: TradeJournal, window: int = 20) -> Dict[str, Any]:
    """Has a strategy lost its edge recently? Compare last windows."""
    rows = journal.trades(limit=400)
    if len(rows) < window * 2:
        return {"warning": "insufficient history for decay analysis"}
    recent = rows[:window]
    older = rows[window : window * 2]

    def wr(chunk):
        wins = sum(1 for r in chunk if r["won"])
        return wins / len(chunk) if chunk else 0.0

    recent_wr, older_wr = wr(recent), wr(older)
    delta = recent_wr - older_wr
    return {
        "recent_win_rate": round(recent_wr, 4),
        "prior_win_rate": round(older_wr, 4),
        "delta": round(delta, 4),
        "decaying": delta < -0.12,
        "advice": "quarantine or re-tune" if delta < -0.12 else "stable",
    }


def journal_report(journal: TradeJournal) -> Dict[str, Any]:
    rows = journal.trades(limit=10000)
    if not rows:
        return {"empty": True, "message": "journal has no closed trades yet"}
    total_pnl = sum(float(r["pnl"]) for r in rows)
    wins = sum(1 for r in rows if r["won"])
    curve = journal.equity_curve()
    equity = [1000 + p for _, p in curve]
    return {
        "trades": len(rows),
        "wins": wins,
        "win_rate": round(wins / len(rows), 4),
        "net_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_drawdown(equity), 4),
        "avg_pnl": round(mean([float(r["pnl"]) for r in rows]), 3),
        "streaks": streaks(rows),
        "by_strategy": journal.strategy_stats(),
        "by_regime": regime_breakdown(journal),
        "by_asset": asset_breakdown(journal),
        "decay": decay_check(journal),
    }


def strategy_decay(journal: TradeJournal, window: int = 10) -> List[Dict[str, Any]]:
    """Per-strategy recent-vs-prior win rate — the stale-edge detector.

    A strategy that WAS working and is now fading deserves a flag before the
    pooled record notices (``decay_check`` averages everyone together).
    Rows need ``window`` recent + ``window`` prior trades to speak; worst
    deltas first.
    """
    rows = journal.trades(limit=400)  # newest first
    by: Dict[str, List[bool]] = defaultdict(list)
    for r in rows:
        by[str(r.get("strategy") or "unknown")].append(bool(r["won"]))
    out: List[Dict[str, Any]] = []
    for name, outcomes in by.items():
        recent = outcomes[:window]
        prior = outcomes[window: window * 2]
        if len(recent) < window or len(prior) < window:
            continue
        recent_wr = sum(recent) / len(recent)
        prior_wr = sum(prior) / len(prior)
        delta = recent_wr - prior_wr
        out.append({
            "strategy": name,
            "recent_win_rate": round(recent_wr, 4),
            "prior_win_rate": round(prior_wr, 4),
            "delta": round(delta, 4),
            "decaying": delta < -0.12,
            "n_recent": len(recent),
            "n_prior": len(prior),
        })
    out.sort(key=lambda r: r["delta"])
    return out


__all__ = [
    "regime_breakdown",
    "asset_breakdown",
    "streaks",
    "decay_check",
    "strategy_decay",
    "journal_report",
]
