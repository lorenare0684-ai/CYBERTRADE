"""Phase-5 tests — the calibrated edge gate and adaptive expiry selection.

The backtest lab is gone, so the gate is exercised where it now lives: on
the live engine, one signal at a time, with the venue stub as the broker.
"""

from __future__ import annotations

import time
import unittest

from cybertrade.config import AppConfig, ConfigError
from cybertrade.quant.expiry import choose_expiry, est_vol_per_bar
from cybertrade.bot.engine import TradingEngine
from cybertrade.constants import Side
from cybertrade.data.models import Signal, Tick
from cybertrade.regime.detector import RegimeReading
from tests.venue_stubs import VenueFeed, VenueStub

ASSET = "EURUSD_otc"


class TestExpiryChooser(unittest.TestCase):
    def test_vol_estimate(self):
        self.assertEqual(est_vol_per_bar([1.0] * 30), 0.0)
        self.assertLess(len([1.0, 1.01]), 3)  # series sanity
        wiggly = est_vol_per_bar([1.0 + 0.002 * ((i % 4) - 1.5) for i in range(30)])
        self.assertGreater(wiggly, 0.0)

    def test_fallbacks(self):
        # too little data -> default
        self.assertEqual(
            choose_expiry("call", 1.0, 0.85, [1.0], [30.0, 60.0], 0.7, default=45.0),
            45.0,
        )
        # unknown side -> default
        self.assertEqual(
            choose_expiry(
                "sideways", 1.0, 0.85, [1.0, 1.01, 1.02], [30.0], 0.7, default=30.0
            ),
            30.0,
        )
        # no candidates -> default
        self.assertEqual(
            choose_expiry("call", 1.0, 0.85, [1.0, 1.01, 1.02], [], 0.7, default=30.0),
            30.0,
        )

    def test_pick_within_candidates(self):
        closes = [1.0 + 0.001 * ((i % 6) - 2.5) for i in range(80)]
        pick = choose_expiry(
            "call", closes[-1], 0.85, closes, [30.0, 60.0, 120.0], 0.7, default=60.0
        )
        self.assertIn(pick, [30.0, 60.0, 120.0])

    def test_deterministic(self):
        closes = [1.0 + 0.001 * ((i % 6) - 2.5) for i in range(80)]
        a = choose_expiry("put", 1.0, 0.85, closes, [30.0, 90.0], 0.6, default=30.0)
        b = choose_expiry("put", 1.0, 0.85, closes, [30.0, 90.0], 0.6, default=30.0)
        self.assertEqual(a, b)


class TestLiveEdgeGate(unittest.TestCase):
    """The gate the backtester used to measure now runs on the live engine:
    a hard min_edge band must trade less than no gate at all."""

    def _engine(self, gate, min_edge):
        cfg = AppConfig()
        cfg.risk.edge_gate = gate
        cfg.risk.min_edge = min_edge
        cfg.risk.win_rate_floor = 0.0
        return TradingEngine(cfg, feed=VenueFeed(assets=[ASSET]),
                             broker=VenueStub(balance=1000.0))

    def _trade(self, gate, min_edge, confidence=0.9):
        eng = self._engine(gate, min_edge)
        eng.boot()
        try:
            eng.broker.on_tick(Tick(asset=ASSET, price=1.10))
            sig = Signal(asset=ASSET, side=Side.CALL, confidence=confidence,
                         strategy="alpha", ts=time.time())
            return eng._try_execute(sig, RegimeReading())
        finally:
            eng.shutdown()

    def test_hard_gate_vetoes_a_low_confidence_claim(self):
        self.assertFalse(self._trade("hard", 0.95))

    def test_no_gate_lets_the_same_claim_through(self):
        self.assertTrue(self._trade("off", 0.0))


class TestExpiryConfig(unittest.TestCase):
    def test_expiry_select_validation(self):
        cfg = AppConfig()
        cfg.risk.expiry_select = "bogus"
        with self.assertRaises(ConfigError):
            cfg.validate()

    def test_expiry_candidates_validation(self):
        cfg = AppConfig()
        cfg.risk.expiry_candidates = []
        with self.assertRaises(ConfigError):
            cfg.validate()
        cfg.risk.expiry_candidates = [0]
        with self.assertRaises(ConfigError):
            cfg.validate()


if __name__ == "__main__":
    unittest.main()
