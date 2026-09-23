"""Double-entry-lite ledger: balance, equity, and per-strategy attribution."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..data.models import Settlement, TradeRecord
from ..statestore import StateError, finite
from ..utils import timex


@dataclass
class LedgerEntry:
    ts: float
    kind: str               # deposit | stake | settle | fee
    amount: float
    balance_after: float
    ref: str = ""
    note: str = ""


class Ledger:
    """Chronological cash book with strategy attribution."""

    def __init__(self, starting_balance: float = 0.0) -> None:
        self._lock = threading.RLock()
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.peak = starting_balance
        self.entries: List[LedgerEntry] = []
        self.trades: List[TradeRecord] = []
        self.by_strategy: Dict[str, Dict[str, float]] = {}
        self.by_asset: Dict[str, Dict[str, float]] = {}
        self.equity_curve: List[tuple] = [(timex.now(), starting_balance)]

    def export_state(self) -> dict:
        """Cash anchors + bounded chart trace; the journal owns trade history."""
        with self._lock:
            return {"starting_balance": self.starting_balance, "balance": self.balance,
                    "peak": self.peak, "equity_curve": list(self.equity_curve[-400:])}

    @staticmethod
    def decode_state(data: dict) -> dict:
        try:
            result = {k: finite(data[k], k) for k in ("starting_balance", "balance", "peak")}
            if result["peak"] < result["balance"]:
                raise StateError("ledger peak below cash")
            curve = data["equity_curve"]
            if not isinstance(curve, list) or len(curve) > 400:
                raise StateError("invalid equity trace")
            result["equity_curve"] = [(finite(p[0], "curve.ts"), finite(p[1], "curve.balance"))
                                      for p in curve if len(p) == 2]
            if len(result["equity_curve"]) != len(curve):
                raise StateError("invalid equity point")
            return result
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise StateError("invalid saved ledger") from exc

    def restore_state(self, data: dict) -> None:
        decoded = self.decode_state(data)
        with self._lock:
            self.starting_balance = decoded["starting_balance"]
            self.balance, self.peak = decoded["balance"], decoded["peak"]
            self.equity_curve = decoded["equity_curve"]
            self.entries.clear()
            self.trades.clear()
            self.by_strategy.clear()
            self.by_asset.clear()

    def deposit(self, amount: float, ref: str = "", note: str = "") -> None:
        with self._lock:
            self._add("deposit", amount, ref, note)

    def stake(self, amount: float, ref: str = "", note: str = "") -> None:
        with self._lock:
            self._add("stake", -abs(amount), ref, note)

    def record_settlement(self, settlement: Settlement, strategy: str = "", regime: str = "") -> TradeRecord:
        with self._lock:
            self._add("settle", settlement.returned, settlement.id, f"won={settlement.won}")
            record = TradeRecord(settlement=settlement, strategy=strategy, regime=regime)
            self.trades.append(record)
            self._bucket(self.by_strategy, strategy or "unknown", record)
            self._bucket(self.by_asset, settlement.asset, record)
            self.equity_curve.append((settlement.ts, self.balance))
            if self.balance > self.peak:
                self.peak = self.balance
            return record

    def _add(self, kind: str, amount: float, ref: str, note: str) -> None:
        self.balance += amount
        self.entries.append(
            LedgerEntry(
                ts=timex.now(),
                kind=kind,
                amount=amount,
                balance_after=self.balance,
                ref=ref,
                note=note,
            )
        )
        if len(self.entries) > 5000:
            del self.entries[:-5000]

    def _bucket(self, store: Dict[str, Dict[str, float]], key: str, rec: TradeRecord) -> None:
        b = store.setdefault(key, {"n": 0, "w": 0, "pnl": 0.0, "stake": 0.0})
        b["n"] += 1
        b["w"] += 1 if rec.won else 0
        b["pnl"] += rec.pnl
        b["stake"] += rec.settlement.stake

    # -- queries -----------------------------------------------------------
    def net_pnl(self) -> float:
        return self.balance - self.starting_balance

    def win_rate(self) -> float:
        closed = [t for t in self.trades if not t.settlement.refunded]
        if not closed:
            return 0.0
        return sum(1 for t in closed if t.won) / len(closed)

    def drawdown(self) -> float:
        if self.peak <= 0:
            return 0.0
        return max(0.0, (self.peak - self.balance) / self.peak)

    def summary(self) -> dict:
        closed = [t for t in self.trades if not t.settlement.refunded]
        gross_win = sum(t.pnl for t in closed if t.pnl > 0)
        gross_loss = -sum(t.pnl for t in closed if t.pnl < 0)
        return {
            "balance": round(self.balance, 2),
            "net_pnl": round(self.net_pnl(), 2),
            "trades": len(closed),
            "win_rate": round(self.win_rate(), 4),
            "gross_win": round(gross_win, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "peak": round(self.peak, 2),
            "drawdown": round(self.drawdown(), 4),
        }

    def top_strategies(self, limit: int = 10) -> List[dict]:
        rows = []
        for name, b in self.by_strategy.items():
            rows.append(
                {
                    "strategy": name,
                    "trades": int(b["n"]),
                    "win_rate": (b["w"] / b["n"]) if b["n"] else 0.0,
                    "pnl": round(b["pnl"], 2),
                }
            )
        rows.sort(key=lambda r: r["pnl"], reverse=True)
        return rows[:limit]


__all__ = ["Ledger", "LedgerEntry"]
