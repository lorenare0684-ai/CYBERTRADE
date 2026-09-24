"""Phase-13 tests — wire the journal: the record survives its process.

The sqlite journal store existed since Phase 3 and was never fed. Now every
settlement lands in it (sessions bookended at boot/shutdown), and the MC lab
falls back to the journal when the in-memory trade list is empty — so the
risk lab keeps its sample across restarts.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from cybertrade.config import AppConfig
from cybertrade.data.models import Settlement, Side, TradeRecord
from cybertrade.journal import TradeJournal
from cybertrade.risk.montecarlo import simulate_from_records

from tests.venue_stubs import VenueFeed, VenueStub
CONF = 0.8


def _rec(i: int, won: bool = True, strategy: str = "macd") -> TradeRecord:
    return TradeRecord(
        settlement=Settlement(
            fill_id=f"f{i}", order_id=f"o{i}", asset="EURUSD_otc", side=Side.CALL,
            strike=1.0, expiry_price=1.01 if won else 0.99,
            stake=10.0, payout=0.85, won=won, ts=1000.0 + i, id=f"set{i}",
        ),
        strategy=strategy, regime="range",
    )


class TestJournalStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "journal.db")
        self.j = TradeJournal(self.path)

    def test_roundtrip(self):
        self.j.record_trade(_rec(1, won=True, strategy="macd"))
        self.j.record_trade(_rec(2, won=False, strategy="rsi"))
        self.assertEqual(self.j.count(), 2)
        rows = self.j.trades(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pnl"], -10.0)  # newest first: rec 2 lost
        stats = {r["strategy"]: r for r in self.j.strategy_stats()}
        self.assertEqual(stats["macd"]["wins"], 1)

    def test_record_trade_idempotent_by_id(self):
        self.j.record_trade(_rec(1))
        self.j.record_trade(_rec(1))
        self.assertEqual(self.j.count(), 1)

    def test_simulate_from_journal_rows(self):
        for i in range(30):
            self.j.record_trade(_rec(i, won=(i % 3 != 0)))
        report = simulate_from_records(self.j.trades(limit=100), runs=50, horizon=20)
        self.assertGreater(report.runs, 0)

    def test_sessions_bookend(self):
        sid = self.j.start_session("paper", 1000.0)
        self.assertGreater(sid, 0)
        self.j.end_session(sid, 1005.0, notes="clean stop")
        row = sqlite3.connect(self.path).execute(
            "SELECT ended, final_balance, notes FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], 1005.0)


class TestEngineFeedsJournal(unittest.TestCase):
    def _engine(self, tmp):
        from cybertrade.bot.engine import TradingEngine
                
        cfg = AppConfig()
        cfg.journal_path = os.path.join(tmp, "journal.db")
        cfg.calibration_path = os.path.join(tmp, "cal.json")
        eng = TradingEngine(cfg, feed=VenueFeed(),
                            broker=VenueStub())
        return cfg, eng

    def test_settle_lands_in_journal_and_sessions_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, eng = self._engine(tmp)
            eng.boot()
            self.assertGreater(eng._journal_session, 0)
            eng._on_settle(_rec(1, won=True))
            eng._on_settle(_rec(2, won=False))
            self.assertEqual(eng.journal.count(), 2)
            eng.shutdown()
            row = sqlite3.connect(cfg.journal_path).execute(
                "SELECT ended FROM sessions WHERE id=?", (eng._journal_session,)
            ).fetchone()
            self.assertIsNotNone(row[0])

    def test_hub_mc_falls_back_to_journal(self):
        from cybertrade.web.server import EngineHub

        with tempfile.TemporaryDirectory() as tmp:
            cfg, eng = self._engine(tmp)
            eng.boot()
            for i in range(25):
                eng._on_settle(_rec(i, won=(i % 2 == 0)))
            # simulate a restart: fresh engine, empty in-memory list
            eng.shutdown()
            cfg2, eng2 = self._engine(tmp)
            eng2.boot()
            self.assertEqual(len(eng2.oms.ledger.trades), 0)  # memory forgot
            hub = EngineHub(eng2, cfg2)
            mc = hub.montecarlo(runs=30, horizon=20)
            self.assertEqual(mc["source"], "journal")
            self.assertEqual(mc["n_trades"], 25)
            eng2.shutdown()

    def test_in_memory_trades_win_over_journal(self):
        from cybertrade.web.server import EngineHub

        with tempfile.TemporaryDirectory() as tmp:
            cfg, eng = self._engine(tmp)
            eng.boot()
            eng._on_settle(_rec(1))
            eng.oms.ledger.trades.append(_rec(9))  # pretend a live settle list
            hub = EngineHub(eng, cfg)
            self.assertEqual(hub.montecarlo(runs=20, horizon=10)["source"], "trades")
            eng.shutdown()


if __name__ == "__main__":
    unittest.main()
