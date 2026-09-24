"""Phase-26 tests — the strategy deck: live arsenal visibility + control.

`ensemble.describe()` already streamed 40 members into `state()` and
`/api/strategies`, but nothing rendered it and nothing could stop a
strategy from the console. The deck renders ward badges (decay ⛓, win-rate
⌂) with live WR/attempts/weight and offers per-member ON/OFF toggles via
`{"cmd":"strategy"}`.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.data.models import Settlement, Side, TradeRecord
from cybertrade.web.server import EngineHub, WebTerminal

from cybertrade.strategies.registry import build_all_weather
from tests.venue_stubs import venue_candles
from tests.venue_stubs import VenueFeed, VenueStub


def _rec(i: int, won: bool, strategy: str) -> TradeRecord:
    return TradeRecord(
        settlement=Settlement(
            fill_id=f"f{i}", order_id=f"o{i}", asset="EURUSD_otc",
            side=Side.CALL, strike=1.0, expiry_price=1.01 if won else 0.99,
            stake=10.0, payout=0.85, won=won, ts=1000.0 + i, id=f"set{i}",
        ),
        strategy=strategy, regime="range",
    )


class TestDescribeWard(unittest.TestCase):
    def test_members_carry_ward_flags(self):
        ens = build_all_weather()
        d = ens.describe()
        self.assertIn("decay_ward", d)
        self.assertEqual(d["decay_ward"], [])
        self.assertGreaterEqual(len(d["members"]), 30)
        m = d["members"][0]
        self.assertIn("winrate_quarantined", m)
        self.assertIn("decay_quarantined", m)
        self.assertFalse(m["decay_quarantined"])

    def test_decay_ward_flags_the_right_member(self):
        ens = build_all_weather()
        name = ens.members[0].name
        ens.quarantined_votes = {name}
        d = ens.describe()
        self.assertEqual(d["decay_ward"], [name])
        row = next(x for x in d["members"] if x["name"] == name)
        self.assertTrue(row["decay_quarantined"])


class TestStrategyCommand(unittest.TestCase):
    def setUp(self):
        cfg = AppConfig()
        tmp = tempfile.mkdtemp()
        cfg.journal_path = os.path.join(tmp, "j.db")
        cfg.calibration_path = os.path.join(tmp, "c.json")
        self.eng = TradingEngine(
            cfg, feed=VenueFeed(),
            broker=VenueStub(balance=1000.0),
        )
        self.term = WebTerminal(EngineHub(self.eng), host="127.0.0.1", port=0)

    def test_disable_and_reenable(self):
        name = self.eng.ensemble.members[0].name
        r = self.term.command({"cmd": "strategy", "name": name, "enabled": False})
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["strategy"]["enabled"])
        self.assertFalse(self.eng.ensemble.members[0].enabled)
        self.assertTrue(any("DISABLED" in m for m in self.eng.health.messages))
        # state reflects it for the deck renderer
        self.assertFalse(
            self.term.hub.state()["strategies"]["members"][0]["enabled"]
        )
        r2 = self.term.command({"cmd": "strategy", "name": name, "enabled": True})
        self.assertTrue(r2["ok"])
        self.assertTrue(self.eng.ensemble.members[0].enabled)

    def test_unknown_strategy_rejected(self):
        r = self.term.command({"cmd": "strategy", "name": "nope_missing",
                               "enabled": False})
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_state_exposes_deck_fields(self):
        strats = self.term.hub.state()["strategies"]
        self.assertIn("members", strats)
        self.assertIn("decay_ward", strats)
        self.assertIn("weights", strats)
        self.assertGreaterEqual(len(strats["members"]), 30)

    def test_disabled_members_do_not_vote(self):
        ens = build_all_weather()
        for m in ens.members:
            m.enabled = False
        from cybertrade.regime.detector import RegimeReading
        from cybertrade.strategies.base import StrategyContext
        cs = venue_candles(n=250, shape="trend_up")
        ctx = StrategyContext(
            asset="EURUSD_otc", candles=cs, regime=RegimeReading(),
            timeframe_seconds=60, expiry_seconds=60, payout=0.85, ts=1.0,
        )
        self.assertIsNone(ens.generate(ctx))


if __name__ == "__main__":
    unittest.main()
