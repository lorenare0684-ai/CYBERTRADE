"""Phase-29 tests — the airlock: Chrome pairing + live candles only.

Quotex's CAPTCHA defeats headless login, so the operator solves it by hand
in a persistent Chrome profile; `pairing` detects the `sessionid` cookie
over localhost DevTools and persists it (0600). Live modes load that session
and **refuse synthetic feeds** — `LiveQuotexFeed.is_synthetic is False`,
`TradingEngine` raises `ConfigError` against any generator-backed feed when
`broker.mode` is quotex/dryrun, and `_live_api` hard-fails with a pairing
hint instead of degrading.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest

from cybertrade.brokers.quotex.pairing import (
    chrome_argv,
    cdp_cookies,
    devtools_browser_ws,
    devtools_page_ws,
    devtools_targets,
    extract_session,
    load_session,
    pair_session,
    save_session,
    wait_for_session,
)
from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.data.feed import ReplayFeed
from cybertrade.data.livefeed import LiveQuotexFeed
from cybertrade.data.models import Candle, Tick
from cybertrade.exceptions import ConfigError, FeedError
from tests.venue_stubs import VenueFeed, VenueStub, venue_candles


def _candles(asset: str, n: int = 12):
    return [
        Candle(asset=asset, timeframe_seconds=60, open_ts=1_000_000.0 + i * 60,
               open=1.0, high=1.1, low=0.9, close=1.05 + i * 0.001)
        for i in range(n)
    ]


class FakeApi:
    def __init__(self, bars: int = 12):
        self.bars = bars
        self.subscribed = []
        self.handlers = []
        self.ssid_set = None
        self.connected = True

    def set_ssid(self, ssid, cookies=""):
        self.ssid_set = (ssid, cookies)

    def connect(self, authorize=True):
        return True

    def get_candles(self, asset, tf=60, count=200, wait=3.0):
        return _candles(asset, self.bars) if self.bars else []

    def last_price(self, asset):
        return 1.05

    def subscribe(self, asset, timeframe_seconds=60):
        self.subscribed.append((asset, timeframe_seconds))

    def add_tick_handler(self, fn):
        self.handlers.append(fn)

    def payout_for(self, asset, expiry_seconds=60):
        return 0.85


class TestExtractSession(unittest.TestCase):
    def test_picks_qxbroker_sessionid(self):
        sess = extract_session([
            {"name": "sessionid", "domain": ".evil.com", "value": "nope"},
            {"name": "other", "domain": ".qxbroker.com", "value": "x"},
            {"name": "sessionid", "domain": ".qxbroker.com", "value": "SESS123"},
        ])
        self.assertIsNotNone(sess)
        self.assertEqual(sess["ssid"], "SESS123")
        # sessionid leads; sibling venue cookies (CF clearance) ride along
        self.assertTrue(sess["cookies"].startswith("sessionid=SESS123"))
        self.assertIn("other=x", sess["cookies"])
        self.assertNotIn("nope", sess["cookies"])

    def test_accepts_quotex_front_doors(self):
        for domain in (".quotex.com", "quotex.io", ".qxbroker.com"):
            sess = extract_session([
                {"name": "sessionid", "domain": domain, "value": "S1"},
            ])
            self.assertIsNotNone(sess, domain)
            self.assertEqual(sess["ssid"], "S1")

    def test_none_without_cookie(self):
        self.assertIsNone(extract_session([]))
        self.assertIsNone(extract_session(
            [{"name": "sessionid", "domain": ".evil.com", "value": "z"}]))
        self.assertIsNone(extract_session(
            [{"name": "other", "domain": ".qxbroker.com", "value": "z"}]))


class TestSessionStore(unittest.TestCase):
    def test_roundtrip_0600_and_corrupt_safe(self):
        path = os.path.join(tempfile.mkdtemp(), "qx.json")
        self.assertTrue(save_session(path, {"ssid": "ABC",
                                            "cookies": "sessionid=ABC"}))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(load_session(path)["ssid"], "ABC")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{bad")
        self.assertEqual(load_session(path), {})
        self.assertEqual(load_session(path + ".missing"), {})


class TestChromeLaunch(unittest.TestCase):
    def test_argv_binds_profile_and_localhost_port(self):
        argv = chrome_argv("https://qxbroker.com/en/trade", "/tmp/prof",
                           9333, "/bin/chrome")
        self.assertIn("--remote-debugging-port=9333", argv)
        self.assertIn("--user-data-dir=/tmp/prof", argv)
        self.assertNotIn("--headless", argv)          # CAPTCHA = human hands
        self.assertEqual(argv[-1], "https://qxbroker.com/en/trade")


class TestDevTools(unittest.TestCase):
    def test_devtools_ws_success(self):
        ws = devtools_browser_ws(
            9333, fetch=lambda *a, **k: {
                "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"
            }, deadline=1.0,
        )
        self.assertTrue(ws.startswith("ws://"))

    def test_devtools_ws_times_out(self):
        def boom(*a, **k):
            raise OSError("refused")
        with self.assertRaises(TimeoutError):
            devtools_browser_ws(9333, fetch=boom, deadline=0.4)

    def test_page_ws_prefers_quotex_tab(self):
        targets = [
            {"type": "page", "url": "https://www.google.com/",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/g"},
            {"type": "page", "url": "https://qxbroker.com/en/trade",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/qx"},
        ]
        ws = devtools_page_ws(9333, fetch=lambda *a, **k: targets)
        self.assertTrue(ws.endswith("/page/qx"))

    def test_page_ws_empty_when_no_targets(self):
        self.assertEqual(devtools_page_ws(9333, fetch=lambda *a, **k: []), "")
        self.assertEqual(
            devtools_page_ws(9333, fetch=lambda *a, **k: {"not": "a-list"}), "")
        self.assertEqual(devtools_targets(9333, fetch=lambda *a, **k: {}), [])

    def test_wait_for_session_uses_page_target(self):
        seen = {}

        def fetch(url, timeout=0):
            seen.setdefault("urls", []).append(url)
            if url.endswith("/json/list"):
                return [{"type": "page", "url": "https://quotex.com/en/trade",
                         "webSocketDebuggerUrl": "ws://page-qx"}]
            return {"webSocketDebuggerUrl": "ws://browser"}

        def cdp(ws):
            seen["ws"] = ws
            return [{"name": "sessionid", "domain": ".quotex.com",
                     "value": "PAGE1"}]

        sess = wait_for_session(9333, timeout=2.0, fetch=fetch, cdp=cdp,
                                poll=0.05)
        self.assertEqual(sess["ssid"], "PAGE1")
        self.assertEqual(seen.get("ws"), "ws://page-qx")

    def test_cdp_cookies_parses_storage_response(self):
        class FakeWS:
            def __init__(self, url, timeout=0):
                self.sent = []
            def connect(self):
                pass
            def send(self, data):
                self.sent.append(json.loads(data))
            def recv_text(self):
                return json.dumps({
                    "id": 1,
                    "result": {"cookies": [
                        {"name": "sessionid", "domain": ".qxbroker.com",
                         "value": "CDP1"},
                    ]},
                })
            def close(self):
                pass
        cookies = cdp_cookies("ws://x", timeout=1.0, ws_factory=FakeWS)
        self.assertEqual(cookies[0]["value"], "CDP1")

    def test_wait_for_session_finds_and_times_out(self):
        sess = wait_for_session(
            9333, timeout=2.0,
            fetch=lambda *a, **k: {"webSocketDebuggerUrl": "ws://x"},
            cdp=lambda ws: [{"name": "sessionid", "domain": ".qxbroker.com",
                             "value": "W1"}],
            poll=0.05,
        )
        self.assertEqual(sess["ssid"], "W1")
        with self.assertRaises(TimeoutError) as ctx:
            wait_for_session(
                9333, timeout=0.35,
                fetch=lambda *a, **k: (_ for _ in ()).throw(OSError("down")),
                cdp=lambda ws: [],
                poll=0.1,
            )
        self.assertIn("CAPTCHA", str(ctx.exception))


class TestPairSession(unittest.TestCase):
    def test_launcher_and_waiter_injected_persist_file(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.json")
        seen = {}

        def launcher(url, profile, port, chrome):
            seen["launch"] = (url, profile, port, chrome)

        def waiter(port, timeout=0):
            seen["wait"] = (port, timeout)
            return {"ssid": "P1", "cookies": "sessionid=P1"}

        sess = pair_session(session_path=path, profile_dir="/tmp/p", port=9444,
                            chrome="/bin/chrome", timeout=9.0,
                            launcher=launcher, waiter=waiter)
        self.assertEqual(sess["ssid"], "P1")
        self.assertEqual(seen["launch"][2], 9444)
        self.assertEqual(load_session(path)["ssid"], "P1")


class TestLiveFeed(unittest.TestCase):
    def test_flags_and_warmup_and_ticks(self):
        self.assertFalse(LiveQuotexFeed.is_synthetic)
        self.assertTrue(ReplayFeed.is_synthetic)

        api = FakeApi(bars=12)
        feed = LiveQuotexFeed(api, assets=["EURUSD_otc"],
                              timeframe_seconds=60, warm_bars=50,
                              refresh_seconds=0.0)
        feed.warmup()
        m1 = feed.book("EURUSD_otc").book(60).candles(limit=50)
        self.assertGreaterEqual(len(m1), 12)
        self.assertEqual(feed.last_price("EURUSD_otc"), 1.05)

        seen = []
        feed.add_listener(seen.append)
        feed.start()
        self.assertEqual(api.subscribed, [("EURUSD_otc", 60)])
        self.assertEqual(len(api.handlers), 1)
        api.handlers[0](Tick(asset="EURUSD_otc", price=1.11, ts=2000.0))
        self.assertEqual(len(seen), 1)
        self.assertEqual(feed.last_price("EURUSD_otc"), 1.11)
        feed.stop()

    def test_zero_candles_refuses_boot(self):
        feed = LiveQuotexFeed(FakeApi(bars=0), assets=["EURUSD_otc"],
                              timeframe_seconds=60, warm_bars=10,
                              refresh_seconds=0.0)
        with self.assertRaises(FeedError) as ctx:
            feed.warmup()
        self.assertIn("quotex login", str(ctx.exception))


class TestEngineRefusesForeignFeeds(unittest.TestCase):
    def test_replay_feed_is_refused_in_live_mode(self):
        cfg = AppConfig()
        cfg.broker.mode = "quotex"
        with self.assertRaises(ConfigError) as ctx:
            TradingEngine(cfg, feed=ReplayFeed("EURUSD_otc", venue_candles(n=5)),
                          broker=VenueStub())
        self.assertIn("quotex login", str(ctx.exception))

    def test_any_mode_but_quotex_is_refused(self):
        for mode in ("paper", "dryrun", "sim", "bogus"):
            cfg = AppConfig()
            cfg.broker.mode = mode
            with self.assertRaises(ConfigError):
                TradingEngine(cfg, feed=VenueFeed(assets=["EURUSD_otc"]),
                              broker=VenueStub())

    def test_missing_broker_or_feed_is_refused(self):
        cfg = AppConfig()
        cfg.broker.mode = "quotex"
        with self.assertRaises(ConfigError):
            TradingEngine(cfg, feed=None, broker=None)


class TestLiveApiSessionSources(unittest.TestCase):
    def test_paired_session_file_reaches_set_ssid(self):
        from cybertrade.cli import _live_api

        tmp = tempfile.mkdtemp()
        cfg = AppConfig()
        cfg.qx_session_path = os.path.join(tmp, "qx.json")
        save_session(cfg.qx_session_path,
                     {"ssid": "FILESSID", "cookies": "sessionid=FILESSID"})
        captured = {}

        class Api:
            def set_ssid(self, ssid, cookies=""):
                captured["ssid"] = ssid
                captured["cookies"] = cookies
            def connect(self, authorize=True):
                return True

        api = _live_api(cfg, api_factory=lambda: Api())
        self.assertEqual(captured["ssid"], "FILESSID")
        self.assertEqual(captured["cookies"], "sessionid=FILESSID")
        self.assertIsNotNone(api)

    def test_no_session_raises_with_pairing_hint(self):
        from cybertrade.cli import _live_api

        cfg = AppConfig()
        cfg.qx_session_path = os.path.join(tempfile.mkdtemp(), "none.json")
        with self.assertRaises(ConfigError) as ctx:
            _live_api(cfg, api_factory=lambda: FakeApi())
        self.assertIn("quotex login", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
