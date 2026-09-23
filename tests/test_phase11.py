"""Phase-11 tests — the calibrator that survives its process + liar ledger.

Learning that evaporates on restart is a demo, not a survivor.  The ledger
persists (`save`/`load`), refuses to clobber real data with empty sessions,
and `honesty()` rates every strategy record with the P10 question: is this
record a winner, or a loser in a winner's clothes?
"""

from __future__ import annotations

import os
import tempfile
import unittest

from cybertrade.cli import main
from cybertrade.config import AppConfig
from cybertrade.quant.calibration import CalibrationTracker
from cybertrade.risk.montecarlo import simulate_posterior


def _ledger_dict(a_w: int = 80, a_l: int = 20, b_w: int = 12, b_l: int = 8):
    return {
        "version": 1,
        "observed": (a_w + a_l) + (b_w + b_l),
        "global": [
            {"wins": a_w + b_w, "total": a_w + a_l + b_w + b_l},
            {"wins": 0, "total": 0},
            {"wins": 0, "total": 0},
            {"wins": 0, "total": 0},
        ],
        "by_strategy": {
            "alpha": [
                {"wins": a_w, "total": a_w + a_l},
                {"wins": 0, "total": 0},
                {"wins": 0, "total": 0},
                {"wins": 0, "total": 0},
            ],
            "weak": [
                {"wins": b_w, "total": b_w + b_l},
                {"wins": 0, "total": 0},
                {"wins": 0, "total": 0},
                {"wins": 0, "total": 0},
            ],
        },
        "by_regime": {},
    }


class TestLedgerRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "cal.json")

    def test_save_load_roundtrip(self):
        t = CalibrationTracker()
        for _ in range(6):
            t.observe("macd", 0.8, True, regime="bull_trend")
        for _ in range(4):
            t.observe("macd", 0.8, False)
        t.observe("rsi", 0.6, True)
        self.assertTrue(t.save(self.path))

        u = CalibrationTracker()
        self.assertTrue(u.load(self.path))
        self.assertEqual(t.evidence(), u.evidence())
        self.assertEqual(t.observations, u.observations)
        self.assertEqual(t.p_for("macd", 0.8), u.p_for("macd", 0.8))
        self.assertEqual(t.p_regime("macd", "bull_trend", 0.8),
                         u.p_regime("macd", "bull_trend", 0.8))
        st, su = t.summary(), u.summary()
        self.assertEqual([r["strategy"] for r in st["strategies"]],
                         [r["strategy"] for r in su["strategies"]])

    def test_load_missing_is_false(self):
        self.assertFalse(CalibrationTracker().load(os.path.join(self.tmp, "nope.json")))

    def test_empty_save_never_clobbers(self):
        t = CalibrationTracker()
        t.observe("x", 0.7, True)
        self.assertTrue(t.save(self.path))
        before = open(self.path).read()
        empty = CalibrationTracker()
        self.assertFalse(empty.save(self.path))  # zero obs + file exists → refuse
        self.assertEqual(before, open(self.path).read())

    def test_from_dict_tolerant(self):
        t = CalibrationTracker.from_dict(_ledger_dict()) if hasattr(
            CalibrationTracker, "from_dict") else None
        if t is None:
            t = CalibrationTracker()
            t.update_from(_ledger_dict())
        self.assertEqual(t.evidence(), (92, 28))


class TestHonestyLedger(unittest.TestCase):
    def setUp(self):
        self.t = CalibrationTracker()
        self.t.update_from(_ledger_dict())

    def test_liar_flagged_and_honest_not(self):
        rows = {r["strategy"]: r for r in self.t.honesty(0.85, runs=600)}
        self.assertLess(rows["alpha"]["p_edge_negative"], 0.1)   # 80/20 earned it
        self.assertFalse(rows["alpha"]["liar"])
        self.assertGreater(rows["weak"]["p_edge_negative"], 0.25)  # 12/8 uncertain
        liar = CalibrationTracker()
        liar.update_from(_ledger_dict(a_w=20, a_l=80))
        r = {x["strategy"]: x for x in liar.honesty(0.85, runs=600)}["alpha"]
        self.assertTrue(r["liar"])
        self.assertGreater(r["p_edge_negative"], 0.9)

    def test_worst_first_sort(self):
        rows = self.t.honesty(0.85, runs=600)
        self.assertEqual(rows[0]["strategy"], "weak")  # higher p_edge than alpha
        ps = [r["p_edge_negative"] for r in rows]
        self.assertEqual(ps, sorted(ps, reverse=True))

    def test_deterministic(self):
        a = self.t.honesty(0.85, runs=400)
        b = self.t.honesty(0.85, runs=400)
        self.assertEqual(a, b)

    def test_summary_carries_honesty_and_evidence(self):
        s = self.t.summary(0.85)
        self.assertIn("honesty", s)
        self.assertIn("evidence", s)
        self.assertEqual(s["evidence"], [92, 28])


class TestEnginePersistence(unittest.TestCase):
    def test_boot_restore_shutdown_save(self):
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.data.feed import SyntheticFeed
        from cybertrade.execution.paper import PaperBroker

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cal.json")
            seed = CalibrationTracker()
            seed.update_from(_ledger_dict())
            self.assertTrue(seed.save(path))

            cfg = AppConfig()
            cfg.calibration_path = path
            eng = TradingEngine(
                cfg, feed=SyntheticFeed(tick_interval=60.0), broker=PaperBroker()
            )
            eng.boot()
            self.assertEqual(eng.calibrator.evidence(), (92, 28))
            eng.calibrator.observe("late", 0.7, True)
            eng.shutdown()
            self.assertTrue(os.path.exists(path))

            again = CalibrationTracker()
            self.assertTrue(again.load(path))
            self.assertEqual(again.evidence_for("late"), (1, 0))


class TestCliCalibrate(unittest.TestCase):
    def test_cli_shows_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cal.json")
            t = CalibrationTracker()
            t.update_from(_ledger_dict())
            self.assertTrue(t.save(path))
            self.assertEqual(main(["calibrate", "--path", path, "--payout", "0.85"]), 0)

    def test_cli_missing_ledger(self):
        self.assertEqual(main(["calibrate", "--path", "/nonexistent/cal.json"]), 1)


class TestPooledPosteriorStillHonest(unittest.TestCase):
    def test_ledger_and_lab_agree_on_liar(self):
        # the ledger's flag and the P10 lab answer the same question
        t = CalibrationTracker()
        t.update_from(_ledger_dict(a_w=20, a_l=80))
        row = [r for r in t.honesty(0.85, runs=600) if r["strategy"] == "alpha"][0]
        report = simulate_posterior(20, 80, payout=0.85, runs=300, horizon=30, seed=5)
        self.assertTrue(row["liar"])
        self.assertGreater(report.p_edge_negative, 0.9)


if __name__ == "__main__":
    unittest.main()
