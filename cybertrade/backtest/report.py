"""Performance analytics and survival scoring for backtests."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..data.models import TradeRecord, summarize_trades
from ..utils.mathx import max_drawdown, profit_factor, sharpe, sortino


@dataclass
class BacktestReport:
    starting_balance: float
    final_balance: float
    net_pnl: float
    return_pct: float
    trades: int
    wins: int
    losses: int
    refunds: int
    win_rate: float
    profit_factor: Optional[float]
    max_drawdown: float
    sharpe: float
    sortino: float
    avg_win: float
    avg_loss: float
    expectancy: float
    longest_loss_streak: int
    longest_win_streak: int
    signals_total: int
    vetoes: int
    exposure: float
    ulcer_index: float
    recovery_factor: float
    survival_score: float
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "starting_balance": round(self.starting_balance, 2),
            "final_balance": round(self.final_balance, 2),
            "net_pnl": round(self.net_pnl, 2),
            "return_pct": round(self.return_pct, 2),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "refunds": self.refunds,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": (round(self.profit_factor, 3)
                              if self.profit_factor is not None and math.isfinite(self.profit_factor)
                              else None),
            "max_drawdown": round(self.max_drawdown, 4),
            "sharpe": round(self.sharpe, 3),
            "sortino": round(self.sortino, 3),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "expectancy": round(self.expectancy, 3),
            "longest_loss_streak": self.longest_loss_streak,
            "longest_win_streak": self.longest_win_streak,
            "signals_total": self.signals_total,
            "vetoes": self.vetoes,
            "exposure": round(self.exposure, 4),
            "ulcer_index": round(self.ulcer_index, 3),
            "recovery_factor": round(self.recovery_factor, 3),
            "survival_score": round(self.survival_score, 3),
            "notes": self.notes,
        }

    def grade(self) -> str:
        s = self.survival_score
        if s >= 0.85:
            return "S"
        if s >= 0.7:
            return "A"
        if s >= 0.55:
            return "B"
        if s >= 0.4:
            return "C"
        if s >= 0.25:
            return "D"
        return "F"

    def summary_text(self) -> str:
        return (
            f"bal {self.final_balance:.2f} ({self.return_pct:+.2f}%) | "
            f"WR {self.win_rate * 100:.1f}% over {self.trades} | "
            f"DD {self.max_drawdown * 100:.1f}% | PF "
            f"{self.profit_factor if self.profit_factor and math.isfinite(self.profit_factor) else 'inf':} "
            f"| survival {self.survival_score:.2f} [{self.grade()}]"
        )


def build_report(
    trades: Sequence[TradeRecord],
    equity_curve: Sequence[float],
    starting_balance: float,
    final_balance: float,
    signals_total: int,
    vetoes: int,
) -> BacktestReport:
    summary = summarize_trades(trades)
    returns = _curve_returns(equity_curve)
    dd = max_drawdown(equity_curve) if equity_curve else 0.0
    pf_raw = profit_factor(summary["gross_win"], summary["gross_loss"])
    pf = pf_raw if math.isfinite(pf_raw) else None

    win = summary["avg_win"] if summary["avg_win"] else 0.0
    loss = summary["avg_loss"] if summary["avg_loss"] else 0.0
    wr = summary["win_rate"]
    expectancy = wr * win - (1 - wr) * loss

    longest_loss = _streak([t.won for t in trades if not t.settlement.refunded], False)
    longest_win = _streak([t.won for t in trades if not t.settlement.refunded], True)
    exposure = summary["count"] / max(1, len(equity_curve))
    ulcer = _ulcer(equity_curve)
    net = final_balance - starting_balance
    recovery = (net / (dd * starting_balance)) if dd > 0 and starting_balance > 0 else (
        1.0 if net > 0 else 0.0
    )

    # survival score: can we take a punch?
    score = 0.0
    score += 0.30 * (1.0 - min(1.0, dd / 0.35))          # capital preservation
    score += 0.20 * clamp01(wr / 0.65)                    # hit rate sanity
    score += 0.15 * clamp01((net + starting_balance * 0.2) / (starting_balance * 0.4))
    score += 0.15 * clamp01(1.0 - longest_loss / 12.0)    # streak containment
    score += 0.10 * clamp01(summary["count"] / 30.0)      # statistical activity
    score += 0.10 * clamp01(expectancy / max(1e-9, loss or 1.0)) if summary["count"] else 0.0
    if final_balance <= starting_balance * 0.5:
        score *= 0.5

    notes: List[str] = []
    if dd > 0.25:
        notes.append("deep drawdown — reduce size or add filters")
    if summary["count"] < 10:
        notes.append("small sample — treat statistics as noisy")
    if longest_loss >= 8:
        notes.append("long loss streak — survivor cooldowns should engage")
    if vetoes > signals_total > 0 and vetoes / max(1, signals_total) > 0.8:
        notes.append("defense stack blocked most signals (expected in crisis sims)")

    return BacktestReport(
        starting_balance=starting_balance,
        final_balance=final_balance,
        net_pnl=net,
        return_pct=(net / starting_balance * 100.0) if starting_balance else 0.0,
        trades=summary["count"],
        wins=summary["wins"],
        losses=summary["losses"],
        refunds=summary["refunds"],
        win_rate=wr,
        profit_factor=pf,
        max_drawdown=dd,
        sharpe=sharpe(returns) if returns else 0.0,
        sortino=sortino(returns) if returns else 0.0,
        avg_win=win,
        avg_loss=loss,
        expectancy=expectancy,
        longest_loss_streak=longest_loss,
        longest_win_streak=longest_win,
        signals_total=signals_total,
        vetoes=vetoes,
        exposure=exposure,
        ulcer_index=ulcer,
        recovery_factor=recovery,
        survival_score=max(0.0, min(1.0, score)),
        notes=notes,
    )


def matrix_table(results: Sequence[BacktestResultLike]) -> str:
    """Render the stress-matrix as a fixed-width table for terminals."""
    header = (
        f"{'scenario':<20} {'seed':>5} {'bal':>9} {'ret%':>8} {'WR%':>6} "
        f"{'trd':>4} {'DD%':>6} {'PF':>6} {'score':>6} {'grade':>5} alive"
    )
    lines = [header, "-" * len(header)]
    for r in results:
        rep = r.report
        pf = rep.profit_factor
        pf_s = f"{pf:.2f}" if pf is not None and math.isfinite(pf) else "  ∞ "
        lines.append(
            f"{r.scenario:<20} {r.seed:>5} {rep.final_balance:>9.2f} "
            f"{rep.return_pct:>8.2f} {rep.win_rate * 100:>6.1f} {rep.trades:>4} "
            f"{rep.max_drawdown * 100:>6.1f} {pf_s:>6} {rep.survival_score:>6.2f} "
            f"{rep.grade():>5} {'YES' if r.survived else 'NO':>4}"
        )
    return "\n".join(lines)


class BacktestResultLike:  # typing stub for readability
    scenario: str
    seed: int
    report: BacktestReport
    survived: bool


def _curve_returns(equity: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(equity)):
        if equity[i - 1] > 0:
            out.append((equity[i] - equity[i - 1]) / equity[i - 1])
    return out


def _streak(results: Sequence[bool], target: bool) -> int:
    best = cur = 0
    for r in results:
        if r == target:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _ulcer(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = -math.inf
    dd2 = []
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            d = (peak - v) / peak * 100.0
            dd2.append(d * d)
    return math.sqrt(sum(dd2) / len(dd2)) if dd2 else 0.0


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


__all__ = ["BacktestReport", "build_report", "matrix_table"]
