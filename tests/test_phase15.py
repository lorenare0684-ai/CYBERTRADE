"""Phase-15 tests — the live wire: mode=quotex with the full safety stack.

`BrokerConfig.mode` declared `paper | quotex | dryrun` since Phase 1, dryrun
became real in Phase 9 — and `quotex` was still just a comment. The adapter
was already a complete Broker (submit -> api.buy, local settle_due, venue
reconciliation); this wires the build path and the interlocks: allow_orders
rides on cfg.risk.allow_live, which the I-UNDERSTAND gate sets BEFORE the
engine is built. Without --live, mode=quotex degrades to dry-run and says so.
Phase-29: live modes now require LIVE venue candles (`_live_api` +
LiveQuotexFeed) — synthetic feeds are refused outright.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock as mock
from types import SimpleNamespace

from cybertrade.brokers.quotex.adapter import QuotexBroker
from cybertrade.config import AppConfig
from cybertrade.data.models import Candle, Order
from cybertrade.constants import Side
from cybertrade.exceptions import OrderRejected


def _candles(asset: str, n: int = 8):
    """Canned venue history for LiveQuotexFeed warmup (P29)."""
    return [
        Candle(asset=asset, timeframe_seconds=60, open_ts=1_000_000.0 + i,
               open=1.0, high=1.1, low=0.9, close=1.05)
        for i in range(n)
    ]


class FakeApi:
    def __init__(self):
        self.bought = []
        self.ssid_set = None
        self.connected = True
        self.demo = True
        self.session = SimpleNamespace(host="qxbroker.com", demo=True, ssid="QX.s")
        self.assets = {"EURUSD_otc": object()}

    def set_ssid(self, ssid, cookies=""):
        self.ssid_set = ssid

    def login(self, email, password, is_demo=None):
        pass

    def connect(self, authorize=True):
        return True

    def close(self):
        pass

    def add_listener(self, fn):
        pass

    def add_tick_handler(self, fn):
        pass

    def request_instruments(self):
        pass

    def get_candles(self, asset, tf=60, count=200, wait=3.0):
        return _candles(asset, 8)

    def payout_for(self, asset, expiry_seconds=60):
        return 0.9

    def last_price(self, asset):
        return 1.0

    def buy(self, asset, amount, action, duration, **kw):
        self.bought.append((asset, amount, action, duration))
        return SimpleNamespace(request_id=f"req{len(self.bought)}")

    def account_snapshot(self):
        return SimpleNamespace(balance=1000.0, open_positions=0)


class TestLiveSubmit(unittest.TestCase):
    def test_orders_reach_venue_when_allowed(self):
        fake = FakeApi()
        b = QuotexBroker(fake, allow_orders=True)
        order = Order(asset="EURUSD_otc", side=Side.CALL, amount=10.0,
                      expiry_seconds=60, payout=0.9, strategy="s", tag="")
        fill = b.submit(order)
        self.assertEqual(fake.bought, [("EURUSD_otc", 10.0, "call", 60)])
        self.assertEqual(fill.broker_id, "req1")

    def test_rail_still_blocks_when_disallowed(self):
        b = QuotexBroker(FakeApi(), allow_orders=False)
        order = Order(asset="EURUSD_otc", side=Side.CALL, amount=10.0,
                      expiry_seconds=60, payout=0.9, strategy="s", tag="")
        with self.assertRaises(OrderRejected):
            b.submit(order)


class TestBuildWiring(unittest.TestCase):
    def test_build_venue_forwards_allow_orders(self):
        from cybertrade.cli import _build_venue

        fake = FakeApi()
        v = _build_venue(AppConfig(), api_factory=lambda: fake, allow_orders=True)
        self.assertTrue(v.allow_orders)
        v2 = _build_venue(AppConfig(), api_factory=lambda: FakeApi())
        self.assertFalse(v2.allow_orders)

    def _build(self, allow_live: bool):
        from cybertrade.cli import _build_engine

        cfg = AppConfig()
        cfg.broker.mode = "quotex"
        cfg.risk.allow_live = allow_live
        with tempfile.TemporaryDirectory() as tmp:
            cfg.journal_path = os.path.join(tmp, "journal.db")
            cfg.calibration_path = os.path.join(tmp, "cal.json")
            cfg.qx_session_path = os.path.join(tmp, "qx.json")
            fake = FakeApi()
            with mock.patch("cybertrade.cli._live_api",
                            lambda c, api_factory=None: fake):
                return _build_engine(cfg)

    def test_quotex_without_live_degrades_to_dryrun(self):
        eng = self._build(allow_live=False)
        self.assertEqual(eng.broker.name, "PAPER-DRY")
        self.assertFalse(eng.broker.venue.allow_orders)
        eng.shutdown()

    def test_quotex_with_live_is_the_live_wire(self):
        eng = self._build(allow_live=True)
        self.assertEqual(eng.broker.name, "QUOTEX-DEMO")
        self.assertTrue(eng.broker.allow_orders)
        eng.shutdown()


class TestConfirmLiveGate(unittest.TestCase):
    def _args(self, live, yes):
        return SimpleNamespace(live=live, yes=yes)

    def test_without_live_flag_nothing_touches_allow(self):
        from cybertrade.cli import _confirm_live

        cfg = AppConfig()
        self.assertTrue(_confirm_live(self._args(live=False, yes=False), cfg))
        self.assertFalse(cfg.risk.allow_live)

    def test_wrong_typing_aborts(self):
        from cybertrade.cli import _confirm_live

        cfg = AppConfig()
        with mock.patch("builtins.input", lambda *_: "nope"):
            self.assertFalse(_confirm_live(self._args(live=True, yes=False), cfg))
        self.assertFalse(cfg.risk.allow_live)

    def test_i_understand_unlocks(self):
        from cybertrade.cli import _confirm_live

        cfg = AppConfig()
        with mock.patch("builtins.input", lambda *_: "I UNDERSTAND"):
            self.assertTrue(_confirm_live(self._args(live=True, yes=False), cfg))
        self.assertTrue(cfg.risk.allow_live)

    def test_yes_flag_skips_prompt_but_still_gates(self):
        from cybertrade.cli import _confirm_live

        cfg = AppConfig()
        self.assertTrue(_confirm_live(self._args(live=True, yes=True), cfg))
        self.assertTrue(cfg.risk.allow_live)


if __name__ == "__main__":
    unittest.main()
