"""Phase-27 tests — the positions bay: open contracts, live marks, one-cut close.

`state()["positions"]` lists every open contract with feed/broker mark,
ITM/OTM/EVEN state, and countdown; `{"cmd":"close"}` runs P19's
`broker.close_position` for ONE contract and immediately pumps the pending
settlement so ledger/journal/HUD see the cut (the lifeboat's downstream
path — no special cases).
"""

from __future__ import annotations

import os
import tempfile
import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.constants import Side
from cybertrade.data.models import Tick
from cybertrade.events import Topic, default_bus
from cybertrade.web.server import EngineHub, WebTerminal
from tests.venue_stubs import VenueFeed, VenueStub


def _engine() -> TradingEngine:
    cfg = AppConfig()
    tmp = tempfile.mkdtemp()
    cfg.journal_path = os.path.join(tmp, "j.db")
    cfg.calibration_path = os.path.join(tmp, "c.json")
    return TradingEngine(
        cfg,
        feed=VenueFeed(assets=["EURUSD_otc", "GBPUSD_otc"]),
        broker=VenueStub(balance=1000.0),
    )


class TestPositionsBlock(unittest.TestCase):
    def setUp(self):
        self.eng = _engine()
        self.eng.boot()
        self.term = WebTerminal(EngineHub(self.eng), host="127.0.0.1", port=0)

    def tearDown(self):
        self.eng.shutdown()

    def _place(self, price: float = 1.10, expiry: int = 60,
               asset_idx: int = 0) -> str:
        asset = self.eng.feed.assets[asset_idx]
        self.eng.broker.on_tick(Tick(asset=asset, price=price, ts=1.0))
        order = self.eng.oms.submit(asset, Side.CALL, 5.0, expiry,
                                    payout=0.85, strategy="test_strat")
        self.assertIsNotNone(order)
        return order.id

    def test_empty_initially(self):
        self.assertEqual(self.term.hub.state()["positions"], [])

    def test_placed_position_marks_itm_otm_even(self):
        self._place()
        asset = self.eng.feed.assets[0]
        rows = self.term.hub.state()["positions"]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        strike = r["strike"]
        for mark, expected in ((strike + 0.01, "itm"),
                               (strike - 0.01, "otm")):
            self.eng.feed._last[asset] = mark
            row = self.term.hub.state()["positions"][0]
            self.assertEqual(row["state"], expected)
            self.assertEqual(row["mark"], round(mark, 5))
        # EVEN needs the exact unrounded strike
        pos = self.eng.broker.open_positions()[0]
        self.eng.feed._last[asset] = pos.strike
        row = self.term.hub.state()["positions"][0]
        self.assertEqual(row["state"], "even")
        row = self.term.hub.state()["positions"][0]
        self.assertEqual(row["side"], "call")
        self.assertEqual(row["strategy"], "test_strat")
        self.assertGreater(row["seconds_left"], 0)
        self.assertGreater(row["expiry_ts"], row["seconds_left"])

    def test_sorted_by_expiry(self):
        self._place(expiry=60, asset_idx=0)
        self._place(expiry=120, asset_idx=1)   # max_per_asset = 1
        rows = self.term.hub.state()["positions"]
        self.assertEqual(len(rows), 2)
        self.assertLessEqual(rows[0]["expiry_ts"], rows[1]["expiry_ts"])


class TestCloseCommand(unittest.TestCase):
    def setUp(self):
        self.eng = _engine()
        self.eng.boot()
        self.term = WebTerminal(EngineHub(self.eng), host="127.0.0.1", port=0)

    def tearDown(self):
        self.eng.shutdown()

    def test_close_settles_into_journal(self):
        asset = self.eng.feed.assets[0]
        self.eng.broker.on_tick(Tick(asset=asset, price=1.10, ts=1.0))
        order = self.eng.oms.submit(asset, Side.CALL, 5.0, 60,
                                    payout=0.85, strategy="test_strat")
        self.assertIsNotNone(order)
        rows = self.term.hub.state()["positions"]
        self.assertEqual(len(rows), 1)
        pos_id = rows[0]["id"]

        kinds = []
        with default_bus.subscribe(
            Topic.ALERT, lambda e: kinds.append(e.payload.get("kind"))
        ):
            res = self.term.command({"cmd": "close", "position": pos_id})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.term.hub.state()["positions"], [])
        # pending settlement flushed: record landed for HUD/blotter
        trades = self.term.hub.state()["trades"]
        self.assertEqual(len(trades), 1)
        self.assertIn("manual_close", kinds)

    def test_errors(self):
        self.assertFalse(self.term.command({"cmd": "close"})["ok"])
        self.assertFalse(
            self.term.command({"cmd": "close", "position": "pos-nope"})["ok"]
        )


if __name__ == "__main__":
    unittest.main()
