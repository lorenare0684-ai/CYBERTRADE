"""Phase-12 tests — bet against the liar: posterior-pessimistic sizing.

Kelly on a lucky thin record is how accounts die politely. ``size_stake``
now receives the LOWER Beta quantile of the evidence (``p_win_lower``), so a
record sizes like what it might really be. The edge gate keeps the mean —
gating is about expected value, sizing is about uncertainty.
"""

from __future__ import annotations

import unittest

from cybertrade.config import AppConfig
from cybertrade.quant.calibration import CalibrationTracker, beta_quantile
from cybertrade.risk.manager import RiskManager

from tests.venue_stubs import VenueFeed, VenueStub
CONF = 0.8  # lives in bucket [0.80, 0.85) -> index 6
RISK = AppConfig().risk


def _tracker(wins: int, losses: int, strategy: str = "alpha") -> CalibrationTracker:
    t = CalibrationTracker()
    rows = [{"wins": 0, "total": 0}] * 10
    rows[6] = {"wins": wins, "total": wins + losses}
    t.update_from({
        "observed": wins + losses,
        "global": [dict(r) for r in rows],
        "by_strategy": {strategy: [dict(r) for r in rows]},
        "by_regime": {},
    })
    return t


class TestBetaQuantile(unittest.TestCase):
    def test_strong_record_pessimism_is_mild(self):
        q = beta_quantile(80, 20, 0.05)
        self.assertGreater(q, 0.65)
        self.assertLess(q, 0.79)

    def test_thin_record_pessimism_is_severe(self):
        q = beta_quantile(5, 3, 0.05)
        self.assertLess(q, 0.5)

    def test_prior_is_wide(self):
        q = beta_quantile(0, 0, 0.05)
        self.assertLess(q, 0.3)
        self.assertGreater(q, 0.0)

    def test_mean_path_and_determinism(self):
        self.assertAlmostEqual(beta_quantile(80, 20, 0.0), 82 / 104)
        self.assertEqual(beta_quantile(30, 30, 0.05), beta_quantile(30, 30, 0.05))


class TestPWinLower(unittest.TestCase):
    def test_lower_never_beats_mean(self):
        t = _tracker(30, 30)
        self.assertLessEqual(t.p_win_lower("alpha", CONF), t.p_win_for("alpha", CONF))

    def test_cold_start_falls_back_to_opinion(self):
        t = CalibrationTracker()
        self.assertEqual(t.p_win_lower("nobody", CONF), t.p_win_for("nobody", CONF))

    def test_liar_trap_thin_record_gets_no_kelly(self):
        t = _tracker(5, 3)
        mean = t.p_win_for("alpha", CONF)
        low = t.p_win_lower("alpha", CONF)
        self.assertGreater(mean, 0.55)     # the blend flatters
        self.assertLess(low, 0.52)         # the quantile does not
        from cybertrade.config import RiskConfig
        rm = RiskManager(RiskConfig(vol_target_enabled=False))
        s_mean = rm.size_stake(balance=1000, payout=0.85, confidence=CONF,
                               win_rate=mean, drawdown=0.0)
        s_low = rm.size_stake(balance=1000, payout=0.85, confidence=CONF,
                              win_rate=low, drawdown=0.0)
        self.assertEqual(s_mean.model, "kelly")
        self.assertNotEqual(s_low.model, "kelly")
        self.assertLess(s_low.stake, s_mean.stake)

    def test_earned_record_still_kellys(self):
        t = _tracker(80, 20)
        low = t.p_win_lower("alpha", CONF)
        self.assertGreater(low, 0.6)
        from cybertrade.config import RiskConfig
        rm = RiskManager(RiskConfig(vol_target_enabled=False))
        s = rm.size_stake(balance=1000, payout=0.85, confidence=CONF,
                          win_rate=low, drawdown=0.0)
        self.assertEqual(s.model, "kelly")

    def test_zero_quantile_is_mean_path(self):
        t = _tracker(5, 3)
        self.assertEqual(
            t.p_win_lower("alpha", CONF, quantile=0.0),
            t.p_win_for("alpha", CONF),
        )

    def test_votes_min_rule_survives(self):
        t = CalibrationTracker()
        rows_good = [{"wins": 0, "total": 0}] * 10
        rows_good[6] = {"wins": 80, "total": 100}
        rows_bad = [{"wins": 0, "total": 0}] * 10
        rows_bad[6] = {"wins": 2, "total": 10}
        t.update_from({
            "observed": 110,
            "global": [{"wins": 82, "total": 110}] + [{"wins": 0, "total": 0}] * 9,
            "by_strategy": {"good": rows_good, "bad": rows_bad},
            "by_regime": {},
        })
        with_votes = t.p_win_lower(
            "good", CONF,
            votes=[{"strategy": "good", "confidence": 0.8},
                   {"strategy": "bad", "confidence": 0.8}],
        )
        self.assertLess(with_votes, t.p_win_lower("good", CONF))

    def test_regime_path_runs(self):
        t = _tracker(20, 10)
        p = t.p_win_lower("alpha", CONF, regime="bull_trend")
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)


class TestConfigGate(unittest.TestCase):
    def test_default_and_validation(self):
        self.assertEqual(RISK.kelly_quantile, 0.05)
        from cybertrade.config import RiskConfig
        from cybertrade.exceptions import ConfigError
        with self.assertRaises(ConfigError):
            RiskConfig(kelly_quantile=0.6).validate()
        with self.assertRaises(ConfigError):
            RiskConfig(kelly_quantile=-0.1).validate()


if __name__ == "__main__":
    unittest.main()
