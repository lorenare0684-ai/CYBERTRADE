"""Phase-5 tests — the calibrated edge gate under gauntlet conditions and
adaptive expiry selection.

The Phase-4 gate now runs inside the backtester too, so the GAUNTLET
measures what calibration actually buys: fewer negative-EV entries.
"""

from __future__ import annotations

import unittest

from cybertrade.backtest.engine import Backtester
from cybertrade.config import AppConfig, ConfigError
from cybertrade.quant.expiry import choose_expiry, est_vol_per_bar


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


class TestBacktestEdgeGate(unittest.TestCase):
    def _run(self, gate: str, min_edge: float, expiry_select: str = "signal"):
        cfg = AppConfig()
        cfg.risk.edge_gate = gate
        cfg.risk.min_edge = min_edge
        cfg.risk.expiry_select = expiry_select
        bt = Backtester(cfg)
        return bt, bt.run_scenario("bull_trend", bars=300, seed=3)

    def test_hard_gate_trades_less_than_off(self):
        _, res_off = self._run("off", 0.0)
        _, res_hard = self._run("hard", 0.4)
        self.assertGreater(res_off.report.trades, 0)
        self.assertLess(res_hard.report.trades, res_off.report.trades)
        self.assertGreater(res_hard.edge_rejects, 0)

    def test_scale_gate_still_trades(self):
        _, res = self._run("scale", 0.02)
        self.assertGreater(res.report.trades, 0)

    def test_negative_ev_always_vetoes_even_when_off(self):
        # with gate "off" the min_edge band is ignored entirely —
        # only the unconditional negative-EV veto remains
        _, res_wide = self._run("off", 0.9)
        _, res_zero = self._run("off", 0.0)
        self.assertEqual(res_wide.report.trades, res_zero.report.trades)

    def test_calibrator_observes_settlements(self):
        bt, res = self._run("scale", 0.02)
        self.assertEqual(bt.calibrator.observations, len(res.trades))

    def test_adaptive_expiry_runs(self):
        _, res = self._run("scale", 0.02, expiry_select="adaptive")
        self.assertGreater(res.report.trades, 0)

    def test_result_carries_edge_rejects(self):
        _, res = self._run("hard", 0.4)
        self.assertIn("edge_rejects", res.to_dict())


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
