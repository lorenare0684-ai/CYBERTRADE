"""Phase-22 tests — the session clock: when you trade is a market condition.

`session_for` classifies the wall clock per asset class and returns a stake
multiplier; the engine multiplies it into `regime_scale` (thin tape, smaller
share), the cockpit HUD reads `state()["session"]`, and the journal breaks
P&L down by session so thin-hour edges are visible, not averaged away.
"""

from __future__ import annotations

import datetime
import inspect
import os
import tempfile
import unittest

from cybertrade.config import AppConfig
from cybertrade.data.feed import SyntheticFeed
from cybertrade.data.models import Settlement, Side, TradeRecord
from cybertrade.execution.paper import PaperBroker
from cybertrade.journal import TradeJournal, journal_report
from cybertrade.web.server import EngineHub, WebTerminal

from cybertrade.risk.sessions import (
    WEEKEND_SCALE,
    asset_class,
    session_for,
    session_report,
)
from cybertrade.bot.engine import TradingEngine


def _ts(hour: int, weekday: int = 1) -> float:
    """UTC wall-clock timestamp: weekday Mon=0, hour on that day."""
    day = datetime.date(2026, 3, 9) + datetime.timedelta(days=weekday)
    return datetime.datetime(
        day.year, day.month, day.day, hour, 0, tzinfo=datetime.timezone.utc
    ).timestamp()


def _rec(i: int, ts: float) -> TradeRecord:
    return TradeRecord(
        settlement=Settlement(
            fill_id=f"f{i}", order_id=f"o{i}", asset="EURUSD_otc",
            side=Side.CALL, strike=1.0, expiry_price=1.01, stake=10.0,
            payout=0.85, won=True, ts=ts, id=f"set{i}",
        ),
        strategy="alpha", regime="range",
    )


class TestSessionClassifier(unittest.TestCase):
    def test_asset_class_from_symbol(self):
        self.assertEqual(asset_class("EURUSD_otc"), "fx")
        self.assertEqual(asset_class("USDJPY_otc"), "fx")
        self.assertEqual(asset_class("BTCUSD_otc"), "crypto")
        self.assertEqual(asset_class("XAUUSD_otc"), "metal")
        self.assertEqual(asset_class("US500_otc"), "index")

    def test_session_boundaries_and_scales(self):
        # Tue 2026-03-10 — classic FX clock (UTC)
        self.assertEqual(session_for("EURUSD", _ts(11, 1)).name, "london")
        self.assertEqual(session_for("EURUSD", _ts(12, 1)).name, "overlap")
        self.assertEqual(session_for("EURUSD", _ts(19, 1)).name, "newyork")
        self.assertEqual(session_for("EURUSD", _ts(21, 1)).name, "offhours")
        # thick tape trades full size; offhours is discounted
        thick = session_for("EURUSD", _ts(13, 1))
        thin = session_for("EURUSD", _ts(21, 1))
        self.assertEqual(thick.scale, 1.0)
        self.assertEqual(thick.liquidity, "thick")
        self.assertLess(thin.scale, thick.scale)
        # index outside US hours is thinner than FX offhours-free overlap
        idx_asia = session_for("US500", _ts(3, 1))
        self.assertLessEqual(idx_asia.scale, 0.5)

    def test_weekend_floor_and_crypto_exempt(self):
        sat = _ts(13, 5)  # Saturday
        fx = session_for("EURUSD_otc", sat)
        self.assertTrue(fx.weekend)
        self.assertEqual(fx.scale, WEEKEND_SCALE)
        self.assertEqual(fx.liquidity, "thin")
        crypto = session_for("BTCUSD_otc", sat)
        self.assertFalse(crypto.weekend)   # 24/7 — no classic close
        self.assertGreater(crypto.scale, WEEKEND_SCALE)


class TestSessionWiring(unittest.TestCase):
    def _engine(self) -> TradingEngine:
        return TradingEngine(
            AppConfig(),
            feed=SyntheticFeed(tick_interval=60.0),
            broker=PaperBroker(starting_balance=1000.0),
        )

    def test_engine_session_report_in_state(self):
        eng = self._engine()
        rep = eng.session_report()
        self.assertIn(rep["name"],
                      ("asia", "london", "overlap", "newyork", "offhours"))
        self.assertLessEqual(rep["min_scale"], 1.0)
        self.assertIn(eng.feed.assets[0], rep["assets"])
        term = WebTerminal(EngineHub(eng), host="127.0.0.1", port=0)
        state = term.hub.state()
        self.assertIn("session", state)
        self.assertEqual(state["session"]["name"], rep["name"])

    def test_try_execute_multiplies_session_scale(self):
        """The stake seam: survivor family scale × session scale (source lock)."""
        src = inspect.getsource(TradingEngine._try_execute)
        self.assertIn("session_for(signal.asset, signal.ts).scale", src)
        self.assertIn("decision.stake_scale *", src)

    def test_session_report_is_scale_free(self):
        rep = session_report(["EURUSD_otc", "BTCUSD_otc"], _ts(13, 1))
        self.assertEqual(rep["min_scale"], 1.0)   # Tuesday overlap, full size
        self.assertFalse(rep["weekend"])


class TestSessionBreakdown(unittest.TestCase):
    def test_journal_buckets_by_session(self):
        j = TradeJournal(os.path.join(tempfile.mkdtemp(), "j.db"))
        j.record_trade(_rec(0, _ts(13, 1)))   # overlap
        j.record_trade(_rec(1, _ts(13, 1)))
        j.record_trade(_rec(2, _ts(21, 1)))   # offhours
        report = journal_report(j)
        rows = report["by_session"]
        self.assertEqual(sum(r["trades"] for r in rows), 3)
        by_name = {r["session"]: r for r in rows}
        self.assertEqual(by_name["overlap"]["trades"], 2)
        self.assertEqual(by_name["offhours"]["trades"], 1)
        self.assertEqual(by_name["overlap"]["win_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
