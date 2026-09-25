"""Phase-15 tests — the live wire: mode=quotex with the full safety stack.

The adapter is a complete Broker (submit -> api.buy, local settle_due, venue
reconciliation).  The build path wires it with `cli._build_engine`, which
demands LIVE venue candles (`_live_api` + LiveQuotexFeed) and an explicitly
chosen purse — nothing degrades to a simulator any more.  `_confirm_live` is
the one human check that survives: type I UNDERSTAND, or --yes for scripts.
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
from cybertrade.cli import main

from tests.venue_stubs import VenueFeed, VenueStub

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
    def _build(self):
        from cybertrade.cli import _build_engine

        cfg = AppConfig()
        cfg.broker.mode = "quotex"
        cfg.broker.demo_account = True          # the purse is chosen, never defaulted
        with tempfile.TemporaryDirectory() as tmp:
            cfg.journal_path = os.path.join(tmp, "journal.db")
            cfg.calibration_path = os.path.join(tmp, "cal.json")
            cfg.qx_session_path = os.path.join(tmp, "qx.json")
            fake = FakeApi()
            with mock.patch("cybertrade.cli._live_api",
                            lambda c, api_factory=None: fake):
                return _build_engine(cfg)

    def test_live_wire_arms_the_venue(self):
        eng = self._build()
        try:
            self.assertTrue(eng.broker.name.startswith("QUOTEX-"))
            self.assertTrue(eng.broker.allow_orders)
            # the venue sees the purse the operator chose
            self.assertTrue(eng.config.broker.purse_chosen)
        finally:
            eng.shutdown()

    def test_unchosen_purse_blocks_the_build(self):
        from cybertrade.cli import _build_engine
        from cybertrade.exceptions import ConfigError

        cfg = AppConfig()
        cfg.broker.mode = "quotex"
        with tempfile.TemporaryDirectory() as tmp:
            cfg.journal_path = os.path.join(tmp, "journal.db")
            cfg.calibration_path = os.path.join(tmp, "cal.json")
            cfg.qx_session_path = os.path.join(tmp, "qx.json")
            with mock.patch("cybertrade.cli._live_api",
                            lambda c, api_factory=None: FakeApi()):
                with self.assertRaises(ConfigError):
                    _build_engine(cfg)


class TestConfirmLiveGate(unittest.TestCase):
    def _args(self, yes, **kw):
        return SimpleNamespace(yes=yes, **kw)

    def test_wrong_typing_aborts_before_the_venue_is_touched(self):
        from cybertrade.cli import _confirm_live

        cfg = AppConfig()
        with mock.patch("builtins.input", lambda *_: "nope"):
            self.assertFalse(_confirm_live(self._args(yes=False), cfg))

        # and `run` never reaches the venue wire when the gate refuses
        with mock.patch("cybertrade.cli._confirm_live", lambda a, c: False), \
                mock.patch("cybertrade.cli._build_engine",
                           side_effect=AssertionError("must not build")):
            self.assertEqual(main(["run", "--demo"]), 1)

    def test_i_understand_unlocks(self):
        from cybertrade.cli import _confirm_live

        cfg = AppConfig()
        with mock.patch("builtins.input", lambda *_: "I UNDERSTAND"):
            self.assertTrue(_confirm_live(self._args(yes=False), cfg))
        self.assertTrue(cfg.risk.allow_live)

    def test_yes_flag_skips_prompt_but_still_gates(self):
        from cybertrade.cli import _confirm_live

        cfg = AppConfig()
        self.assertTrue(_confirm_live(self._args(yes=True), cfg))
        self.assertTrue(cfg.risk.allow_live)

    def test_purse_is_declared_before_the_gate(self):
        from cybertrade.cli import _confirm_live, _resolve_purse

        cfg = AppConfig()
        _resolve_purse(cfg, self._args(yes=False, demo=True))
        self.assertTrue(cfg.broker.purse_chosen)
        with mock.patch("builtins.input", lambda *_: "I UNDERSTAND"):
            self.assertTrue(_confirm_live(self._args(yes=False), cfg))

    def test_purse_flag_reaches_config(self):
        from cybertrade.cli import _resolve_purse

        cfg = AppConfig()
        _resolve_purse(cfg, self._args(yes=False, real=True))
        self.assertTrue(cfg.broker.purse_chosen)
        self.assertIs(cfg.broker.demo_account, False)

    def test_both_purses_at_once_is_an_error(self):
        from cybertrade.cli import _resolve_purse
        from cybertrade.exceptions import ConfigError

        with self.assertRaises(ConfigError):
            _resolve_purse(AppConfig(), self._args(yes=False, demo=True, real=True))


if __name__ == "__main__":
    unittest.main()
