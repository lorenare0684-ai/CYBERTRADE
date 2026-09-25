"""Phase-30 tests — the ghost wire: organic pacing + advanced venue continuity.

"Undetectable" = network manners indistinguishable from the paired Chrome
(human jitter, truthful headers, jittered reconnects, session-fault
classification) — **never** CAPTCHA bypass or fingerprint fraud (none of
which exists). Plus the advanced-integration half: subscription replay on
either reconnect path, chart-like gap-only backfill, portfolio folding,
and boot-time adoption of venue-open contracts.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from types import SimpleNamespace

from cybertrade.brokers.quotex.adapter import QuotexBroker, _venue_expiry
from cybertrade.brokers.quotex.api import QuotexAPI
from cybertrade.brokers.quotex.ghost import (
    Pacekeeper,
    is_session_fault,
    parity_headers,
    reconnect_delay,
)
from cybertrade.brokers.quotex.models import QXOrderResult
from cybertrade.brokers.quotex.protocol import parse_portfolio
from cybertrade.brokers.quotex.sync import backfill_gaps
from cybertrade.config import AppConfig
from cybertrade.data.history import CandleSeries
from cybertrade.data.livefeed import LiveQuotexFeed
from cybertrade.data.models import Candle
from cybertrade.exceptions import ConfigError
from cybertrade.utils import timex
from tests.venue_stubs import VenueFeed, VenueStub


def _candles(asset: str, tf: int, start_ts: float, n: int):
    return [
        Candle(asset=asset, timeframe_seconds=tf, open_ts=start_ts + i * tf,
               open=1.0, high=1.1, low=0.9, close=1.05)
        for i in range(n)
    ]


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start
        self.sleeps = []

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _pace(max_orders_per_min=3, order_min_gap_ms=300, think_ms=100, seed=7):
    fc = FakeClock()
    p = Pacekeeper(
        order_think_ms=think_ms,
        order_min_gap_ms=order_min_gap_ms,
        max_orders_per_min=max_orders_per_min,
        clock=fc.clock, sleep=fc.sleep, rnd=random.Random(seed),
    )
    return p, fc


class TestPacekeeper(unittest.TestCase):
    def test_disabled_never_sleeps(self):
        def boom(_):
            raise AssertionError("disabled pace must not sleep")
        p = Pacekeeper(enabled=False, sleep=boom)
        self.assertEqual(p.wait("order"), 0.0)
        self.assertEqual(p.waits, 0)

    def test_first_order_thinks_second_respects_gap(self):
        p, fc = _pace()
        first = p.wait("order")
        self.assertGreaterEqual(first, 0.07)      # jittered think-time only
        second = p.wait("order")
        self.assertGreaterEqual(second, 0.30)     # hard min gap + think
        self.assertGreaterEqual(fc.t, 0.37)

    def test_sliding_window_blocks_burst(self):
        p, _ = _pace(max_orders_per_min=2)
        p.wait("order")
        p.wait("order")
        blocked = p.wait("order")                 # window full → wait ~60s
        self.assertGreaterEqual(blocked, 59.0)
        stats = p.stats()
        self.assertEqual(stats["orders"], 3)
        self.assertTrue(stats["enabled"])

    def test_class_gaps_and_jitter_not_metronomic(self):
        p1, _ = _pace(seed=7)
        a = p1.wait("order")
        p2, _ = _pace(seed=11)
        b = p2.wait("order")
        self.assertNotAlmostEqual(a, b, places=6)  # jitter varies think-time
        p, _ = _pace()
        self.assertEqual(p.wait("frame"), 0.0)     # first frame: no gap yet
        gap = p.wait("frame")
        self.assertGreaterEqual(gap, 0.04)

    def test_history_gap(self):
        p, _ = _pace()
        p.wait("history")
        self.assertGreaterEqual(p.wait("history"), 0.12)


class TestGhostHelpers(unittest.TestCase):
    def test_reconnect_delay_jittered_growing_capped(self):
        d1 = reconnect_delay(1, random.Random(1))
        self.assertGreaterEqual(d1, 2.0 * 0.8)
        self.assertLessEqual(d1, 2.0 * 1.25)
        d5 = reconnect_delay(5, random.Random(1))
        self.assertGreater(d5, d1)
        for seed in range(8):
            self.assertLessEqual(reconnect_delay(40, random.Random(seed)), 75.0)

    def test_parity_headers_truthful_and_no_fake_extensions(self):
        h = parity_headers("Mozilla/5.0 … Chrome", origin="https://qxbroker.com")
        self.assertIn("User-Agent", h)
        self.assertEqual(h["Accept-Language"], "en-US,en;q=0.9")
        self.assertEqual(h["Cache-Control"], "no-cache")
        self.assertNotIn("Sec-WebSocket-Extensions", h)   # never fake a capability
        self.assertNotIn("Accept-Encoding", h)            # we do not decode br/gzip

    def test_session_fault_classification(self):
        for msg in ("Invalid session", "unauthorized", "Please login",
                    "cloudflare challenge", "CAPTCHA required"):
            self.assertTrue(is_session_fault(msg), msg)
        for msg in ("asset is closed", "insufficient balance", "rate limited"):
            self.assertFalse(is_session_fault(msg), msg)


class TestParsePortfolio(unittest.TestCase):
    def test_shapes_fold_into_order_rows(self):
        rows = parse_portfolio([
            {"orders": [{"id": "1", "asset": "EURUSD_otc", "amount": 5,
                         "action": "call", "status": "open"}]},
            {"data": [{"requestId": "r2", "asset": "XAUUSD_otc", "amount": 2}]},
            {"open": [{"id": "3", "asset": "GBPUSD_otc", "amount": 1}]},
            {"mystery": {"no": "row"}},
        ])
        ids = {r.order_id or r.request_id for r in rows}
        self.assertEqual(ids, {"1", "r2", "3"})
        self.assertTrue(all(r.status for r in rows))


class FakeSock:
    def __init__(self):
        self.sent = []
        self.connected = True

    def send(self, wire):
        self.sent.append(wire)

    def disconnect(self):
        self.connected = False

    def stats(self):
        return {"connected": self.connected}


class TestApiGhostBehaviour(unittest.TestCase):
    def _api(self):
        return QuotexAPI(pace=Pacekeeper(enabled=False))

    def test_buy_rides_paced_order_class(self):
        fc = FakeClock()
        api = QuotexAPI(pace=Pacekeeper(
            order_think_ms=50, order_min_gap_ms=100,
            clock=fc.clock, sleep=fc.sleep, rnd=random.Random(3),
        ))
        api.socket = FakeSock()
        api.buy("EURUSD_otc", 5, "call", 60)
        self.assertEqual(api.pace.orders, 1)
        self.assertGreaterEqual(fc.t, 0.03)          # think-time happened
        self.assertGreater(api.pace.slept_total, 0.0)
        wires = "".join(api.socket.sent)
        self.assertIn("settings/apply", api.socket.sent[0])   # tab parity
        self.assertIn("orders/open", api.socket.sent[-1])
        self.assertLess(wires.index("settings/apply"), wires.index("orders/open"))

    def test_subscribe_registry_and_reconnect_replay(self):
        api = self._api()
        api.socket = FakeSock()
        api.subscribe("EURUSD_otc", 60)
        api.subscribe("XAUUSD_otc", 60)
        api.subscribe("EURUSD_otc", 60)              # dupe collapses
        self.assertEqual(len(api._subs), 2)
        events = []
        api.add_listener(lambda k, p: events.append(k))
        api.socket.sent.clear()
        api._on_reconnected()
        wires = "".join(api.socket.sent)
        self.assertEqual(wires.count("instruments/update"), 2)
        self.assertEqual(wires.count("depth/follow"), 2)
        self.assertIn("instruments/get", wires)  # bootstrap replays too
        self.assertIn("reconnected", events)
        self.assertEqual(api.stats()["subscriptions"], 2)

    def test_close_clears_socket(self):
        api = self._api()
        api.socket = FakeSock()
        api.close()
        self.assertIsNone(api.socket)

    def test_session_fault_marks_stale_and_emits_once(self):
        api = self._api()
        events = []
        api.add_listener(lambda k, p: events.append((k, p)))
        api._on_socket_event("error", ["Invalid session"])
        self.assertTrue(api._session_stale)
        api._on_socket_event("error", ["Invalid session"])   # once only
        self.assertEqual([k for k, _ in events].count("session_stale"), 1)
        self.assertTrue(api.stats()["session_stale"])
        api._on_socket_event("error", ["asset closed"])       # benign
        self.assertEqual([k for k, _ in events].count("session_stale"), 1)
        api.set_ssid("fresh")                                 # re-pair clears
        self.assertFalse(api._session_stale)


class TestBackfillGaps(unittest.TestCase):
    def test_fetches_only_when_tail_is_stale(self):
        now = timex.bucket_start(timex.now(), 60) + 10.0
        series = CandleSeries("EURUSD_otc", 60, maxlen=500)
        start = timex.bucket_start(now, 60) - 12 * 60
        for c in _candles("EURUSD_otc", 60, start, 5):
            series.push_closed(c)                 # tail = bucket-7*60 → missing>2
        calls = []

        class Api:
            def get_candles(self, asset, tf, count, wait=3.0):
                calls.append((asset, tf, count))
                start = timex.bucket_start(now, tf) - 5 * tf
                return _candles(asset, tf, start, 6)

        added = backfill_gaps(Api(), series, now=now)
        self.assertGreaterEqual(added, 3)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0][2], 500)

        # tail now current → no second fetch
        added2 = backfill_gaps(Api(), series, now=now)
        self.assertEqual(added2, 0)
        self.assertEqual(len(calls), 1)

    def test_empty_series_is_noop(self):
        class Api:
            def get_candles(self, *a, **k):
                raise AssertionError("must not fetch")

        self.assertEqual(backfill_gaps(Api(), CandleSeries("A", 60)), 0)


class TestLiveFeedContinuity(unittest.TestCase):
    def test_backfill_all_and_reconnect_event_sweep(self):
        class Api:
            def __init__(self, now):
                self.now = now
                self.calls = 0

            def get_candles(self, asset, tf, count=200, wait=3.0):
                self.calls += 1
                bucket = timex.bucket_start(self.now, tf)
                # two bars that leave a >2-bar hole behind → next sweep fetches
                return _candles(asset, tf, bucket - 4 * tf, 2)

            def add_tick_handler(self, fn):
                pass

            def add_listener(self, fn):
                pass

            def subscribe(self, asset, timeframe_seconds=60):
                pass

            def last_price(self, asset):
                return None

        now = timex.now()
        api = Api(now)
        feed = LiveQuotexFeed(api, assets=["EURUSD_otc"],
                              timeframe_seconds=60, refresh_seconds=0.0)
        # seed a gapped book: last M1 bucket is ~5 minutes old
        feed.book("EURUSD_otc").on_price(1.0, now - 5 * 60)
        feed._running = True
        added = feed._backfill_all()
        self.assertGreaterEqual(added, 2)
        m1 = feed.book("EURUSD_otc").book(60).candles(limit=10)
        self.assertEqual(m1[-1].open_ts,
                         timex.bucket_start(now, 60) - 3 * 60)  # tail advanced
        # reconnect event triggers the same sweep
        calls_before = api.calls
        feed._on_api_event("reconnected", {})
        self.assertGreater(api.calls, calls_before)


class TestReconcileVenue(unittest.TestCase):
    def _broker(self, trades):
        class Api:
            def __init__(self):
                self.session = SimpleNamespace(ssid="x")
                self.connected = True

            def add_listener(self, fn):
                pass

            def open_trades(self):
                return trades

        return QuotexBroker(Api(), allow_orders=False)

    def test_adopts_known_expiry_and_skips_orphans(self):
        now = timex.now()
        good = QXOrderResult(
            order_id="o1", request_id="r1", status="open",
            asset="EURUSD_otc", amount=5.0, action="call",
            open_price=1.10, payout=0.85,
            raw={"time": int(now + 45)},
        )
        orphan = QXOrderResult(
            order_id="o2", status="open", asset="XAUUSD_otc",
            amount=3.0, action="put", open_price=2000.0,
        )
        broker = self._broker([good, orphan])
        adopted = broker.reconcile_venue()
        self.assertEqual(adopted, 1)
        positions = broker.open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].strategy, "venue")
        self.assertEqual(positions[0].fill.broker_id, "o1")
        self.assertAlmostEqual(positions[0].expiry_ts, now + 45, delta=1)
        # idempotent — second sweep adopts nothing new
        self.assertEqual(broker.reconcile_venue(), 0)

    def test_api_without_portfolio_is_noop(self):
        class Api:
            session = SimpleNamespace(ssid="x")

            def add_listener(self, fn):
                pass

        broker = QuotexBroker(Api(), allow_orders=False)
        self.assertEqual(broker.reconcile_venue(), 0)

    def test_venue_expiry_ms_and_junk(self):
        now = timex.now()
        row = QXOrderResult(raw={"time": int((now + 60) * 1000)})   # ms
        self.assertAlmostEqual(_venue_expiry(row, now), now + 60, delta=1)
        self.assertEqual(_venue_expiry(QXOrderResult(raw={}), now), 0.0)
        self.assertEqual(
            _venue_expiry(QXOrderResult(raw={"time": "soon"}), now), 0.0)


class TestGhostConfig(unittest.TestCase):
    def test_roundtrip_and_validation(self):
        cfg = AppConfig()
        cfg.broker.ghost_pace = False
        cfg.broker.order_think_ms = 50
        cfg.broker.max_orders_per_min = 6
        blob = cfg.to_dict()
        self.assertIn("ghost_pace", blob["broker"])
        self.assertFalse(blob["broker"]["ghost_pace"])
        back = AppConfig.from_dict(json.loads(json.dumps(blob)))
        self.assertEqual(back.broker.max_orders_per_min, 6)
        self.assertFalse(back.broker.ghost_pace)

        cfg.broker.max_orders_per_min = 0
        with self.assertRaises(ConfigError):
            cfg.broker.validate()
        cfg2 = AppConfig()
        cfg2.broker.order_think_ms = -1
        with self.assertRaises(ConfigError):
            cfg2.broker.validate()


if __name__ == "__main__":
    unittest.main()
