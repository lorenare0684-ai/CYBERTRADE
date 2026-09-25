"""Data layer tests: models, history, venue/replay feeds (no synthetic market)."""

from __future__ import annotations

import unittest

from cybertrade.constants import Side, Timeframe
from cybertrade.data.feed import Feed, QuoteBook, ReplayFeed
from cybertrade.data.history import CandleSeries, MultiTimeframeBook, load_history, resample
from cybertrade.data.models import Candle, Fill, Order, Position, Settlement, Tick

from tests.venue_stubs import VenueFeed, VenueStub, venue_candles

class TestCandle(unittest.TestCase):
    def test_update_and_props(self):
        c = Candle("A", 60, 0.0, 1.0, 1.2, 0.9, 1.1)
        c.update(1.3, 1.0)
        self.assertEqual(c.high, 1.3)
        self.assertEqual(c.close, 1.3)
        self.assertEqual(c.direction, 1)
        self.assertGreaterEqual(c.range, 0)

    def test_dict_roundtrip(self):
        c = Candle("A", 60, 100.0, 1.0, 2.0, 0.5, 1.5, volume=3.0, closed=True)
        c2 = Candle.from_dict(c.to_dict())
        self.assertEqual(c2.close, 1.5)
        self.assertTrue(c2.closed)


class TestSettlement(unittest.TestCase):
    def test_call_wins(self):
        fill = Fill("o1", "EURUSD", Side.CALL, 1.1, 10.0, 0.85)
        pos = Position(fill, expiry_ts=10.0)
        s = pos.settle(1.2)
        self.assertTrue(s.won)
        self.assertAlmostEqual(s.pnl, 8.5)
        self.assertAlmostEqual(s.returned, 18.5)

    def test_put_wins_and_atm_refund(self):
        fill = Fill("o2", "EURUSD", Side.PUT, 1.1, 10.0, 0.85)
        pos = Position(fill, expiry_ts=10.0)
        self.assertTrue(pos.settle(1.0).won)
        s2 = pos.settle(1.1)
        self.assertTrue(s2.refunded)
        self.assertEqual(s2.pnl, 0.0)
        self.assertEqual(s2.returned, 10.0)


class TestCandleSeries(unittest.TestCase):
    def test_bucket_rollover(self):
        cs = CandleSeries("A", 60, maxlen=10)
        cs.update(1.0, ts=100.0)
        cs.update(1.1, ts=110.0)
        cs.update(1.2, ts=170.0)   # new bucket
        candles = cs.candles()
        self.assertEqual(len(candles), 2)
        self.assertTrue(candles[0].closed)
        self.assertFalse(candles[1].closed)

    def test_resample(self):
        small = venue_candles(bars=60)
        big = resample(small, 300)
        self.assertTrue(len(big) <= 13)
        self.assertEqual(big[0].timeframe_seconds, 300)

    def test_mtf_book(self):
        book = MultiTimeframeBook("A", (Timeframe.M1, Timeframe.M5))
        for i in range(10):
            book.on_price(1.0 + i * 0.01, ts=1000.0 + i * 30)
        self.assertGreaterEqual(len(book.book(60)), 1)

    def test_load_history(self):
        candles = load_history("A", [1.0, 1.1, 1.2], 60, end_ts=1_700_000_000)
        self.assertEqual(len(candles), 3)
        self.assertEqual(candles[-1].close, 1.2)


class TestFeeds(unittest.TestCase):
    def test_replay(self):
        candles = venue_candles(bars=20)
        feed = ReplayFeed("A", candles)
        seen = []
        feed.add_listener(lambda t: seen.append(t.price))
        ticks = feed.replay_once()
        self.assertEqual(len(ticks), 20)
        self.assertEqual(len(seen), 20)

    def test_replay_feed_is_refused_in_live_modes(self):
        """The airlock still holds: a recorded tape is not venue data."""
        feed = ReplayFeed("A", venue_candles(bars=20))
        self.assertTrue(feed.is_synthetic)
        from cybertrade.config import AppConfig
        from cybertrade.exceptions import ConfigError

        with self.assertRaises(ConfigError):
            from cybertrade.bot.engine import TradingEngine

            TradingEngine(AppConfig(), feed=feed, broker=VenueStub())

    def test_quote_book(self):
        qb = QuoteBook()
        for i in range(10):
            qb.on_tick(Tick("A", 1.0 + i * 0.001, bid=1.0 + i * 0.001 - 0.0002,
                            ask=1.0 + i * 0.001 + 0.0002))
        self.assertGreater(qb.spread("A"), 0)
        self.assertGreaterEqual(qb.liquidity_score("A"), 0.0)


if __name__ == "__main__":
    unittest.main()
