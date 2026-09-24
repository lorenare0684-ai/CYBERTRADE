"""Regime, strategy, ensemble, and survivor tests."""

from __future__ import annotations

import unittest

from cybertrade.constants import MarketRegime, Side
from cybertrade.data.models import Signal
from tests.venue_stubs import venue_candles as generate_candles
from cybertrade.regime.detector import RegimeDetector
from cybertrade.strategies.base import StrategyContext
from cybertrade.strategies.ensemble import AllWeatherEnsemble
from cybertrade.strategies.registry import (
    STRATEGY_REGISTRY,
    build_all_weather,
    build_universe,
    create,
    list_strategies,
)
from cybertrade.bot.survivor import Posture, Survivor


def _ctx(candles, regime=None):
    return StrategyContext(
        asset="SIM",
        candles=candles,
        regime=regime or RegimeDetector().assess(candles),
        expiry_seconds=60,
        payout=0.85,
        ts=candles[-1].close_ts,
    )


class TestRegime(unittest.TestCase):
    def test_bull_detected(self):
        cs = generate_candles(shape="trend_up", n=300, seed=1)
        r = RegimeDetector().assess(cs)
        self.assertIn(r.regime, (MarketRegime.BULL_TREND, MarketRegime.HIGH_VOL))

    def test_bear_detected(self):
        cs = generate_candles(shape="trend_down", n=300, seed=1)
        r = RegimeDetector().assess(cs)
        self.assertIn(r.regime, (MarketRegime.BEAR_TREND, MarketRegime.HIGH_VOL))

    def test_range_detected(self):
        cs = generate_candles(shape="range", n=300, seed=1)
        r = RegimeDetector().assess(cs)
        self.assertIn(r.regime, (MarketRegime.RANGE, MarketRegime.LOW_VOL))

    def test_short_series_unknown(self):
        cs = generate_candles(shape="mixed", n=10)
        r = RegimeDetector().assess(cs)
        self.assertEqual(r.regime, MarketRegime.UNKNOWN)

    def test_reading_dict(self):
        cs = generate_candles(shape="mixed", n=200)
        d = RegimeDetector().assess(cs).to_dict()
        self.assertIn("stress", d)
        self.assertIn("regime", d)


class TestStrategies(unittest.TestCase):
    def test_registry_complete(self):
        names = list_strategies()
        self.assertGreaterEqual(len(names), 30)
        for name in names:
            strat = create(name)
            self.assertTrue(strat.name)
            self.assertTrue(hasattr(strat, "decide"))

    def test_every_strategy_runs_without_crash(self):
        for shape in ("trend_up", "trend_down", "range", "mixed"):
            cs = generate_candles(shape=shape, n=250, seed=11)
            regime = RegimeDetector().assess(cs)
            ctx = _ctx(cs[-150:], regime)
            for strat in build_universe():
                result = strat.generate(ctx)  # must never raise
                if result is not None:
                    self.assertTrue(result.is_trade)
                    self.assertEqual(result.asset, "SIM")

    def test_signal_side_valid(self):
        cs = generate_candles(shape="trend_up", n=250, seed=2)
        ctx = _ctx(cs)
        for strat in build_universe():
            sig = strat.generate(ctx)
            if sig:
                self.assertIn(sig.side, (Side.CALL, Side.PUT))


class TestEnsemble(unittest.TestCase):
    def test_build_and_describe(self):
        ens = build_all_weather()
        d = ens.describe()
        self.assertIn("members", d)
        self.assertGreaterEqual(len(d["members"]), 30)

    def test_adaptive_quarantine(self):
        ens = AllWeatherEnsemble(members=build_universe()[:3], adaptive=True,
                                 win_rate_floor=0.9)
        for member in ens.members:
            for _ in range(12):
                member.record_result(False, -1.0)
        cs = generate_candles(shape="trend_up", n=250)
        sig = ens.generate(_ctx(cs))
        # all members quarantined -> no signal
        self.assertIsNone(sig)

    def test_confidence_gate(self):
        ens = build_all_weather(min_confidence=1.1)
        cs = generate_candles(shape="trend_up", n=250)
        self.assertIsNone(ens.generate(_ctx(cs)))

    def test_reinforce_changes_weights(self):
        ens = build_all_weather()
        name = ens.members[0].name
        before = ens._weights[name]
        ens.reinforce_vote(name, True)
        self.assertGreaterEqual(ens._weights[name], before)


class TestSurvivor(unittest.TestCase):
    def _sig(self, conf=0.8, side=Side.CALL, expiry=60):
        return Signal(asset="EURUSD_otc", side=side, confidence=conf,
                      strategy="t", expiry_seconds=expiry)

    def test_normal_allows(self):
        from cybertrade.regime.detector import RegimeReading

        s = Survivor()
        reg = RegimeReading(regime=MarketRegime.BULL_TREND, confidence=0.8, stress=0.1)
        d = s.evaluate(self._sig(), reg, strategy_family="trend")
        self.assertTrue(d.allow)
        self.assertEqual(d.posture, Posture.NORMAL)

    def test_crisis_blocks_meanrev(self):
        from cybertrade.regime.detector import RegimeReading

        s = Survivor()
        reg = RegimeReading(regime=MarketRegime.CRISIS, confidence=0.5, stress=0.7)
        d = s.evaluate(self._sig(), reg, strategy_family="meanrev")
        self.assertFalse(d.allow)

    def test_low_liquidity_blocks(self):
        from cybertrade.regime.detector import RegimeReading

        s = Survivor()
        reg = RegimeReading(regime=MarketRegime.RANGE, confidence=0.6, stress=0.1)
        d = s.evaluate(self._sig(), reg, liquidity=0.05)
        self.assertFalse(d.allow)

    def test_news_blackout(self):
        from cybertrade.regime.detector import RegimeReading

        s = Survivor()
        s.flag_news(now=1000.0)
        reg = RegimeReading(regime=MarketRegime.RANGE, confidence=0.6)
        d = s.evaluate(self._sig(), reg, now=1060.0)
        self.assertFalse(d.allow)

    def test_lockdown_blocks_everything(self):
        from cybertrade.regime.detector import RegimeReading

        s = Survivor()
        s.engage_lockdown("test")
        reg = RegimeReading(regime=MarketRegime.BULL_TREND, confidence=0.9, stress=0.0)
        self.assertFalse(s.evaluate(self._sig(), reg).allow)
        s.clear_lockdown()

    def test_stress_scales_stake(self):
        from cybertrade.regime.detector import RegimeReading

        s = Survivor()
        calm = RegimeReading(regime=MarketRegime.BULL_TREND, confidence=0.8, stress=0.0)
        hot = RegimeReading(regime=MarketRegime.HIGH_VOL, confidence=0.8, stress=0.6)
        d1 = s.evaluate(self._sig(), calm)
        d2 = s.evaluate(self._sig(), hot)
        self.assertLess(d2.stake_scale, d1.stake_scale)

    def test_posture_ladder_order(self):
        self.assertEqual(
            Posture.most_defensive(Posture.ATTACK, Posture.DEFENSE), Posture.DEFENSE
        )


if __name__ == "__main__":
    unittest.main()
