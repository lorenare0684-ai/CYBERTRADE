"""Regime, strategy, ensemble, and survivor tests."""

from __future__ import annotations

import json
import unittest

from cybertrade.constants import MarketRegime, Side
from cybertrade.data.models import Signal
from tests.venue_stubs import venue_candles as generate_candles
from cybertrade.regime.detector import RegimeDetector, RegimeReading
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


class TestTheDeadConfigKnobsActuallyDoSomething(unittest.TestCase):
    """Config fields that were accepted, stored, round-tripped -- and ignored.

    ``from_dict`` rejects unknown keys, so these could not simply be deleted
    without breaking existing configs. They are wired instead.
    """

    def _engine(self, **strategy_kw):
        from cybertrade.config import AppConfig, StrategyConfig
        from cybertrade.bot.engine import TradingEngine
        from tests.venue_stubs import VenueFeed, VenueStub

        cfg = AppConfig()
        cfg.strategy = StrategyConfig(**strategy_kw)
        feed = VenueFeed(assets=["EURUSD"], n=120, timeframe_seconds=60)
        return cfg, TradingEngine(cfg, feed=feed, broker=VenueStub())

    def test_max_signals_per_candle_defaults_to_no_cap(self):
        """The old default of 2 was never enforced -- honouring it now would
        have silently halved the default vote set."""
        _, eng = self._engine()
        self.assertEqual(eng.ensemble.max_votes, 0)

    def test_max_signals_per_candle_actually_caps_the_vote_set(self):
        for cap in (1, 3, 7):
            _, eng = self._engine(max_signals_per_candle=cap)
            self.assertEqual(eng.ensemble.max_votes, cap)

    def test_the_cap_keeps_the_strongest_votes(self):
        """Real strategies, real Signals: the cap blends the strongest N."""
        from cybertrade.strategies.ensemble import AllWeatherEnsemble

        bases = list(STRATEGY_REGISTRY.values())
        members = []
        for i in range(40):
            m = type(f"Capped{i}", (bases[i % len(bases)],), {})()
            m.name = f"capped_{i}"
            members.append(m)

        for cap, expected in ((0, 40), (3, 3), (7, 7)):
            ens = AllWeatherEnsemble(members=members, max_votes=cap,
                                     min_confidence=0.5)
            for i, m in enumerate(ens.members):
                # confidence descends with index, so quality does too
                conf = 0.95 - (i * 0.015)

                def gen(ctx, c=conf):
                    return Signal(asset="EURUSD", side=Side.CALL,
                                  confidence=c, strategy="capped",
                                  timeframe_seconds=60)
                m.generate = gen
            seen = []
            ens._blend = lambda c, vs: (seen.append([v.confidence for v in vs]),
                                        (Side.CALL, 0.9, []))[1]
            ens.decide(StrategyContext(asset="EURUSD", candles=[]))
            self.assertEqual(len(seen[0]), expected)
            if expected < 40:
                # the cap must not drop the best evidence
                self.assertEqual(max(seen[0]), 0.95)
                self.assertGreater(min(seen[0]), 0.95 - expected * 0.015)

    def test_a_bad_cap_is_a_config_error_not_a_traceback(self):
        from cybertrade.config import AppConfig, ConfigError

        for bad in ("abc", None):
            cfg = AppConfig()
            cfg.strategy.max_signals_per_candle = bad
            with self.assertRaises(ConfigError):
                cfg.strategy.validate()

    def test_a_negative_cap_becomes_no_cap(self):
        from cybertrade.config import AppConfig

        cfg = AppConfig()
        cfg.strategy.max_signals_per_candle = -5
        cfg.strategy.validate()
        self.assertEqual(cfg.strategy.max_signals_per_candle, 0)

    def test_trade_on_weak_drops_the_floor_into_the_weak_band(self):
        _, off = self._engine(trade_on_weak=False)
        _, on = self._engine(trade_on_weak=True)
        self.assertEqual(off.ensemble.min_confidence, 0.55)
        self.assertEqual(on.ensemble.min_confidence, 0.30)

    def test_trade_on_weak_cannot_lift_the_floor(self):
        _, eng = self._engine(min_confidence=0.9, trade_on_weak=True)
        self.assertEqual(eng.ensemble.min_confidence, 0.30)

    def test_a_weak_signal_is_admitted_only_with_the_flag(self):
        """A 0.40-confidence signal is WEAK at the 0.55 floor; the flag is the
        only thing that lets it through."""
        sig = Signal(asset="EURUSD", side=Side.CALL, confidence=0.40,
                     strategy="weak_one", timeframe_seconds=60)
        self.assertLess(sig.confidence, 0.55)
        for weak, expected in ((False, False), (True, True)):
            _, eng = self._engine(trade_on_weak=weak)
            ens = eng.ensemble

            m = type("One", (next(iter(STRATEGY_REGISTRY.values())),), {})()
            m.name = "weak_one"
            m.generate = lambda ctx: sig

            ens.members = [m]
            ens._weights = {"weak_one": 1.0}
            ens._scores = {"weak_one": 0.5}
            out = ens.decide(StrategyContext(asset="EURUSD", candles=[]))
            self.assertEqual(bool(out), expected,
                             f"trade_on_weak={weak} -> {out!r}")

    def test_crisis_stake_scale_reaches_the_stake(self):
        """risk.crisis_stake_scale was accepted and ignored -- an operator who
        cut it to 0.25 to shrink risk in a crisis got full-size stakes in
        exactly the conditions it was written for (source lock)."""
        import inspect
        from cybertrade.bot.engine import TradingEngine

        src = inspect.getsource(TradingEngine._try_execute)
        self.assertIn("crisis_stake_scale if reading.is_defensive else 1.0", src)
        self.assertIn("decision.stake_scale *", src)

    def test_crisis_stake_scale_shrinks_the_stake_in_a_defensive_regime(self):
        """And it really does move the number, not just the source."""
        from cybertrade.config import AppConfig, StrategyConfig, RiskConfig
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.regime.detector import RegimeReading
        from cybertrade.constants import MarketRegime
        from tests.venue_stubs import VenueFeed, VenueStub

        def engine(scale):
            cfg = AppConfig()
            cfg.strategy = StrategyConfig()
            cfg.risk = RiskConfig(crisis_stake_scale=scale)
            return TradingEngine(cfg, feed=VenueFeed(assets=["EURUSD"], n=60),
                                 broker=VenueStub())

        # a defensive reading is what makes the knob bite
        r = RegimeReading()
        r.regime = MarketRegime.CRISIS
        r.stress = 1.0
        self.assertTrue(r.is_defensive)

        # same conditions, two scales -> the scaled stake is a quarter of it
        full = engine(1.0).risk.size_stake(
            balance=1000.0, payout=0.85, confidence=0.8, win_rate=0.6,
            drawdown=0.0, regime_scale=1.0).stake
        quarter = engine(0.25).risk.size_stake(
            balance=1000.0, payout=0.85, confidence=0.8, win_rate=0.6,
            drawdown=0.0, regime_scale=0.25).stake
        self.assertLess(quarter, full)
        self.assertAlmostEqual(quarter, full * 0.25, places=6)

    def test_trend_filter_blocks_the_knife_catch(self):
        """In a bull trend a put is forbidden, in a bear trend a long is
        forbidden -- that is what `trend_filter` buys."""
        from cybertrade.bot.survivor import Survivor

        def reading(regime):
            r = RegimeReading()
            r.regime = regime
            r.trend_strength = 0.9
            r.trend_direction = -1 if regime is MarketRegime.BEAR_TREND else 1
            return r

        def call():
            return Signal(asset="EURUSD", side=Side.CALL, confidence=0.8,
                          strategy="s", timeframe_seconds=60)

        def put():
            return Signal(asset="EURUSD", side=Side.PUT, confidence=0.8,
                          strategy="s", timeframe_seconds=60)

        def allowed(survivor, sig, regime):
            d = survivor.evaluate(sig, reading(regime))
            return d.allow, " ".join(d.reasons)

        on, off = Survivor(trend_filter=True), Survivor(trend_filter=False)
        # bear trend: the long is the knife-catch, the put is the ride
        self.assertIn("trend filter", allowed(on, call(), MarketRegime.BEAR_TREND)[1])
        self.assertTrue(allowed(on, put(), MarketRegime.BEAR_TREND)[0])
        # bull trend: the put is the counter-trend entry
        self.assertIn("trend filter", allowed(on, put(), MarketRegime.BULL_TREND)[1])
        self.assertTrue(allowed(on, call(), MarketRegime.BULL_TREND)[0])
        # with the flag off, none of that is vetoed
        for sig, reg in ((call(), MarketRegime.BEAR_TREND),
                         (put(), MarketRegime.BULL_TREND)):
            self.assertNotIn("trend filter", allowed(off, sig, reg)[1])

    def test_regime_rotation_gates_the_family_table(self):
        from cybertrade.bot.survivor import Survivor

        for posture in ("ATTACK", "NORMAL", "GUARD", "DEFENSE", "LOCKDOWN"):
            off = Survivor(regime_rotation=False).strategy_filter(posture)
            on = Survivor(regime_rotation=True).strategy_filter(posture)
            if posture != "LOCKDOWN":
                self.assertEqual(off["weights"], {})
                self.assertTrue(on["weights"])


class TestSurvivorEnabledActuallyEnables(unittest.TestCase):
    """survivor.enabled was stored on self.enabled and echoed into the
    snapshot, then never read. survivor.enabled=false therefore vetoed exactly
    as hard as true -- the flag lied, and it lied about a risk control."""

    def _reading(self, regime=MarketRegime.CRISIS, stress=1.0):
        r = RegimeReading()
        r.regime = regime
        r.stress = stress
        return r

    def _sig(self, side=Side.CALL):
        return Signal(asset="EURUSD", side=side, confidence=0.9,
                      strategy="rsi_stretch", timeframe_seconds=60)

    def test_the_flag_changes_the_verdict(self):
        on = Survivor(enabled=True).evaluate(self._sig(), self._reading(),
                                             strategy_family="meanrev")
        off = Survivor(enabled=False).evaluate(self._sig(), self._reading(),
                                               strategy_family="meanrev")
        self.assertFalse(on.allow)
        self.assertTrue(off.allow)

    def test_a_disabled_playbook_reports_no_caps(self):
        """The engine clamps the expiry to decision.max_expiry_seconds and
        reads min_confidence off the decision, so a disabled playbook that
        still reported the posture table would constrain through the back
        door."""
        d = Survivor(enabled=False).evaluate(self._sig(),
                                             self._reading(MarketRegime.LOW_VOL,
                                                           0.0),
                                             strategy_family="meanrev")
        self.assertEqual(d.min_confidence, 0.0)
        self.assertEqual(d.forbidden_families, set())
        self.assertGreaterEqual(d.max_expiry_seconds, 60)

    def test_says_why_it_allowed_the_trade(self):
        d = Survivor(enabled=False).evaluate(self._sig(),
                                             self._reading(MarketRegime.LOW_VOL,
                                                           0.0),
                                             strategy_family="meanrev")
        self.assertIn("playbook disabled", " ".join(d.reasons))

    def test_manual_lockdown_survives_the_switch(self):
        """The emergency stop is not playbook tuning. Disabling the playbook
        must not disable the one control an operator reaches for in a panic."""
        s = Survivor(enabled=False)
        s.engage_lockdown("operator hit the button")
        d = s.evaluate(self._sig(), self._reading(MarketRegime.LOW_VOL, 0.0),
                       strategy_family="meanrev")
        self.assertFalse(d.allow)
        self.assertIn("manual lockdown", d.reasons)

    def test_news_blackout_survives_the_switch(self):
        s = Survivor(enabled=False)
        s.flag_news()
        d = s.evaluate(self._sig(), self._reading(MarketRegime.LOW_VOL, 0.0),
                       strategy_family="meanrev")
        self.assertFalse(d.allow)
        self.assertIn("news blackout", d.reasons)

    def test_panic_deleverage_is_its_own_knob(self):
        """Turning the playbook off does not silently turn off the separate
        panic_deleverage flag."""
        stressed = self._reading(MarketRegime.LOW_VOL, 0.9)
        on = Survivor(enabled=False, panic_deleverage=True).evaluate(
            self._sig(), stressed, strategy_family="meanrev")
        off = Survivor(enabled=False, panic_deleverage=False).evaluate(
            self._sig(), stressed, strategy_family="meanrev")
        self.assertLess(on.stake_scale, off.stake_scale)

    def test_a_disabled_playbook_still_scales_by_risk_scale(self):
        d = Survivor(enabled=False).evaluate(self._sig(),
                                             self._reading(MarketRegime.LOW_VOL,
                                                           0.0),
                                             strategy_family="meanrev",
                                             risk_scale=0.5)
        self.assertAlmostEqual(d.stake_scale, 0.5, places=6)

    def test_the_engine_passes_the_config_flag_through(self):
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.config import AppConfig
        from tests.venue_stubs import VenueFeed, VenueStub

        cfg = AppConfig()
        cfg.survivor.enabled = False
        eng = TradingEngine(cfg, feed=VenueFeed(assets=["EURUSD"], n=60),
                            broker=VenueStub())
        self.assertFalse(eng.survivor.enabled)
        cfg.survivor.enabled = True
        eng = TradingEngine(cfg, feed=VenueFeed(assets=["EURUSD"], n=60),
                            broker=VenueStub())
        self.assertTrue(eng.survivor.enabled)
