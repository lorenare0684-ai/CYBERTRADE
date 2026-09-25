"""Phase-6 tests — per-voter calibration, Kelly on evidence, and the
kelly_scaled argument-order regression.

The gauntlet showed the ensemble blob erases strategy identity: one liar in
the chorus paid no personal price.  These tests pin the granular ledger.
"""

from __future__ import annotations

import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.constants import MarketRegime, Side
from cybertrade.data.models import Signal, Tick
from cybertrade.quant.calibration import CalibrationTracker
from cybertrade.regime.detector import RegimeReading
from cybertrade.risk.manager import RiskManager
from cybertrade.utils import timex
from tests.venue_stubs import VenueFeed, VenueStub


def _votes(names, conf=0.9):
    return [{"strategy": n, "confidence": conf, "side": "call"} for n in names]


class TestPerVoterCalibration(unittest.TestCase):
    def test_observe_votes_updates_voters(self):
        t = CalibrationTracker()
        for _ in range(8):
            t.observe_votes(_votes(["alice", "bob"]), True)
        self.assertGreater(t.p_for("alice", 0.9), 0.6)
        self.assertGreater(t.p_for("bob", 0.9), 0.6)

    def test_p_win_for_falls_back_without_votes(self):
        t = CalibrationTracker()
        self.assertEqual(t.p_win_for("s", 0.7), t.p_for("s", 0.7))
        self.assertEqual(t.p_win_for("s", 0.7, []), t.p_for("s", 0.7))

    def test_liars_drag_gate_below_claim(self):
        t = CalibrationTracker()
        for _ in range(30):
            t.observe_votes(_votes(["liar1", "liar2", "liar3"]), False)
        p = t.p_win_for("ensemble", 0.9, _votes(["liar1", "liar2", "liar3"]))
        self.assertLess(p, 0.725)   # far below the shrunk 0.9 claim (0.725)
        self.assertLess(p, 0.5405)  # below breakeven at 0.85 — gate must veto

    def test_winners_lift_gate(self):
        t = CalibrationTracker()
        for _ in range(30):
            t.observe_votes(_votes(["pro1", "pro2"], conf=0.6), True)
        self.assertGreater(t.p_for("pro1", 0.6), 0.65)   # voters earned a record
        p = t.p_win_for("ensemble", 0.6, _votes(["pro1", "pro2"], conf=0.6))
        self.assertGreater(p, 0.65)                      # gate lifted off cold 0.55

    def test_malformed_votes_ignored(self):
        t = CalibrationTracker()
        t.observe_votes([{"strategy": "x"}, {"confidence": 0.9}, None, "junk"], True)
        self.assertEqual(t.observations, 1)

    def test_liar_in_chorus_lowers_gate(self):
        # voter evidence may LOWER the gate below the blob, never raise it
        t = CalibrationTracker()
        for _ in range(30):
            t.observe("chorus", 0.9, True)     # blob record is clean
        for _ in range(30):
            t.observe("mole", 0.9, False)      # one voter is a saboteur
        p = t.p_win_for("chorus", 0.9, _votes(["chorus", "mole"]))
        self.assertLess(p, t.p_for("chorus", 0.9))


class TestKellyOnEvidence(unittest.TestCase):
    def test_kelly_argument_order_regression(self):
        # pre-Phase-6 bug: (kelly_fraction, min_stake, max_stake) were passed
        # into (min_stake, max_stake, kelly_mult) — stakes clamped to ~1.0
        cfg = AppConfig()
        cfg.risk.vol_target_enabled = False
        rm = RiskManager(cfg.risk)
        d = rm.size_stake(
            balance=1000.0,
            payout=0.85,
            confidence=0.7,
            win_rate=0.6,
            drawdown=0.0,
            regime_scale=1.0,
        )
        self.assertGreater(d.stake, 5.0)      # not clamped to ~1
        self.assertLess(d.stake, 200.0)       # fractional Kelly, not full

    def test_kelly_scales_with_edge(self):
        cfg = AppConfig()
        cfg.risk.vol_target_enabled = False
        rm = RiskManager(cfg.risk)
        kw = dict(balance=1000.0, payout=0.85, confidence=0.7,
                  drawdown=0.0, regime_scale=1.0)
        weak = rm.size_stake(win_rate=0.56, **kw).stake
        strong = rm.size_stake(win_rate=0.70, **kw).stake
        self.assertGreater(strong, weak)


class TestEngineGateUsesVotes(unittest.TestCase):
    def _signal(self, engine: TradingEngine, names):
        return Signal(
            asset=engine.feed.assets[0],
            side=Side.CALL,
            confidence=0.95,
            strategy="ensemble_all_weather",
            expiry_seconds=60,
            price=1.0,
            ts=timex.now(),
            meta={"votes": _votes(names)},
        )

    def test_liar_chorus_vetoes_high_claim(self):
        cfg = AppConfig()
        eng = TradingEngine(cfg, feed=VenueFeed(), broker=VenueStub())
        for name in ("liar1", "liar2", "liar3"):
            for _ in range(30):
                eng.calibrator.observe(name, 0.9, False)
        ok = eng._try_execute(self._signal(eng, ["liar1", "liar2", "liar3"]), _reading())
        self.assertFalse(ok)
        self.assertGreaterEqual(eng.edge_rejects, 1)

    def test_winner_chorus_passes_the_gate(self):
        cfg = AppConfig()
        cfg.risk.win_rate_floor = 0.0
        eng = TradingEngine(cfg, feed=VenueFeed(), broker=VenueStub())
        eng.broker.connect()  # engine not booted in this unit test
        eng.broker.on_tick(Tick(asset=eng.feed.assets[0], price=1.0))  # a venue quote
        for name in ("pro1", "pro2", "pro3"):
            for _ in range(30):
                eng.calibrator.observe(name, 0.9, True)
        before = eng.edge_rejects
        ok = eng._try_execute(self._signal(eng, ["pro1", "pro2", "pro3"]), _reading())
        self.assertTrue(ok)
        self.assertEqual(eng.edge_rejects, before)


def _reading() -> RegimeReading:
    return RegimeReading(regime=MarketRegime.RANGE, confidence=0.5, stress=0.1,
                         trend_strength=0.1, range_score=0.6)


if __name__ == "__main__":
    unittest.main()
