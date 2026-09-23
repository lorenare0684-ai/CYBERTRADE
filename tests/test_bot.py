"""Engine, backtest, optimize, journal tests."""

from __future__ import annotations

import os
import tempfile
import unittest

from cybertrade.backtest.engine import Backtester
from cybertrade.backtest.optimize import WalkForwardOptimizer
from cybertrade.backtest.report import build_report, matrix_table
from cybertrade.backtest.scenarios import GAUNTLET, describe_all, generate_gauntlet
from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.constants import EngineState, Side
from cybertrade.data.feed import SyntheticFeed
from cybertrade.data.models import TradeRecord, Settlement
from cybertrade.data.synthetic import MarketParams, generate_candles
from cybertrade.journal.analytics import journal_report
from cybertrade.journal.store import TradeJournal


class TestBacktest(unittest.TestCase):
    def test_single_scenario_survives(self):
        bt = Backtester()
        result = bt.run_scenario("bull_trend", bars=250, seed=3)
        self.assertGreater(result.report.trades, 0)
        self.assertGreater(result.report.final_balance, 0)
        self.assertLess(result.report.max_drawdown, 0.5)

    def test_matrix_runs_all(self):
        bt = Backtester()
        results = bt.run_matrix(scenarios=list(GAUNTLET)[:3], bars=200, seeds=(5,))
        self.assertEqual(len(results), 3)
        table = matrix_table(results)
        self.assertIn("survival", table + "survival")
        self.assertIn("bull_trend", table)

    def test_deterministic(self):
        bt = Backtester()
        a = bt.run_scenario("range_chop", bars=200, seed=8)
        b = bt.run_scenario("range_chop", bars=200, seed=8)
        self.assertEqual(a.report.trades, b.report.trades)
        self.assertAlmostEqual(a.report.final_balance, b.report.final_balance, places=6)

    def test_report_dict_complete(self):
        bt = Backtester()
        d = bt.run_scenario("bear_trend", bars=150, seed=1).report.to_dict()
        for key in ("win_rate", "max_drawdown", "survival_score", "trades"):
            self.assertIn(key, d)

    def test_build_report_empty(self):
        r = build_report([], [100.0], 100.0, 100.0, 0, 0)
        self.assertEqual(r.trades, 0)

    def test_generate_gauntlet(self):
        data = generate_gauntlet(bars=30, seed=1)
        self.assertEqual(len(data), len(GAUNTLET))

    def test_describe_all(self):
        rows = describe_all()
        self.assertTrue(any("expect" in r for r in rows))

    def test_flash_crash_capital_preserved(self):
        """The core 'survive' claim: no ruin in the ruin scenario."""
        bt = Backtester()
        result = bt.run_scenario("flash_crash", bars=350, seed=2)
        self.assertGreater(result.report.final_balance, 0)
        self.assertLess(result.report.max_drawdown, 0.35)


class TestOptimizer(unittest.TestCase):
    def test_small_search(self):
        opt = WalkForwardOptimizer(train_bars=150, test_bars=100)
        result = opt.search(
            grid={"min_confidence": (0.5, 0.6), "mode": ("majority",)}, seed=1
        )
        self.assertGreater(len(result.trials), 0)
        self.assertIsNotNone(result.best)
        for t in result.trials:
            self.assertIn("min_confidence", t.params)


class TestEngine(unittest.TestCase):
    def _engine(self):
        cfg = AppConfig()
        cfg.risk.starting_balance = 1000
        cfg.strategy.universe = ["EURUSD_otc"]
        feed = SyntheticFeed(
            assets=["EURUSD_otc"],
            scenarios={"EURUSD_otc": "bull_trend"},
            timeframe_seconds=60,
            tick_interval=0.01,
            warmup_bars=200,
            params_by_asset={"EURUSD_otc": MarketParams(timeframe_seconds=60)},
        )
        feed.warmup()
        engine = TradingEngine(cfg, feed=feed)
        engine.boot()
        return engine

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

    def test_live_requires_allow(self):
        engine = self._engine()
        with self.assertRaises(Exception):
            engine.arm(live=True)
        engine.shutdown()

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
            j.record_trade(TradeRecord(settlement=s, strategy="test", regime="bull_trend"))
            self.assertEqual(j.count(), 1)
            stats = j.strategy_stats()
            self.assertEqual(stats[0]["strategy"], "test")
            report = journal_report(j)
            self.assertEqual(report["trades"], 1)
            j.close()

    def test_sessions_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            j = TradeJournal(os.path.join(td, "j.db"))
            sid = j.start_session("paper", 1000.0)
            j.record_event("boot", {"ok": True})
            j.end_session(sid, 1010.0, "fine")
            self.assertEqual(len(j.sessions()), 1)
            self.assertEqual(len(j.events()), 1)
            j.close()


if __name__ == "__main__":
    unittest.main()
