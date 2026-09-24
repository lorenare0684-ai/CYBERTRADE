"""Regime, strategy, ensemble, and survivor tests."""

from __future__ import annotations

import json
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


class TestDisabledStrategiesAreActuallyDisabled(unittest.TestCase):
    """``strategy.enabled`` / ``strategy.disabled`` were accepted and ignored.

    Both fields sat in ``StrategyConfig``, were documented in the config
    file, and never reached the ensemble -- the engine built it from every
    registered strategy. An operator who disabled a strategy that was
    losing them money got no effect at all, and had no way to notice from
    the terminal because ``cybertrade strategies`` listed everything.
    """

    def setUp(self):
        from cybertrade.config import StrategyConfig
        from cybertrade.strategies import STRATEGY_REGISTRY, list_strategies

        self.cfg_cls = StrategyConfig
        self.registry = STRATEGY_REGISTRY
        self.everything = list_strategies()

    def test_the_default_config_runs_every_strategy(self):
        from cybertrade.strategies import configured_members

        self.assertEqual(sorted(configured_members(self.cfg_cls())),
                         sorted(self.everything))

    def test_a_disabled_strategy_leaves_the_ensemble(self):
        from cybertrade.strategies import configured_members

        scfg = self.cfg_cls(disabled=["ema_cross_trend", "macd_trend_rider"])
        members = configured_members(scfg)
        self.assertNotIn("ema_cross_trend", members)
        self.assertNotIn("macd_trend_rider", members)
        self.assertEqual(len(members), len(self.everything) - 2)

    def test_enabled_narrows_to_the_named_strategies(self):
        from cybertrade.strategies import configured_members

        scfg = self.cfg_cls(enabled=["rsi_stretch", "bollinger_revert"])
        self.assertEqual(sorted(configured_members(scfg)),
                         ["bollinger_revert", "rsi_stretch"])

    def test_the_all_weather_sentinel_means_everything(self):
        from cybertrade.strategies import ALL_WEATHER, configured_members

        scfg = self.cfg_cls(enabled=[ALL_WEATHER])
        self.assertEqual(len(configured_members(scfg)), len(self.everything))

    def test_unknown_names_do_not_silently_empty_the_ensemble(self):
        """A typo'd list must not stop trading without saying so."""
        from cybertrade.strategies import configured_members

        scfg = self.cfg_cls(enabled=["no_such_strategy"])
        self.assertEqual(len(configured_members(scfg)), len(self.everything))

    def test_disabling_every_enabled_strategy_is_a_config_error(self):
        """Falling back to "all of them" would re-enable what was turned off."""
        from cybertrade.exceptions import ConfigError
        from cybertrade.strategies import configured_members

        scfg = self.cfg_cls(enabled=["rsi_stretch"], disabled=["rsi_stretch"])
        with self.assertRaises(ConfigError) as ctx:
            configured_members(scfg)
        self.assertIn("leave no strategies", str(ctx.exception))

    def test_a_typo_in_disabled_is_rejected_at_validation(self):
        from cybertrade.exceptions import ConfigError

        scfg = self.cfg_cls(disabled=["ema_cros_trend"])
        with self.assertRaises(ConfigError) as ctx:
            scfg.validate()
        self.assertIn("unknown strategy", str(ctx.exception))

    def test_a_real_name_in_disabled_validates(self):
        self.cfg_cls(disabled=["ema_cross_trend"]).validate()

    def test_the_engine_ensemble_excludes_disabled_members(self):
        import json
        import os
        import tempfile

        from cybertrade.bot.engine import TradingEngine
        from cybertrade.config import AppConfig
        from tests.venue_stubs import VenueFeed, VenueStub

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            for attr in ("qx_session_path", "journal_path", "calibration_path",
                         "operator_path", "continuity_path", "heartbeat_path",
                         "log_path"):
                setattr(cfg, attr, os.path.join(tmp, attr))
            cfg.broker.ssid = "QX." + "a" * 32
            cfg.broker.demo_account = True
            cfg.strategy.disabled = ["ema_cross_trend", "macd_trend_rider"]
            cfg.validate()

            feed = VenueFeed(assets=list(cfg.strategy.universe[:2]), n=200,
                             start_price=1.0850)
            stub = VenueStub(balance=1000.0)
            feed.api = stub
            eng = TradingEngine(cfg, feed=feed, broker=stub)
            try:
                eng.boot()
                names = {m.name for m in eng.ensemble.members}
                self.assertNotIn("ema_cross_trend", names)
                self.assertNotIn("macd_trend_rider", names)
                self.assertEqual(len(names), len(self.everything) - 2)
            finally:
                eng.shutdown()

    def test_the_cli_reports_the_active_set(self):
        """The operator must be able to check from the terminal."""
        import os
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"strategy": {"disabled": ["ema_cross_trend"]}}, fh)
            proc = subprocess.run(
                [sys.executable, "-m", "cybertrade", "--config", path,
                 "strategies"],
                capture_output=True, timeout=120,
                env=dict(os.environ, PYTHONPATH=os.getcwd()),
                stdin=subprocess.DEVNULL)
            out = proc.stdout.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-300:])
            self.assertIn("OFF ema_cross_trend", out)
            self.assertIn("disabled by config: ema_cross_trend", out)
            self.assertIn(f"active: {len(self.everything) - 1}/"
                          f"{len(self.everything)}", out)



if __name__ == "__main__":
    unittest.main()
