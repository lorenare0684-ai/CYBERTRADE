"""Phase-4 tests — the quant edge layer.

Covers binary-options payout math, confidence calibration, order-flow
microstructure, tape forensics, orderflow strategies, and the engine's
calibrated edge gate: the wall between the bot and structural negative EV.
"""

from __future__ import annotations

import tempfile
import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig, ConfigError
from cybertrade.constants import MarketRegime, Side
from cybertrade.data.models import Candle, Settlement, TradeRecord
from cybertrade.data.tape import TapeRecorder, latest_tape, replay
from cybertrade.events import Topic, default_bus
from cybertrade.indicators.orderflow import (
    TickFlow,
    VolumeProfile,
    cumulative_delta,
    delta_divergence,
    tick_imbalance,
)
from cybertrade.quant.binary import (
    QuantError,
    best_expiry,
    breakeven_winrate,
    edge_of,
    expiry_edge_profile,
    kelly_fraction_for,
    kelly_stake,
    probability_itm,
    required_winrate,
)
from cybertrade.quant.calibration import CalibrationTracker, ReliabilityBucket
from cybertrade.regime.detector import RegimeReading
from cybertrade.strategies.base import StrategyContext
from cybertrade.strategies.orderflow import AbsorptionFade, ImbalanceMomentum, POCReversion
from cybertrade.utils import timex


class TestBinaryMath(unittest.TestCase):
    def test_breakeven_is_exact_hurdle(self):
        self.assertAlmostEqual(breakeven_winrate(0.85), 1.0 / 1.85, places=6)
        self.assertAlmostEqual(breakeven_winrate(0.80), 1.0 / 1.80, places=6)
        self.assertAlmostEqual(breakeven_winrate(1.0), 0.5, places=6)

    def test_edge_of_identity(self):
        self.assertAlmostEqual(edge_of(0.55, 0.85), 0.55 * 1.85 - 1.0, places=6)
        self.assertLess(edge_of(breakeven_winrate(0.85) - 0.01, 0.85), 0.0)

    def test_required_winrate_clears_min_edge(self):
        p = required_winrate(0.85, 0.02)
        self.assertGreaterEqual(edge_of(p, 0.85), 0.02 - 1e-9)

    def test_kelly_zero_at_breakeven(self):
        self.assertAlmostEqual(kelly_fraction_for(breakeven_winrate(0.85), 0.85), 0.0, places=6)
        self.assertLessEqual(kelly_fraction_for(breakeven_winrate(0.85) - 0.05, 0.85), 0.0)

    def test_kelly_fraction_is_edge_over_b(self):
        self.assertAlmostEqual(kelly_fraction_for(0.6, 0.85), edge_of(0.6, 0.85) / 0.85, places=6)

    def test_kelly_stake_smoke_value(self):
        # Ground truth from the Phase-4 smoke run (fractional Kelly default).
        self.assertAlmostEqual(kelly_stake(0.6, 0.85, 1000.0), 32.35, places=1)

    def test_probability_itm_atm_is_half(self):
        self.assertAlmostEqual(probability_itm("call", 1.0, 1.0, 60, 0.0006), 0.5, places=6)
        self.assertAlmostEqual(probability_itm("put", 1.0, 1.0, 60, 0.0006), 0.5, places=6)

    def test_probability_itm_directional(self):
        pc_up = probability_itm("call", 1.001, 1.0, 60, 0.0006)
        pc_flat = probability_itm("call", 1.0, 1.0, 60, 0.0006)
        pp_up = probability_itm("put", 1.001, 1.0, 60, 0.0006)
        self.assertGreater(pc_up, pc_flat)
        self.assertLess(pp_up, 0.5)

    def test_probability_itm_sigma_pulls_toward_half(self):
        low = probability_itm("call", 0.995, 1.0, 60, 0.0002)
        high = probability_itm("call", 0.995, 1.0, 60, 0.002)
        self.assertGreater(high, low)  # OTM call: more vol → more hope

    def test_expiry_profile_and_best(self):
        expiries = [30.0, 60.0, 120.0, 240.0]
        prof = expiry_edge_profile("call", 1.0, 0.85, expiries, 0.0004, 0.0)
        self.assertEqual(len(prof), len(expiries))
        best = best_expiry("call", 1.0, 0.85, expiries, 0.0004, 0.0, default=60.0)
        self.assertIn(best, expiries + [60.0])

    def test_quant_error_on_bad_side(self):
        with self.assertRaises(QuantError):
            probability_itm("sideways", 1.0, 1.0, 60, 0.0006)


class TestCalibration(unittest.TestCase):
    def test_cold_start_shrinks_to_prior(self):
        t = CalibrationTracker()
        self.assertAlmostEqual(t.p_for("s", 0.7), 0.6, places=6)  # 0.5 + 0.5*(c-0.5)
        self.assertAlmostEqual(t.p_for("s", 0.5), 0.5, places=6)

    def test_wins_raise_p(self):
        t = CalibrationTracker()
        for _ in range(10):
            t.observe("s", 0.7, True)
        self.assertGreater(t.p_for("s", 0.7), 0.6)

    def test_losses_lower_p(self):
        t = CalibrationTracker()
        for _ in range(30):
            t.observe("s", 0.7, False)
        # Smoke ground truth: Beta(2,2) posterior blended with raw 0.6.
        self.assertAlmostEqual(t.p_for("s", 0.7), 0.275, places=2)

    def test_strategies_independent(self):
        t = CalibrationTracker()
        for _ in range(12):
            t.observe("winner", 0.8, True)
            t.observe("loser", 0.8, False)
        self.assertGreater(t.p_for("winner", 0.8), t.p_for("loser", 0.8))

    def test_summary_and_gap(self):
        t = CalibrationTracker()
        for _ in range(6):
            t.observe("s", 0.7, True)
        summary = t.summary()
        self.assertIn("calibration_gap", summary)
        self.assertEqual(t.observations, 6)
        self.assertIsInstance(t.calibration_gap(), float)

    def test_bucket_shrink(self):
        b = ReliabilityBucket()
        b.observe(True)
        self.assertAlmostEqual(b.hit_rate, 1.0)
        self.assertGreater(b.posterior(), 0.5)
        self.assertLess(b.posterior(), 1.0)  # prior keeps it honest

    def test_edge_config_validation(self):
        cfg = AppConfig()
        self.assertEqual(cfg.risk.edge_gate, "scale")
        self.assertAlmostEqual(cfg.risk.min_edge, 0.02)
        cfg.risk.edge_gate = "bogus"
        with self.assertRaises(ConfigError):
            cfg.validate()


class TestOrderFlow(unittest.TestCase):
    def test_tick_imbalance_symmetric(self):
        prices = [1.0, 1.01, 1.0, 1.01, 1.0, 1.01, 1.0]  # 6 changes: 3 up, 3 down
        self.assertAlmostEqual(tick_imbalance(prices), 0.0, places=6)

    def test_tick_imbalance_trending(self):
        prices = [1.0 + 0.001 * i for i in range(20)]
        self.assertGreater(tick_imbalance(prices), 0.9)

    def test_cumulative_delta_sizes_dominate(self):
        cd = cumulative_delta([1.0, 2.0, 3.0, 2.0], sizes=[1, 1, 1, 10])
        self.assertEqual(len(cd), 3)  # one delta per tick change
        self.assertLess(cd[-1], 0.0)  # one huge sell swamps three small buys

    def test_delta_divergence_bounded(self):
        prices = [1.0 + 0.001 * ((i % 5) - 2) for i in range(40)]
        d = delta_divergence(prices)
        self.assertIsInstance(d, float)
        self.assertGreaterEqual(d, -1.0)
        self.assertLessEqual(d, 1.0)

    def test_volume_profile_poc(self):
        vp = VolumeProfile()
        for _ in range(10):
            vp.add(1.00, 1.0)
        for _ in range(2):
            vp.add(1.05, 1.0)
        self.assertAlmostEqual(vp.poc, 1.00, places=2)
        lo, hi = vp.value_area()
        self.assertLessEqual(lo, vp.poc)
        self.assertGreaterEqual(hi, vp.poc)

    def test_volume_profile_stretch(self):
        vp = VolumeProfile()
        for i in range(20):
            vp.add(1.0 + 0.001 * (i % 3), 1.0)
        self.assertGreater(vp.stretch(1.05), vp.stretch(1.0))

    def test_tick_flow_streaming(self):
        tf = TickFlow()
        for i in range(30):
            tf.on_tick(1.0 + 0.0005 * i)
        self.assertGreater(tf.delta, 0.0)
        self.assertGreater(tf.imbalance(), 0.0)
        snap = tf.snapshot()
        self.assertIsInstance(snap, dict)
        self.assertGreaterEqual(len(snap), 3)


def _candles_up(n: int = 80) -> list:
    out = []
    ts = 1_700_000_000.0
    for i in range(n):
        o = 1.0 + 0.0005 * i
        out.append(Candle("EURUSD_otc", 60, ts + 60 * i, o, o + 0.0004,
                          o - 0.0004, o + 0.0003, 1.0, True))
    return out


def _flow_up(n: int = 40) -> TickFlow:
    tf = TickFlow()
    for i in range(n):
        tf.on_tick(1.0 + 0.0005 * i)
    return tf


def _ctx(flow: object) -> StrategyContext:
    return StrategyContext(
        asset="EURUSD_otc",
        candles=_candles_up(),
        regime=RegimeReading(regime=MarketRegime.BULL_TREND, confidence=0.6,
                             trend_strength=0.5, trend_direction=1),
        timeframe_seconds=60,
        expiry_seconds=60,
        payout=0.85,
        ts=timex.now(),
        extra={"flow": flow},
    )


class TestOrderflowStrategies(unittest.TestCase):
    def test_no_flow_no_signal(self):
        ctx = _ctx(None)
        ctx.extra = {}
        for cls in (ImbalanceMomentum, AbsorptionFade, POCReversion):
            self.assertIsNone(cls().generate(ctx), cls.__name__)

    def test_imbalance_momentum_direction(self):
        sig = ImbalanceMomentum().generate(_ctx(_flow_up()))
        if sig is not None:
            self.assertEqual(sig.side, Side.CALL)
            self.assertGreater(sig.confidence, 0.0)

    def test_outputs_are_signals_or_none(self):
        from cybertrade.data.models import Signal

        ctx = _ctx(_flow_up())
        for cls in (ImbalanceMomentum, AbsorptionFade, POCReversion):
            out = cls().generate(ctx)
            self.assertTrue(out is None or isinstance(out, Signal), cls.__name__)


class TestTape(unittest.TestCase):
    def test_write_replay_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = TapeRecorder(tmp)
            r.write("tick", {"a": 1})
            r.write("candle", {"a": 2})
            self.assertEqual(r.stats()["written"], 2)
            path = latest_tape(tmp)
            self.assertIsNotNone(path)
            rows = list(replay(path))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["kind"], "tick")
            only = list(replay(path, kinds=["candle"]))
            self.assertEqual(len(only), 1)

    def test_attach_records_bus_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = TapeRecorder(tmp)
            r.attach()
            try:
                default_bus.publish(Topic.SIGNAL, {"msg": "hello tape"}, source="test")
                self.assertGreaterEqual(r.stats()["written"], 1)
            finally:
                r.detach()
                r.close()

    def test_disabled_recorder_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = TapeRecorder(tmp, enabled=False)
            r.write("tick", {"a": 1})
            self.assertEqual(r.stats()["written"], 0)
            self.assertIsNone(latest_tape(tmp))

    def test_latest_tape_missing_dir(self):
        self.assertIsNone(latest_tape("/nonexistent-tapes-dir-xyz"))


class TestEdgeGate(unittest.TestCase):
    def _engine(self, gate: str, min_edge: float) -> TradingEngine:
        cfg = AppConfig()
        cfg.risk.edge_gate = gate
        cfg.risk.min_edge = min_edge
        return TradingEngine(cfg)

    def _signal(self, engine: TradingEngine, conf: float = 0.9):
        return Signalish(engine, conf)

    def test_engine_builds_edge_layer(self):
        eng = self._engine("scale", 0.02)
        self.assertEqual(set(eng.flow.keys()), set(eng.feed.assets))
        self.assertIsInstance(eng.calibrator, CalibrationTracker)
        self.assertEqual(eng.edge_rejects, 0)

    def test_hard_gate_vetoes_thin_edge(self):
        eng = self._engine("hard", 0.5)  # impossible hurdle while cold
        sig = Signalish(eng, 0.9)
        ok = eng._try_execute(sig, _reading())
        self.assertFalse(ok)
        self.assertEqual(eng.edge_rejects, 1)

    def test_negative_edge_always_vetoes_even_when_off(self):
        eng = self._engine("off", 0.0)
        # Teach the calibrator that this strategy is a loser at high confidence.
        for _ in range(40):
            eng.calibrator.observe("edge_test", 0.9, False)
        sig = Signalish(eng, 0.9)
        ok = eng._try_execute(sig, _reading())
        self.assertFalse(ok)
        self.assertGreaterEqual(eng.edge_rejects, 1)

    def test_settle_feeds_calibrator(self):
        eng = self._engine("scale", 0.02)
        settle = Settlement("f1", "o1", "EURUSD_otc", Side.CALL, 1.1, 1.2,
                            10.0, 0.85, True)
        eng._on_settle(TradeRecord(settlement=settle, strategy="edge_test"))
        self.assertEqual(eng.calibrator.observations, 1)


class Signalish:
    """Minimal Signal stand-in bound to an engine's first asset."""

    def __init__(self, engine: TradingEngine, confidence: float):
        from cybertrade.data.models import Signal

        self._sig = Signal(
            asset=engine.feed.assets[0],
            side=Side.CALL,
            confidence=confidence,
            strategy="edge_test",
            expiry_seconds=60,
            price=1.0,
            ts=timex.now(),
        )

    def __getattr__(self, name):
        return getattr(self._sig, name)


def _reading() -> RegimeReading:
    return RegimeReading(regime=MarketRegime.RANGE, confidence=0.5, stress=0.1,
                         trend_strength=0.1, range_score=0.6)


if __name__ == "__main__":
    unittest.main()
