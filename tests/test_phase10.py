"""Phase-10 tests — posterior Monte Carlo: what if the record is a liar?

The bootstrap lab resamples realized P&Ls and cannot express parameter
uncertainty.  `simulate_posterior` draws P(win) from the calibrated Beta
posterior and reports `p_edge_negative` — the honest probability that the
true edge is below the payout hurdle.
"""

from __future__ import annotations

import unittest

from cybertrade.cli import main
from cybertrade.quant.calibration import CalibrationTracker
from cybertrade.risk.montecarlo import simulate_posterior


class TestPosteriorMonteCarlo(unittest.TestCase):
    def test_strong_record_is_mostly_honest(self):
        r = simulate_posterior(80, 20, payout=0.85, runs=400, horizon=100, seed=7)
        self.assertLess(r.p_edge_negative, 0.05)
        self.assertGreater(r.p50_terminal, r.starting_balance)

    def test_weak_record_shows_uncertainty(self):
        # Beta(14,10): plenty of posterior mass below the 0.5405 hurdle
        r = simulate_posterior(12, 8, payout=0.85, runs=400, horizon=100, seed=7)
        self.assertGreater(r.p_edge_negative, 0.1)

    def test_losing_record_flagged(self):
        r = simulate_posterior(20, 80, payout=0.85, runs=400, horizon=100, seed=7)
        self.assertGreater(r.p_edge_negative, 0.9)

    def test_zero_evidence_is_the_prior(self):
        r = simulate_posterior(0, 0, payout=0.85, runs=400, horizon=50, seed=7)
        # Beta(2,2): P(p < 0.5405) ≈ 0.56 — uncertainty is the point
        self.assertGreater(r.p_edge_negative, 0.35)
        self.assertLess(r.p_edge_negative, 0.75)

    def test_deterministic(self):
        a = simulate_posterior(50, 30, payout=0.85, runs=200, seed=11)
        b = simulate_posterior(50, 30, payout=0.85, runs=200, seed=11)
        self.assertEqual(a.p05_terminal, b.p05_terminal)
        self.assertEqual(a.p_edge_negative, b.p_edge_negative)

    def test_report_carries_p_edge(self):
        r = simulate_posterior(30, 30, payout=0.85, runs=100, seed=3)
        self.assertIn("p_edge_negative", r.to_dict())

    def test_verdict_downgrades_on_liar_risk(self):
        r = simulate_posterior(80, 20, payout=0.85, runs=100, seed=3)
        self.assertNotIn("RUIN", r.verdict())
        r.p_edge_negative = 0.6  # isolate the clause
        self.assertIn("RUIN", r.verdict())

    def test_summary_mentions_edge_risk(self):
        r = simulate_posterior(12, 8, payout=0.85, runs=100, seed=3)
        self.assertIn("P(edge<0)", r.summary_text())


class TestTrackerEvidence(unittest.TestCase):
    def test_evidence_aggregates(self):
        t = CalibrationTracker()
        for _ in range(5):
            t.observe("a", 0.8, True)
        for _ in range(3):
            t.observe("b", 0.8, False)
        self.assertEqual(t.evidence(), (5, 3))
        self.assertEqual(t.evidence_for("a"), (5, 0))
        self.assertEqual(t.evidence_for("missing"), (0, 0))


class TestCliPosterior(unittest.TestCase):
    def test_cli_posterior_mode(self):
        self.assertEqual(
            main(["montecarlo", "--wins", "80", "--losses", "20", "--payout", "0.85"]),
            0,
        )


if __name__ == "__main__":
    unittest.main()
