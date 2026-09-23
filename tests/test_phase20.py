"""Phase-20 tests — crisis drills (chaos engineering for the defense stack).

A drill drives real scenario processes through the live engine's tick seam
and scores what the defense stack actually did: posture floor, salvaged
count, kill switch.  Verdicts are honest — a storm that never engaged the
defenses reports SURVIVED (untested), not heroics.
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from types import SimpleNamespace

from cybertrade.bot.drills import (
    CRISIS_SCENARIOS,
    VERDICTS,
    DrillStats,
    StressDrill,
    run_gauntlet,
)
from cybertrade.bot.survivor import Posture
from cybertrade.data.models import Order, Tick
from cybertrade.data.synthetic import make_process
from cybertrade.exceptions import DataError


def _path(name: str, n: int, seed: int = 1337):
    proc = make_process(name, seed=seed)
    state = proc.initial_state()
    out = [float(state.price)]
    for _ in range(n):
        out.append(float(proc.step(state)))
    return out


class TestScenarioShockPaths(unittest.TestCase):
    def test_flash_crash_drops(self):
        prices = _path("flash_crash", 300)
        self.assertLessEqual(min(prices) / prices[0], 0.97)  # >=3% down

    def test_gap_open_jumps(self):
        prices = _path("gap_open", 300)
        biggest = max(abs(prices[i] / prices[i - 1] - 1.0) for i in range(1, len(prices)))
        self.assertGreaterEqual(biggest, 0.006)  # measured 0.0081 @ seed 1337

    def test_whipsaw_flips(self):
        prices = _path("regime_whipsaw", 300)
        rets = [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices))]
        flips = sum(1 for a, b in zip(rets, rets[1:]) if a * b < 0)
        self.assertGreaterEqual(flips, 10)

    def test_all_paths_finite_positive(self):
        for name in CRISIS_SCENARIOS:
            for p in _path(name, 120):
                self.assertTrue(math.isfinite(p) and p > 0, name)


class TestStressDrill(unittest.TestCase):
    def test_arm_shock_drain_cycle(self):
        d = StressDrill(seed=1337)
        stats = d.arm("flash_crash", assets=["EURUSD_otc"], ticks=50)
        self.assertTrue(d.active())
        p0 = d.shock_price("EURUSD_otc", 1.10)
        for _ in range(49):
            d.shock_price("EURUSD_otc", 1.10)
        self.assertFalse(d.active())
        self.assertEqual(stats.ticks, 50)
        self.assertNotEqual(p0, 1.10)          # the shock is real
        self.assertAlmostEqual(d.shock_price("EURUSD_otc", 1.10), 1.10)  # drained = identity

    def test_scale_free_on_any_asset(self):
        d = StressDrill(seed=1337)
        d.arm("flash_crash", assets=["BTCUSD_otc"], ticks=80)
        for _ in range(80):
            p = d.shock_price("BTCUSD_otc", 60000.0)
        self.assertLess(p, 60000.0)            # crash scales to the asset
        self.assertGreater(p, 0.0)

    def test_unknown_scenario_raises(self):
        with self.assertRaises(DataError):
            StressDrill().arm("nope", assets=["EURUSD_otc"])

    def test_verdicts_are_honest(self):
        s = DrillStats(name="x")
        self.assertEqual(s.verdict, "SURVIVED (untested)")
        s.note_posture(Posture.DEFENSE)
        self.assertEqual(s.verdict, "SURVIVED (defended)")
        s.salvaged = 1
        self.assertEqual(s.verdict, "SURVIVED (lifeboat)")
        s.killed = True
        self.assertEqual(s.verdict, "KILLED")
        s2 = DrillStats(name="y")
        s2.note_posture(Posture.LOCKDOWN)
        s2.note_posture(Posture.ATTACK)       # floor must not lift
        self.assertEqual(s2.posture_min, Posture.LOCKDOWN)


class TestEngineDrill(unittest.TestCase):
    def _engine(self, tmp):
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.config import AppConfig
        from cybertrade.data.feed import SyntheticFeed
        from cybertrade.execution.paper import PaperBroker

        cfg = AppConfig()
        cfg.journal_path = os.path.join(tmp, "j.db")
        cfg.calibration_path = os.path.join(tmp, "c.json")
        eng = TradingEngine(cfg, feed=SyntheticFeed(tick_interval=60.0),
                            broker=PaperBroker(starting_balance=1000.0))
        eng.boot()
        return eng

    def test_tick_seeems_shock_and_report_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp)
            self.assertIsNone(eng.drill_report())
            asset = eng.feed.assets[0]
            eng.drill.arm("flash_crash", assets=[asset], ticks=60)
            for i in range(60):
                ts = 1.0 + i * 60.0
                price = eng.drill.shock_price(asset, 1.10)  # explicit shock (P21 seam)
                eng.feed.book(asset).on_price(price, ts)
                eng._on_tick(Tick(asset=asset, price=price, ts=ts))
            self.assertLess(eng.broker.last_price(asset), 1.09)  # paper saw the crash
            eng.survivor = SimpleNamespace(posture_for=lambda *a, **k: Posture.LOCKDOWN)
            eng.regime_of = {asset: SimpleNamespace()}
            eng.cycle(now=2.0)
            row = eng.drill_report()
            self.assertEqual(row["name"], "flash_crash")
            self.assertEqual(row["posture_min"], Posture.LOCKDOWN)
            self.assertFalse(eng.drill.active())
            eng.shutdown()

    def test_kill_flag_marks_the_drill(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp)
            asset = eng.feed.assets[0]
            eng.drill.arm("gap_open", assets=[asset], ticks=10)
            eng._enter_kill("test")
            self.assertEqual(eng.drill_report()["verdict"], "KILLED")
            eng.clear_kill()
            eng.shutdown()


class TestGauntlet(unittest.TestCase):
    def test_five_storms_scored_with_lifeboat(self):
        with tempfile.TemporaryDirectory() as tmp:
            from cybertrade.bot.engine import TradingEngine
            from cybertrade.config import AppConfig
            from cybertrade.data.feed import SyntheticFeed
            from cybertrade.execution.paper import PaperBroker

            cfg = AppConfig()
            cfg.journal_path = os.path.join(tmp, "j.db")
            cfg.calibration_path = os.path.join(tmp, "c.json")
            eng = TradingEngine(cfg, feed=SyntheticFeed(tick_interval=60.0),
                                broker=PaperBroker(starting_balance=1000.0))
            eng.boot()
            eng.survivor = SimpleNamespace(posture_for=lambda *a, **k: Posture.LOCKDOWN)
            rows = run_gauntlet(eng, ticks=40, expiry_seconds=15)
            eng.shutdown()
            self.assertEqual(len(rows), 5)
            self.assertEqual([r["name"] for r in rows], list(CRISIS_SCENARIOS))
            for row in rows:
                self.assertIn(row["verdict"], VERDICTS)
                self.assertEqual(row["verdict"], "SURVIVED (lifeboat)")
                self.assertEqual(row["salvaged"], 1)      # the book was salvaged
                self.assertEqual(row["posture_min"], Posture.LOCKDOWN)
                self.assertTrue(math.isfinite(row["pnl"]))

    def test_natural_chain_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            from cybertrade.bot.engine import TradingEngine
            from cybertrade.config import AppConfig
            from cybertrade.data.feed import SyntheticFeed
            from cybertrade.execution.paper import PaperBroker

            cfg = AppConfig()
            cfg.journal_path = os.path.join(tmp, "j.db")
            cfg.calibration_path = os.path.join(tmp, "c.json")
            eng = TradingEngine(cfg, feed=SyntheticFeed(tick_interval=60.0),
                                broker=PaperBroker(starting_balance=1000.0))
            eng.boot()
            rows = run_gauntlet(eng, scenarios=("flash_crash",), ticks=60)
            eng.shutdown()
            row = rows[0]
            self.assertIn(row["verdict"], VERDICTS)  # honest verdict, whatever it is
            self.assertGreater(row["ticks"], 0)


if __name__ == "__main__":
    unittest.main()
