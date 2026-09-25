"""Phase-14 tests — the venue actually connects: SSID session + real warm.

The paper harness built a QuotexAPI with no session and no connect — live
quotes and history could never arrive.  `cli._live_api` authenticates
(session sources: --ssid/config.ssid/QX_SSID/login — never written to disk)
and raises rather than degrading when no session exists.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock as mock
from types import SimpleNamespace

from cybertrade.cli import _live_api
from cybertrade.config import AppConfig
from cybertrade.exceptions import ConfigError
from cybertrade.data.models import Candle

from tests.venue_stubs import VenueFeed, VenueStub
MARKER = 1_000_000.0


def _candles(asset: str, n: int = 5):
    return [
        Candle(asset=asset, timeframe_seconds=60, open_ts=MARKER + i,
               open=1.0, high=1.1, low=0.9, close=1.05)
        for i in range(n)
    ]


class FakeApi:
    def __init__(self, fail: bool = False, have_session: bool = False):
        self.fail = fail
        self.ssid_set = None
        self.logged_in = None
        self._have = have_session
        self.connected = True
        self.demo = True
        self.session = SimpleNamespace(host="qxbroker.com", demo=True, ssid="QX.secret")
        self.assets = {"EURUSD_otc": object(), "GBPUSD_otc": object()}

    def set_ssid(self, ssid: str, cookies: str = ""):
        self.ssid_set = ssid
        self._have = True

    def login(self, email, password, is_demo=None):
        self.logged_in = (email, password)
        self._have = True

    def connect(self, authorize: bool = True):
        if not self._have:
            raise RuntimeError("no session — call login() or set_ssid() first")
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
        if self.fail:
            raise RuntimeError("venue down")
        return _candles(asset, 3)

    def payout_for(self, asset, expiry_seconds=60):
        return 0.9

    def account_snapshot(self):
        return SimpleNamespace(balance=4321.0)


class TestLiveSessionSources(unittest.TestCase):
    def test_env_ssid_reaches_api(self):
        fake = FakeApi()
        cfg = AppConfig()
        os.environ["QX_SSID"] = "QX.envtoken"
        try:
            api = _live_api(cfg, api_factory=lambda: fake)
        finally:
            os.environ.pop("QX_SSID", None)
        self.assertEqual(fake.ssid_set, "QX.envtoken")
        self.assertTrue(getattr(api, "connected", False))

    def test_config_ssid_wins_and_login_fallback(self):
        fake = FakeApi()
        cfg = AppConfig()
        cfg.broker.ssid = "QX.cfgtoken"
        _live_api(cfg, api_factory=lambda: fake)
        self.assertEqual(fake.ssid_set, "QX.cfgtoken")

        fake2 = FakeApi()
        cfg2 = AppConfig()
        cfg2.broker.username = "u"
        cfg2.broker.password = "p"
        _live_api(cfg2, api_factory=lambda: fake2)
        self.assertEqual(fake2.logged_in, ("u", "p"))

    def test_no_session_raises_instead_of_degrading(self):
        with self.assertRaises(Exception):
            _live_api(AppConfig(), api_factory=lambda: FakeApi())


class TestBootWarm(unittest.TestCase):
    def _boot_engine(self, fake):
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.brokers.quotex.adapter import QuotexBroker
        from cybertrade.data.livefeed import LiveQuotexFeed

        cfg = AppConfig()
        cfg.broker.demo_account = True
        with tempfile.TemporaryDirectory() as tmp:
            cfg.journal_path = os.path.join(tmp, "journal.db")
            cfg.calibration_path = os.path.join(tmp, "cal.json")
            feed = LiveQuotexFeed(fake, assets=cfg.strategy.universe,
                                  timeframe_seconds=cfg.timeframe().seconds,
                                  warm_bars=50, refresh_seconds=0.0)
            eng = TradingEngine(cfg, feed=feed,
                                broker=QuotexBroker(fake, allow_orders=False))
            eng.boot()
            return eng

    def test_real_history_lands_in_books(self):
        eng = self._boot_engine(FakeApi())
        asset = eng.feed.assets[0]
        series = eng.feed.book(asset).book(60)
        ts = {c.open_ts for c in series.candles()}
        self.assertIn(MARKER, ts)          # venue candles arrived
        self.assertIn(MARKER + 2, ts)
        eng.shutdown()

    def test_dead_venue_history_blocks_boot(self):
        # the live build refuses to trade on an empty venue book
        from cybertrade.data.feed import FeedError

        with self.assertRaises((FeedError, ConfigError)) as ctx:
            self._boot_engine(FakeApi(fail=True))
        self.assertIn("warmup", str(ctx.exception).lower())


class TestQuotexCommand(unittest.TestCase):
    def test_status_and_warm(self):
        fake = FakeApi(have_session=True)
        with mock.patch("cybertrade.cli._live_api", lambda cfg: fake):
            from cybertrade.cli import main

            self.assertEqual(main(["quotex", "status", "--ssid", "QX.x"]), 0)
            self.assertEqual(main(["quotex", "warm", "--bars", "5"]), 0)

    def test_no_session_is_error(self):
        from cybertrade.cli import main

        os.environ.pop("QX_SSID", None)
        with mock.patch("cybertrade.cli._live_api",
                        side_effect=RuntimeError("no session")):
            self.assertEqual(main(["quotex", "status"]), 1)


if __name__ == "__main__":
    unittest.main()
