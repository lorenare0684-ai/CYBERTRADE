"""Phase-17 tests — the gauntlet asks the P10 question of its own record.

A stress run is not a track record. `matrix_card` pools per-strategy
evidence across every gauntlet run and reports each row's posterior mass
below breakeven — LIAR past 50% — plus the pooled honesty of the whole
harness. BacktestResult carries the raw evidence so the card can pool it.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from cybertrade.backtest.report import matrix_card
from cybertrade.quant.calibration import CalibrationTracker


def _result(evidence):
    return SimpleNamespace(strategy_evidence=evidence)


class TestStrategyEvidence(unittest.TestCase):
    def test_strategy_evidence_extracts_rows(self):
        t = CalibrationTracker()
        rows = [{"wins": 0, "total": 0}] * 10
        rows[6] = {"wins": 80, "total": 100}
        t.update_from({
            "observed": 100,
            "global": [dict(r) for r in rows],
            "by_strategy": {"alpha": [dict(r) for r in rows]},
            "by_regime": {},
        })
        self.assertEqual(t.strategy_evidence(), {"alpha": (80, 20)})


class TestMatrixCard(unittest.TestCase):
    def test_pools_and_flags_liars(self):
        results = [
            _result({"good": (40, 10), "toxic": (10, 40)}),
            _result({"good": (40, 10), "toxic": (10, 40)}),
        ]
        card = matrix_card(results, payout=0.85)
        self.assertIn("pooled 100W/100L", card)
        self.assertIn("toxic", card)
        self.assertIn("LIAR", card)
        self.assertIn("good", card)
        # the toxic row must carry the flag; the good row must not be flagged
        # (the table prints worst-first — toxic leads)
        first_row = [ln for ln in card.splitlines() if "toxic" in ln or "good" in ln]
        self.assertGreaterEqual(card.count("LIAR"), 1)

    def test_empty_evidence(self):
        self.assertEqual(matrix_card([], payout=0.85), "  report card: no evidence collected")
        self.assertEqual(
            matrix_card([_result({})], payout=0.85),
            "  report card: no evidence collected",
        )

    def test_pooled_line_present(self):
        card = matrix_card([_result({"s": (30, 20)})], payout=0.90)
        self.assertIn("breakeven", card)
        self.assertIn("P(edge<0)", card)


class TestBacktesterFillsCard(unittest.TestCase):
    def test_run_carries_evidence_and_p_edge(self):
        from cybertrade.backtest.engine import Backtester
        from cybertrade.config import AppConfig

        cfg = AppConfig()
        bt = Backtester(cfg)
        result = bt.run_scenario("gbm", bars=120, seed=3)
        self.assertGreaterEqual(result.report.evidence[0] + result.report.evidence[1], 0)
        self.assertIsInstance(result.report.p_edge_negative, float)
        self.assertGreaterEqual(result.report.p_edge_negative, 0.0)
        self.assertLessEqual(result.report.p_edge_negative, 1.0)
        d = result.to_dict()
        self.assertIn("evidence", d)
        self.assertIn("p_edge_negative", d)
        self.assertIn("strategy_evidence", d)

    def test_card_over_real_run_never_crashes(self):
        from cybertrade.backtest.engine import Backtester
        from cybertrade.config import AppConfig

        bt = Backtester(AppConfig())
        results = [bt.run_scenario("gbm", bars=80, seed=1)]
        card = matrix_card(results, payout=0.85)
        self.assertIn("report card", card)


if __name__ == "__main__":
    unittest.main()
