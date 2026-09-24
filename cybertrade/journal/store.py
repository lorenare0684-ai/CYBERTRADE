"""SQLite trade journal — durable, concurrent-safe record of every outcome."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Sequence

from ..data.models import TradeRecord
from ..compat import windows_long_path
from ..exceptions import JournalError
from ..utils import timex

log = logging.getLogger("cybertrade.journal")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    strike REAL NOT NULL,
    expiry_price REAL NOT NULL,
    stake REAL NOT NULL,
    payout REAL NOT NULL,
    won INTEGER NOT NULL,
    refunded INTEGER NOT NULL DEFAULT 0,
    pnl REAL NOT NULL,
    strategy TEXT DEFAULT '',
    regime TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    meta TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    ended REAL,
    mode TEXT,
    starting_balance REAL,
    final_balance REAL,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class TradeJournal:
    """Append-only journal with analytics-friendly queries."""

    def __init__(self, path: str = "data/journal.db") -> None:
        self.path = path
        self._lock = threading.RLock()
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            # A user may point the journal anywhere; on Windows a deep
            # absolute path needs the \\?\ prefix or open() fails past 260
            # characters with an error that names nothing useful.
            self._conn = sqlite3.connect(
                windows_long_path(path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.executescript(_SCHEMA)
                self._conn.commit()
        except (sqlite3.Error, OSError) as exc:
            # makedirs sits inside the guard on purpose: an unwritable
            # journal directory is a JournalError with a path in it, not a
            # bare PermissionError the operator has to interpret.
            raise JournalError(f"cannot open journal at {path}: {exc}") from exc

    # -- writes ------------------------------------------------------------
    def record_trade(self, record: TradeRecord) -> None:
        s = record.settlement
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO trades
                   (id, ts, asset, side, strike, expiry_price, stake, payout,
                    won, refunded, pnl, strategy, regime, tags, meta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    s.id,
                    s.ts,
                    s.asset,
                    s.side.value,
                    s.strike,
                    s.expiry_price,
                    s.stake,
                    s.payout,
                    1 if s.won else 0,
                    1 if s.refunded else 0,
                    record.pnl,
                    record.strategy,
                    record.regime,
                    json.dumps(list(record.tags)),
                    json.dumps(s.__dict__, default=str),
                ),
            )
            self._conn.commit()

    def record_event(self, kind: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, kind, payload) VALUES (?,?,?)",
                (timex.now(), kind, json.dumps(payload, default=str)),
            )
            self._conn.commit()

    def start_session(self, mode: str, starting_balance: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (started, mode, starting_balance) VALUES (?,?,?)",
                (timex.now(), mode, starting_balance),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def end_session(self, session_id: int, final_balance: float, notes: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE sessions SET ended=?, final_balance=?, notes=? WHERE id=?""",
                (timex.now(), final_balance, notes, session_id),
            )
            self._conn.commit()

    # -- queries -----------------------------------------------------------
    def trades(self, limit: int = 100, strategy: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM trades"
        args: List[Any] = []
        if strategy:
            query += " WHERE strategy = ?"
            args.append(strategy)
        query += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()
        return int(row["n"] if row else 0)

    def strategy_stats(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT strategy,
                          COUNT(*) AS n,
                          SUM(won) AS wins,
                          SUM(pnl) AS pnl,
                          SUM(stake) AS staked
                   FROM trades
                   GROUP BY strategy
                   ORDER BY pnl DESC"""
            ).fetchall()
        out = []
        for r in rows:
            n = r["n"] or 0
            out.append(
                {
                    "strategy": r["strategy"] or "unknown",
                    "trades": n,
                    "wins": int(r["wins"] or 0),
                    "win_rate": (r["wins"] / n) if n else 0.0,
                    "pnl": round(float(r["pnl"] or 0.0), 2),
                    "staked": round(float(r["staked"] or 0.0), 2),
                }
            )
        return out

    def equity_curve(self) -> List[tuple]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, pnl FROM trades ORDER BY ts ASC"
            ).fetchall()
        curve = []
        balance = 0.0
        for r in rows:
            balance += float(r["pnl"])
            curve.append((float(r["ts"]), balance))
        return curve

    def sessions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY started DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def events(self, limit: int = 50, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM events"
        args: List[Any] = []
        if kind:
            query += " WHERE kind = ?"
            args.append(kind)
        query += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def purge_before(self, ts: float) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM trades WHERE ts < ?", (ts,))
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["TradeJournal"]
