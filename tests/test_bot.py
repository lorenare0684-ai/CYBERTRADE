"""Engine and journal tests (live-only build: venue stubs, no simulation)."""

from __future__ import annotations

import os
import tempfile
import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.constants import EngineState, Side
from cybertrade.data.models import TradeRecord, Settlement
from cybertrade.journal.analytics import journal_report
from cybertrade.journal.store import TradeJournal

from tests.venue_stubs import VenueFeed, VenueStub, make_engine

class TestEngine(unittest.TestCase):
    def _engine(self):
        return make_engine(self, assets=["EURUSD_otc"])

    def test_boot_disarmed(self):
        engine = self._engine()
        self.assertEqual(engine.state, EngineState.DISARMED)
        engine.shutdown()

    def test_disarmed_does_not_trade(self):
        engine = self._engine()
        for i in range(5):
            summary = engine.cycle(now=1e9 + i * 60 + 30)
        self.assertEqual(summary["orders"], 0)
        engine.shutdown()

    def test_live_arm_requires_allow_live(self):
        """The only arming left is live arming — allow_live is its gate."""
        engine = self._engine()
        engine.config.risk.allow_live = False
        with self.assertRaises(Exception):
            engine.arm()
        engine.shutdown()

    def test_arm_is_live(self):
        engine = self._engine()
        engine.arm()
        self.assertEqual(engine.state, EngineState.LIVE)
        engine.shutdown()

    def test_armed_cycle_trades(self):
        engine = self._engine()
        engine.state = EngineState.ARMED
        orders = 0
        for i in range(120):
            orders += engine.cycle(now=1e9 + i * 60 + 30)["orders"]
        self.assertGreater(orders, 0)
        engine.shutdown()

    def test_kill_blocks_cycles(self):
        engine = self._engine()
        engine.state = EngineState.ARMED
        engine.kill("test kill")
        with self.assertRaises(Exception):
            engine.cycle(now=1e9)
        engine.clear_kill()

    def test_snapshot_shape(self):
        engine = self._engine()
        snap = engine.snapshot()
        for key in ("health", "account", "risk", "regimes", "watchdog", "survivor"):
            self.assertIn(key, snap)
        engine.shutdown()


class TestJournal(unittest.TestCase):
    def test_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "j.db")
            j = TradeJournal(path)
            s = Settlement("f1", "o1", "EURUSD_otc", Side.CALL, 1.1, 1.2, 10.0,
                           0.85, won=True, ts=100.0)
            j.record_trade(TradeRecord(settlement=s, strategy="test", regime="bull"))
            self.assertEqual(j.count(), 1)
            stats = j.strategy_stats()
            self.assertEqual(stats[0]["strategy"], "test")
            report = journal_report(j)
            self.assertEqual(report["trades"], 1)
            j.close()

    def test_sessions_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            j = TradeJournal(os.path.join(td, "j.db"))
            sid = j.start_session("quotex", 1000.0)
            j.record_event("boot", {"ok": True})
            j.end_session(sid, 1010.0, "fine")
            self.assertEqual(len(j.sessions()), 1)
            self.assertEqual(len(j.events()), 1)
            j.close()


if __name__ == "__main__":
    unittest.main()
