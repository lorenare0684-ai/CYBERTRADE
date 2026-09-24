"""Phase-8 tests — the runtime payout gate: the venue quotes the hurdle.

The edge gate computed EV against the CONFIG default payout (0.85) while
the venue pays 0.78–0.92 depending on asset and expiry.  These tests pin
the gate to the real quote — the hurdle becomes per-trade.
"""

from __future__ import annotations

import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.constants import MarketRegime, Side
from cybertrade.data.models import Signal, Tick
from cybertrade.regime.detector import RegimeReading
from cybertrade.utils import timex
from tests.venue_stubs import VenueFeed, VenueStub


def _engine(quote: float, adaptive: bool = False) -> TradingEngine:
    cfg = AppConfig()
    cfg.risk.win_rate_floor = 0.0
    if adaptive:
        cfg.risk.expiry_select = "adaptive"
    eng = TradingEngine(cfg, feed=VenueFeed(), broker=VenueStub())
    eng.broker.connect()
    eng._on_tick(Tick(asset=eng.feed.assets[0], price=1.0))
    eng.broker.payout_for = lambda asset, expiry: quote  # the venue quote
    return eng


def _signal(eng: TradingEngine) -> Signal:
    return Signal(
        asset=eng.feed.assets[0], side=Side.CALL, confidence=0.6,
        strategy="mix", expiry_seconds=60, price=1.0, ts=timex.now(),
    )


def _reading() -> RegimeReading:
    return RegimeReading(regime=MarketRegime.RANGE, confidence=0.5, stress=0.1,
                         trend_strength=0.1, range_score=0.6)


class TestRuntimePayoutGate(unittest.TestCase):
    def test_thin_quote_vetoes(self):
        # cold p_win(0.6) = 0.55 → edge @ 0.80 payout = -0.01: the quote kills it
        eng = _engine(0.80)
        self.assertFalse(eng._try_execute(_signal(eng), _reading()))
        self.assertGreaterEqual(eng.edge_rejects, 1)

    def test_rich_quote_passes_and_fills_at_quote(self):
        # identical signal and p_win → edge @ 0.95 = +0.0725: book it at 0.95
        eng = _engine(0.95, adaptive=True)
        self.assertTrue(eng._try_execute(_signal(eng), _reading()))
        orders = list(eng.oms.orders.values())
        self.assertEqual(orders[-1].payout, 0.95)

    def test_min_payout_floor_vetoes(self):
        # edge gate rejects the 0.50 quote first…
        eng = _engine(0.50)
        self.assertFalse(eng._try_execute(_signal(eng), _reading()))
        # …and the risk floor holds even when the gate is bypassed
        eng2 = _engine(0.50)
        order = eng2.oms.submit(
            asset=eng2.feed.assets[0], side=Side.CALL, stake=5.0,
            expiry_seconds=60, payout=0.50, confidence=0.7, strategy="mix",
        )
        self.assertIsNone(order)


class TestVenueQuoteShape(unittest.TestCase):
    def test_venue_payout_bounds(self):
        eng = TradingEngine(AppConfig(), feed=VenueFeed(), broker=VenueStub())
        q = eng.broker.payout_for("EURUSD_otc", 60)
        self.assertGreaterEqual(q, 0.5)
        self.assertLessEqual(q, 0.95)

    def test_venue_quote_is_the_gate_input(self):
        # the gate must read the venue's own quote, not a config default
        eng = TradingEngine(AppConfig(), feed=VenueFeed(),
                            broker=VenueStub(payout=0.62))
        self.assertEqual(eng.broker.payout_for("EURUSD_otc", 60), 0.62)


if __name__ == "__main__":
    unittest.main()
