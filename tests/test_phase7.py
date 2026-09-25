"""Phase-7 tests — regime-conditional calibration (the WHEN matrix).

Phase-6 found the ceiling: unconditional confidence claims don't predict
outcomes.  These tests pin the remaining hypothesis: *context* does —
a strategy's record inside the current regime is the honest estimate.
Also pins the OMS votes passthrough (live per-voter learning was silently
dead without it).
"""

from __future__ import annotations

import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.constants import MarketRegime, Side
from cybertrade.data.models import Settlement, Signal, Tick, TradeRecord
from cybertrade.quant.calibration import CalibrationTracker
from cybertrade.regime.detector import RegimeReading
from cybertrade.utils import timex
from tests.venue_stubs import VenueFeed, VenueStub


def _vote(name, conf=0.8):
    return {"strategy": name, "confidence": conf, "side": "call"}


class TestRegimeCalibration(unittest.TestCase):
    def test_thin_regime_falls_back(self):
        t = CalibrationTracker()
        for _ in range(3):
            t.observe("s", 0.8, True, regime="bear_trend")
        self.assertEqual(
            t.p_regime("s", "bear_trend", 0.8), t.p_for("s", 0.8)
        )  # < REGIME_MIN_N rows: silent

    def test_regime_discrimination(self):
        t = CalibrationTracker()
        for _ in range(25):
            t.observe("mix", 0.8, True, regime="bull_trend")
            t.observe("mix", 0.8, False, regime="range")
        bull = t.p_regime("mix", "bull_trend", 0.8)
        bear = t.p_regime("mix", "range", 0.8)
        self.assertGreater(bull, 0.7)
        self.assertLess(bear, 0.5405)  # below breakeven at 0.85
        self.assertGreater(bull, bear)

    def test_regime_rows_counter(self):
        t = CalibrationTracker()
        self.assertEqual(t.regime_rows(), 0)
        t.observe("s", 0.8, True, regime="range")
        t.observe("s", 0.8, True, regime="range")
        self.assertEqual(t.regime_rows(), 1)

    def test_p_win_for_regime_kwarg(self):
        t = CalibrationTracker()
        for _ in range(25):
            t.observe("mix", 0.8, True, regime="bull_trend")
            t.observe("mix", 0.8, False, regime="range")
        p_bull = t.p_win_for("mix", 0.8, [_vote("mix")], regime="bull_trend")
        p_bear = t.p_win_for("mix", 0.8, [_vote("mix")], regime="range")
        self.assertGreater(p_bull, p_bear)

    def test_observe_votes_regime_passthrough(self):
        t = CalibrationTracker()
        for _ in range(6):
            t.observe_votes([_vote("v")], True, regime="range")
        self.assertEqual(t.regime_rows(), 1)


class TestRegimeGate(unittest.TestCase):
    def _engine(self, regime_cal: bool) -> TradingEngine:
        cfg = AppConfig()
        cfg.risk.regime_cal = regime_cal
        cfg.risk.win_rate_floor = 0.0
        eng = TradingEngine(cfg, feed=VenueFeed(), broker=VenueStub())
        eng.broker.connect()
        eng.broker.on_tick(Tick(asset=eng.feed.assets[0], price=1.0))
        for _ in range(25):
            eng.calibrator.observe("mix", 0.8, True, regime="bull_trend")
            eng.calibrator.observe("mix", 0.8, False, regime="range")
        return eng

    def _signal(self, eng: TradingEngine) -> Signal:
        return Signal(
            asset=eng.feed.assets[0],
            side=Side.CALL,
            confidence=0.8,
            strategy="mix",
            expiry_seconds=60,
            price=1.0,
            ts=timex.now(),
        )

    def test_range_loser_vetoed(self):
        eng = self._engine(regime_cal=True)
        reading = RegimeReading(regime=MarketRegime.RANGE, confidence=0.5,
                                stress=0.1, trend_strength=0.1, range_score=0.6)
        self.assertFalse(eng._try_execute(self._signal(eng), reading))
        self.assertGreaterEqual(eng.edge_rejects, 1)

    def test_trend_winner_passes(self):
        eng = self._engine(regime_cal=True)
        reading = RegimeReading(regime=MarketRegime.BULL_TREND, confidence=0.6,
                                trend_strength=0.5, trend_direction=1)
        before = eng.edge_rejects
        self.assertTrue(eng._try_execute(self._signal(eng), reading))
        self.assertEqual(eng.edge_rejects, before)

    def test_flag_off_ignores_regime(self):
        eng = self._engine(regime_cal=False)
        reading = RegimeReading(regime=MarketRegime.RANGE, confidence=0.5,
                                stress=0.1, trend_strength=0.1, range_score=0.6)
        # blob record is 25W/25L — mildly positive edge, passes unconditionally
        self.assertTrue(eng._try_execute(self._signal(eng), reading))


class TestVotesPassthrough(unittest.TestCase):
    def test_submit_carries_votes_and_settle_learns_them(self):
        eng = TradingEngine(AppConfig(), feed=VenueFeed(), broker=VenueStub())
        eng.broker.connect()
        eng.broker.on_tick(Tick(asset=eng.feed.assets[0], price=1.0))
        order = eng.oms.submit(
            asset=eng.feed.assets[0],
            side=Side.CALL,
            stake=5.0,
            expiry_seconds=60,
            payout=0.85,
            confidence=0.7,
            strategy="ensemble_all_weather",
            votes=(_vote("chorus_kid"),),
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.meta["votes"][0]["strategy"], "chorus_kid")
        settle = Settlement(
            fill_id="f1", order_id=order.id, asset=order.asset, side=Side.CALL,
            strike=1.0, expiry_price=1.1, stake=5.0, payout=0.85, won=True,
        )
        eng._on_settle(TradeRecord(settlement=settle, strategy="ensemble_all_weather",
                                   regime="range"))
        names = {s["strategy"] for s in eng.calibrator.summary()["strategies"]}
        self.assertIn("chorus_kid", names)  # the vote reached its own table


if __name__ == "__main__":
    unittest.main()
