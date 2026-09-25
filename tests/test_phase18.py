"""Phase-18 tests — the decay watch: stale edges get flagged in real time.

`decay_check` averages everyone together, so one strategy bleeding out
hides behind another's hot streak. `strategy_decay` watches each record
separately; the engine fires a once-per-spell alert (Topic.ALERT + health)
on every settle and re-arms after recovery.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from cybertrade.data.models import Settlement, Side, TradeRecord
from cybertrade.journal import TradeJournal, strategy_decay
from tests.venue_stubs import VenueFeed, VenueStub


def _rec(i: int, won: bool, strategy: str = "alpha") -> TradeRecord:
    return TradeRecord(
        settlement=Settlement(
            fill_id=f"f{i}", order_id=f"o{i}", asset="EURUSD_otc", side=Side.CALL,
            strike=1.0, expiry_price=1.01 if won else 0.99,
            stake=10.0, payout=0.85, won=won, ts=1000.0 + i, id=f"set{i}",
        ),
        strategy=strategy, regime="range",
    )


def _rigged(strategy: str = "alpha", decaying: bool = True):
    """20 trades chronological: i=0..19. Newest = i=19.

    decaying: prior window (i=10..19 newest-first = 19..10) mostly WINS,
    recent window (9..0) mostly LOSSES — wait, newest-first ordering means
    recent == highest i. So losses go on the HIGHEST i for decay.
    """
    j = TradeJournal(os.path.join(tempfile.mkdtemp(), "j.db"))
    for i in range(20):
        if decaying:
            won = i < 12          # i=12..19 (recent) lose; 0..11 (prior) win
        else:
            won = i % 2 == 0
        j.record_trade(_rec(i, won, strategy))
    return j


class TestStrategyDecay(unittest.TestCase):
    def test_flags_decaying_strategy(self):
        rows = strategy_decay(_rigged("alpha", decaying=True), window=8)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["strategy"], "alpha")
        self.assertTrue(row["decaying"])
        self.assertLess(row["delta"], -0.12)

    def test_stable_strategy_not_flagged(self):
        rows = strategy_decay(_rigged("alpha", decaying=False), window=8)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["decaying"])

    def test_needs_full_windows(self):
        j = TradeJournal(os.path.join(tempfile.mkdtemp(), "j.db"))
        for i in range(10):
            j.record_trade(_rec(i, True))
        self.assertEqual(strategy_decay(j, window=8), [])  # only one window

    def test_worst_first(self):
        j = _rigged("toxic", decaying=True)
        for i in range(20):
            j.record_trade(_rec(100 + i, i % 2 == 0, strategy="mild"))
        rows = strategy_decay(j, window=8)
        self.assertEqual(rows[0]["strategy"], "toxic")


class TestEngineDecayAlert(unittest.TestCase):
    def test_alert_fires_once_and_rearms(self):
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.config import AppConfig
                
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.journal_path = os.path.join(tmp, "journal.db")
            cfg.calibration_path = os.path.join(tmp, "cal.json")
            eng = TradingEngine(cfg, feed=VenueFeed(),
                                broker=VenueStub())
            eng.boot()
            # prior window: 8 wins, then 13 losses — window=10 needs 20 rows
            # before any row speaks (10 recent + 10 prior).
            for i in range(8):
                eng._on_settle(_rec(i, True))
            for i in range(8, 20):
                eng._on_settle(_rec(i, False))
            self.assertIn("alpha", eng._decay_alerted)
            # one more loss must NOT re-alert (once per spell)
            eng._on_settle(_rec(20, False))
            self.assertIn("alpha", eng._decay_alerted)
            # recovery: a win streak clears the flag (re-arm)
            for i in range(30, 46):
                eng._on_settle(_rec(i, True))
            self.assertNotIn("alpha", eng._decay_alerted)
            eng.shutdown()

    def test_web_block(self):
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.config import AppConfig
        from cybertrade.web.server import EngineHub

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.journal_path = os.path.join(tmp, "journal.db")
            cfg.calibration_path = os.path.join(tmp, "cal.json")
            eng = TradingEngine(cfg, feed=VenueFeed(),
                                broker=VenueStub())
            eng.boot()
            for i in range(20):
                eng._on_settle(_rec(i, i < 12))
            hub = EngineHub(eng, cfg)
            block = hub._journal_block()
            self.assertTrue(block["available"])
            self.assertEqual(block["trades"], 20)
            self.assertTrue(block["decaying"] and block["decaying"][0]["strategy"] == "alpha")
            eng.shutdown()


if __name__ == "__main__":
    unittest.main()
