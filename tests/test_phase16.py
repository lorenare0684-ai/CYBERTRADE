"""Phase-16 tests — the wire heals: bounded reconnect supervision.

`reconnect_max` sat unused since Phase 1 while a dropped venue socket meant
silent starvation. The supervisor polls link health each cycle: exponential
backoff reconnects (healing only links that were up — a never-started link
is not a dropped wire), resubscribes on success, and gives up loudly at the
attempt cap instead of retrying forever.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from cybertrade.network.supervisor import ReconnectSupervisor
from tests.venue_stubs import VenueFeed, VenueStub


class FakeLink:
    def __init__(self):
        self.connected = True
        self.connect_calls = 0
        self.fail_next = 0          # fail this many connect() attempts
        self.requested = 0

    def connect(self, authorize=True):
        self.connect_calls += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("wire dead")
        self.connected = True
        return True

    def request_instruments(self):
        self.requested += 1


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        self.events = []

    def _sup(self, link, **kw):
        return ReconnectSupervisor(
            link,
            base_delay=1.0,
            max_delay=8.0,
            resubscribe=lambda a: a.request_instruments(),
            on_event=lambda k, p: self.events.append((k, p)),
            **kw,
        )

    def test_standby_when_never_up(self):
        link = FakeLink()
        link.connected = False
        s = self._sup(link)
        self.assertEqual(s.sweep(0.0), "standby")
        self.assertEqual(link.connect_calls, 0)   # do not hammer a dead-at-boot link

    def test_backoff_reconnect_and_resubscribe(self):
        link = FakeLink()
        s = self._sup(link)
        self.assertEqual(s.sweep(0.0), "up")      # link starts healthy
        link.connected = False                    # ...then drops
        link.fail_next = 1
        self.assertEqual(s.sweep(1.0), "retrying")
        self.assertEqual(link.connect_calls, 1)
        self.assertEqual(s.sweep(1.5), "retrying")   # backoff window: no attempt
        self.assertEqual(link.connect_calls, 1)
        self.assertEqual(s.sweep(2.5), "up")      # next window: retry succeeds
        self.assertEqual(link.connect_calls, 2)
        self.assertEqual(link.requested, 1)       # resubscribed after heal
        self.assertEqual(s.status()["attempts"], 0)

    def test_gives_up_loudly_and_stops(self):
        link = FakeLink()
        s = self._sup(link, max_attempts=2)
        s.sweep(0.0)                              # seen up
        link.connected = False
        link.fail_next = 99
        self.assertEqual(s.sweep(1.0), "retrying")
        self.assertEqual(s.sweep(2.0), "given-up")
        calls = link.connect_calls
        self.assertEqual(s.sweep(99.0), "given-up")
        self.assertEqual(link.connect_calls, calls)   # no more hammering
        kinds = [k for k, _ in self.events]
        self.assertIn("giveup", kinds)

    def test_event_stream_is_observable(self):
        link = FakeLink()
        s = self._sup(link)
        s.sweep(0.0)
        link.connected = False
        link.fail_next = 1
        s.sweep(1.0)
        s.sweep(2.5)
        kinds = [k for k, _ in self.events]
        self.assertEqual(kinds, ["retry", "reconnect"])


class TestEngineWiring(unittest.TestCase):
    def _engines(self):
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.brokers.quotex.adapter import QuotexBroker
        from cybertrade.config import AppConfig

        class _Api(FakeLink):
            def __init__(self):
                super().__init__()
                self.demo = True
                self.session = type("S", (), {"ssid": "QX.s", "host": "h", "demo": True})()
                self.assets = {}

            def add_listener(self, fn):
                pass

            def add_tick_handler(self, fn):
                pass

            def get_candles(self, asset, tf=60, count=200, wait=3.0):
                return []

            def payout_for(self, asset, expiry_seconds=60):
                return 0.9

            def account_snapshot(self):
                return type(
                    "S", (),
                    {"balance": 1000.0, "equity": 1000.0, "margin_used": 0.0,
                     "open_positions": 0, "currency": "USD", "is_demo": True},
                )()

            def last_price(self, asset):
                return 1.0

            def close(self):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.journal_path = os.path.join(tmp, "journal.db")
            cfg.calibration_path = os.path.join(tmp, "cal.json")
            venue = QuotexBroker(_Api(), allow_orders=False)
            wire = TradingEngine(cfg, feed=VenueFeed(), broker=venue)
            cfg2 = AppConfig()
            cfg2.journal_path = os.path.join(tmp, "j2.db")
            cfg2.calibration_path = os.path.join(tmp, "c2.json")
            stub = TradingEngine(cfg2, feed=VenueFeed(),
                                 broker=VenueStub())
            yield wire
            yield stub

    def test_venue_engine_heals_its_wire(self):
        wire = list(self._engines())[0]
        wire.boot()
        # a live engine on a real venue wire supervises its reconnects
        self.assertIsNotNone(wire.supervisor)
        self.assertEqual(wire.supervisor.max_attempts,
                         wire.config.broker.reconnect_max)
        wire.cycle()
        wire.shutdown()

    def test_stub_venue_also_supervises(self):
        stub = list(self._engines())[1]
        stub.boot()
        self.assertIsNotNone(stub.supervisor)   # a venue broker either way
        stub.cycle()
        stub.shutdown()


if __name__ == "__main__":
    unittest.main()
