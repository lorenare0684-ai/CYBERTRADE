"""Data layer tests: models, history, synthetic markets, feeds."""

from __future__ import annotations

import time
import unittest

from cybertrade.constants import Side, Timeframe
from cybertrade.data.feed import QuoteBook, ReplayFeed, SyntheticFeed
from cybertrade.data.history import CandleSeries, MultiTimeframeBook, load_history, resample
from cybertrade.data.models import Candle, Fill, Order, Position, Settlement, Tick
from cybertrade.data.synthetic import (
    SCENARIO_NAMES,
    MarketParams,
    MarketSimulator,
    generate_candles,
    make_process,
    scenario_catalog,
)


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
        small = generate_candles("gbm", bars=60, params=MarketParams(timeframe_seconds=60))
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


class TestSynthetic(unittest.TestCase):
    def test_all_scenarios_generate(self):
        for name in SCENARIO_NAMES:
            cs = generate_candles(name, bars=40, seed=3)
            self.assertEqual(len(cs), 40, msg=name)
            for c in cs:
                self.assertGreaterEqual(c.high, c.low)
                self.assertGreaterEqual(c.high, c.open)
                self.assertLessEqual(c.low, c.open)

    def test_deterministic(self):
        a = generate_candles("gbm", bars=50, seed=9)
        b = generate_candles("gbm", bars=50, seed=9)
        self.assertEqual([c.close for c in a], [c.close for c in b])

    def test_different_seeds_differ(self):
        a = generate_candles("gbm", bars=50, seed=1)
        b = generate_candles("gbm", bars=50, seed=2)
        self.assertNotEqual([c.close for c in a], [c.close for c in b])

    def test_bull_trends_up(self):
        cs = generate_candles("bull_trend", bars=300, seed=4)
        self.assertGreater(cs[-1].close, cs[0].close * 1.005)

    def test_bear_trends_down(self):
        cs = generate_candles("bear_trend", bars=300, seed=4)
        self.assertLess(cs[-1].close, cs[0].close * 0.995)

    def test_simulator_tick(self):
        sim = MarketSimulator("range_chop", seed=2)
        px, spread = sim.tick()
        self.assertGreater(px, 0)
        self.assertGreaterEqual(spread, 0)

    def test_catalog(self):
        rows = scenario_catalog()
        self.assertGreaterEqual(len(rows), 10)

    def test_unknown_scenario(self):
        with self.assertRaises(Exception):
            make_process("no_such_scenario")


class TestFeeds(unittest.TestCase):
    def test_replay(self):
        candles = generate_candles("gbm", bars=20)
        feed = ReplayFeed("A", candles)
        seen = []
        feed.add_listener(lambda t: seen.append(t.price))
        ticks = feed.replay_once()
        self.assertEqual(len(ticks), 20)
        self.assertEqual(len(seen), 20)

    def test_synthetic_warmup_and_ticks(self):
        feed = SyntheticFeed(
            assets=["A"], scenarios={"A": "gbm"},
            timeframe_seconds=60, tick_interval=0.01, warmup_bars=50,
        )
        feed.warmup()
        self.assertGreaterEqual(len(feed.book("A").book(60)), 40)
        got = []
        feed.add_listener(lambda t: got.append(t))
        feed.start()
        time.sleep(0.15)
        feed.stop()
        self.assertTrue(got)

    def test_quote_book(self):
        qb = QuoteBook()
        for i in range(10):
            qb.on_tick(Tick("A", 1.0 + i * 0.001, bid=1.0 + i * 0.001 - 0.0002,
                            ask=1.0 + i * 0.001 + 0.0002))
        self.assertGreater(qb.spread("A"), 0)
        self.assertGreaterEqual(qb.liquidity_score("A"), 0.0)


if __name__ == "__main__":
    unittest.main()
