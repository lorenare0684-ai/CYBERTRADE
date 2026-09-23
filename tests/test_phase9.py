"""Phase-9 tests — the dry-run harness: live quotes in, paper fills out,
zero orders ever reach the venue.

Pinned here with stubs (this sandbox has no Quotex session): the adapter
rail, the hybrid quote path, the tick-wiring seam, and an end-to-end fill
priced at the venue quote.
"""

from __future__ import annotations

import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.brokers.quotex.adapter import QuotexBroker
from cybertrade.cli import _build_engine
from cybertrade.config import AppConfig
from cybertrade.constants import MarketRegime, Side
from cybertrade.data.models import Order, Signal, Tick
from cybertrade.exceptions import OrderRejected
from cybertrade.execution.dryrun import DryRunBroker
from cybertrade.regime.detector import RegimeReading
from cybertrade.utils import timex


class StubApi:
    """The venue wire: records everything, forbids buys."""

    connected = False

    def __init__(self):
        self.buys = []
        self.tick_handlers = []
        self.listeners = []

    def buy(self, *a, **kw):
        self.buys.append((a, kw))
        raise AssertionError("venue.buy must never be called in dry-run")

    def add_tick_handler(self, fn):
        self.tick_handlers.append(fn)

    def add_listener(self, fn):
        self.listeners.append(fn)

    def request_instruments(self):
        pass

    def payout_for(self, asset, expiry):
        return 0.91


class StubVenue:
    def __init__(self, api=None, quote=0.91, explode=False):
        self.api = api
        self.quote = quote
        self.explode = explode

    def payout_for(self, asset, expiry_seconds):
        if self.explode:
            raise RuntimeError("quote feed down")
        return self.quote


def _order() -> Order:
    return Order(
        asset="EURUSD_otc", side=Side.CALL, amount=5.0, expiry_seconds=60,
        payout=0.9, strategy="t", tag="", meta={},
    )


class TestAdapterRail(unittest.TestCase):
    def test_rail_blocks_submit_before_anything(self):
        api = StubApi()
        broker = QuotexBroker(api, allow_orders=False)
        with self.assertRaises(OrderRejected) as cm:
            broker.submit(_order())
        self.assertIn("dry-run", str(cm.exception))
        self.assertEqual(api.buys, [])

    def test_default_still_allows_attempt(self):
        broker = QuotexBroker(StubApi())
        self.assertTrue(broker.allow_orders)
        with self.assertRaises(OrderRejected) as cm:
            broker.submit(_order())
        self.assertNotIn("dry-run", str(cm.exception))  # fails later: not connected


class TestDryRunBroker(unittest.TestCase):
    def test_payout_quote_from_venue(self):
        b = DryRunBroker(StubVenue(quote=0.91))
        self.assertAlmostEqual(b.payout_for("EURUSD_otc", 60), 0.91)
        # clamped into sane bounds
        self.assertAlmostEqual(DryRunBroker(StubVenue(quote=0.40)).payout_for("X", 60), 0.5)

    def test_quote_failure_falls_back_to_paper(self):
        b = DryRunBroker(StubVenue(explode=True), default_payout=0.85)
        q = b.payout_for("EURUSD_otc", 60)
        self.assertGreaterEqual(q, 0.5)
        self.assertLessEqual(q, 0.95)

    def test_exposes_api_for_tick_wiring(self):
        api = StubApi()
        b = DryRunBroker(StubVenue(api=api))
        self.assertIs(b.api, api)

    def test_name_is_dry(self):
        self.assertEqual(DryRunBroker(StubVenue()).name, "PAPER-DRY")

    def test_end_to_end_fill_at_venue_quote(self):
        cfg = AppConfig()
        cfg.risk.win_rate_floor = 0.0
        venue = StubVenue(api=StubApi(), quote=0.95)
        eng = TradingEngine(cfg, broker=DryRunBroker(venue))
        eng.broker.connect()
        eng._on_tick(Tick(asset=eng.feed.assets[0], price=1.0))
        sig = Signal(
            asset=eng.feed.assets[0], side=Side.CALL, confidence=0.6,
            strategy="mix", expiry_seconds=60, price=1.0, ts=timex.now(),
        )
        reading = RegimeReading(regime=MarketRegime.RANGE, confidence=0.5,
                                stress=0.1, trend_strength=0.1, range_score=0.6)
        self.assertTrue(eng._try_execute(sig, reading))
        order = list(eng.oms.orders.values())[-1]
        self.assertEqual(order.payout, 0.95)      # priced at the venue quote
        self.assertEqual(venue.api.buys, [])      # venue untouched


class TestDryRunWiring(unittest.TestCase):
    def test_build_engine_dryrun_mode(self):
        # Phase-29: live modes never degrade silently — no session raises.
        from cybertrade.exceptions import ConfigError

        cfg = AppConfig()
        cfg.broker.mode = "dryrun"
        with self.assertRaises(ConfigError) as ctx:
            _build_engine(cfg)
        self.assertIn("quotex login", str(ctx.exception))

        # With a connected API (as `quotex login` provides), dryrun wires
        # venue quotes behind paper fills: PAPER-DRY.
        from unittest import mock
        from cybertrade.data.models import Candle

        class Api:
            connected = True
            def connect(self, authorize=True):
                return True
            def get_candles(self, asset, tf=60, count=200, wait=3.0):
                return [Candle(asset=asset, timeframe_seconds=60,
                               open_ts=1_000_000.0 + i, open=1.0, high=1.1,
                               low=0.9, close=1.05) for i in range(8)]
            def last_price(self, asset):
                return 1.05
            def subscribe(self, asset, timeframe_seconds=60):
                pass
            def add_tick_handler(self, fn):
                pass
            def add_listener(self, fn):
                pass
            def remove_listener(self, fn):
                pass
            def payout_for(self, asset, expiry_seconds=60):
                return 0.85
            def sell_option(self, option_id):
                return True

        with mock.patch("cybertrade.cli._live_api",
                        lambda c, api_factory=None: Api()):
            eng = _build_engine(cfg)
        try:
            self.assertEqual(eng.broker.name, "PAPER-DRY")
            self.assertIsNotNone(eng.broker.api)  # live quotes, not catalog
        finally:
            eng.shutdown()


if __name__ == "__main__":
    unittest.main()
