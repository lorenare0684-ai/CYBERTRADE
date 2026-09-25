"""Quotex integration tests: wire fixtures, catalog sync, history warm-start.

Everything runs offline against recorded/documented wire shapes distilled in
docs/QUOTEX_PROTOCOL.md — no sockets are opened.
"""

from __future__ import annotations

import threading
import unittest

from cybertrade.brokers.quotex.catalog import AssetCatalog
from cybertrade.brokers.quotex.models import QXAsset, QXCandle, QXOrderRequest
from cybertrade.brokers.quotex.protocol import (
    build_authorization,
    build_candle_history,
    build_change_balance,
    build_chart_notification,
    build_depth_follow,
    build_depth_unfollow,
    build_drawing_load,
    build_indicator_list,
    build_instruments,
    build_order,
    build_pending_list,
    build_sell_option,
    build_subscribe_candles,
    build_tick,
    build_unsubscribe_candles,
    is_placeholder,
    parse_balance,
    parse_candles,
    parse_instruments,
    parse_order_result,
    parse_quotes,
    parse_tick,
)
from cybertrade.brokers.quotex.sync import qxcandles_into_series, warm_asset
from cybertrade.constants import Side
from cybertrade.data.history import CandleSeries
from cybertrade.data.models import Candle


class TestWireBuilders(unittest.TestCase):
    def test_authorization_shape(self):
        wire = build_authorization("abc123ssid", is_demo=True)
        self.assertTrue(wire.startswith('42["authorization"'))
        self.assertIn('"session":"abc123ssid"', wire)
        self.assertIn('"isDemo":1', wire)
        # pyquotex-exact: no isFastHistory — unknown fields stay out.
        self.assertNotIn('isFastHistory', wire)

    def test_order_shape_legacy_and_new(self):
        req = QXOrderRequest(
            asset="AUDCAD_otc", amount=6.0, action="put",
            duration=60, time=1637893200, request_id="r1",
        )
        wire = build_order(req)
        self.assertIn('"orders/open"', wire)
        self.assertIn('"asset":"AUDCAD_otc"', wire)
        self.assertIn('"amount":6', wire)
        self.assertIn('"action":"put"', wire)
        self.assertIn('"requestId":"r1"', wire)
        legacy = build_order(req, legacy=True)
        self.assertIn('"buyOption"', legacy)

    def test_helpers_shapes(self):
        self.assertIn('"sellOption"', build_sell_option("o-1"))
        # purse switch rides account/change with a demo flag
        self.assertIn('"account/change"', build_change_balance("REAL"))
        self.assertIn('"demo":0', build_change_balance("REAL"))
        self.assertIn('"demo":1', build_change_balance("PRACTICE"))
        # instrument listing request
        self.assertIn('42["instruments/get"]', build_instruments())
        # subscribe trio
        sub = build_subscribe_candles("EURUSD_otc", 60)
        self.assertIn('"instruments/update"', sub)
        self.assertIn('"period":60', sub)
        note = build_chart_notification("EURUSD_otc")
        self.assertIn('"chart_notification/get"', note)
        self.assertIn('"version":"1.0.0"', note)
        self.assertIn('42["chart_notification/get"]', build_chart_notification())
        foll = build_depth_follow("EURUSD_otc")
        self.assertIn('"depth/follow"', foll)
        self.assertIn("EURUSD_otc", foll)
        self.assertIn('"depth/unfollow"', build_depth_unfollow("EURUSD_otc"))
        # history request carries index/time/offset/period
        hist = build_candle_history("EURUSD_otc", 60, 100, now=1700000000.0)
        self.assertIn('"history/load"', hist)
        self.assertIn('"period":60', hist)
        self.assertIn('"offset":6000', hist)
        self.assertIn('"time":1700000000', hist)
        self.assertIn('"index":', hist)
        # heartbeat + bootstrap are bare emits
        self.assertEqual(build_tick(), '42["tick"]')
        self.assertEqual(build_indicator_list(), '42["indicator/list"]')
        self.assertEqual(build_drawing_load(), '42["drawing/load"]')
        self.assertEqual(build_pending_list(), '42["pending/list"]')
        # unsub names the asset
        unsub = build_unsubscribe_candles("EURUSD_otc")
        self.assertIn('"instruments/unsubscribe"', unsub)


class TestParsers(unittest.TestCase):
    def test_parse_tick_dict_and_list(self):
        asset, price, ts = parse_tick([{"asset": "EURUSD_otc", "price": 1.1, "time": 1700000000000}])
        self.assertEqual(asset, "EURUSD_otc")
        self.assertAlmostEqual(price, 1.1)
        self.assertEqual(ts, 1700000000000)
        # bare price push
        asset2, price2, _ = parse_tick([1.2345], default_asset="X")
        self.assertEqual(asset2, "X")
        self.assertAlmostEqual(price2, 1.2345)
        # [asset, price, ts] row
        asset3, price3, ts3 = parse_tick([["XAUUSD", 1999.5, 1700000000000]])
        self.assertEqual(asset3, "XAUUSD")
        self.assertAlmostEqual(price3, 1999.5)
        self.assertEqual(ts3, 1700000000000)

    def test_parse_balance(self):
        b = parse_balance([{"balance": 1002.5, "currency": "USD"}])
        self.assertAlmostEqual(b.balance, 1002.5)
        b2 = parse_balance([500.0])
        self.assertAlmostEqual(b2.balance, 500.0)

    def test_parse_order_result(self):
        r = parse_order_result([{
            "orderId": "991", "requestId": "r7", "status": "open",
            "asset": "EURUSD_otc", "amount": 5,
        }])
        self.assertEqual(r.order_id, "991")
        self.assertEqual(r.request_id, "r7")

    def test_parse_instruments_all_shapes(self):
        # mapping form
        a = parse_instruments([{"EURUSD": {"payout": 85, "id": 1}}])
        self.assertEqual(a[0].name, "EURUSD")
        self.assertAlmostEqual(a[0].payout, 0.85)
        # row form
        b = parse_instruments([[{"asset": "GBPUSD", "payout": 84, "type": "forex"}]])
        self.assertEqual(b[0].name, "GBPUSD")
        self.assertEqual(b[0].kind, "forex")
        # pair form
        c = parse_instruments([[["USDJPY", {"profit": 90, "isOTC": True}]]])
        self.assertEqual(c[0].name, "USDJPY")
        self.assertAlmostEqual(c[0].payout, 0.90)
        self.assertTrue(c[0].is_otc)
        # name-keyed row
        d = parse_instruments([{"asset": "XAUUSD", "payout": 80, "isOpen": False}])
        self.assertEqual(d[0].name, "XAUUSD")
        self.assertFalse(d[0].open)

    def test_parse_instruments_positional_rows(self):
        rows = [[1, "EURUSD", "EUR/USD", "forex", 4, 84, 60, 30, 3, 1,
                 0, 0, [], 1, True]]
        a = parse_instruments([rows])
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].name, "EURUSD")
        self.assertEqual(a[0].asset_id, "1")
        self.assertAlmostEqual(a[0].payout, 0.84)
        self.assertTrue(a[0].open)
        self.assertEqual(a[0].kind, "forex")
        shut = [[169, "XAUUSD_otc", "Gold OTC", "metal", 2, 0, 60, 30, 3,
                 1, 0, 0, [], 1, False]]
        b = parse_instruments([shut])
        self.assertEqual(b[0].kind, "metal_otc")
        self.assertFalse(b[0].open)

    def test_parse_quotes_batch(self):
        rows = [["EURUSD_otc", 1700000000, 1.08521, 1],
                ["XAUUSD", 1700000001, 2380.5, 0]]
        got = parse_quotes(rows)
        self.assertEqual(got, [("EURUSD_otc", 1.08521, 1700000000),
                               ("XAUUSD", 2380.5, 1700000001)])
        self.assertEqual(parse_quotes([["bogus"]]), [])
        self.assertEqual(parse_quotes([["X", 1, 0.0]]), [])  # no zero ticks

    def test_parse_candles_aggregates_history_ticks(self):
        base = 1700000000 - (1700000000 % 60)
        ticks = [[base + i, 1.0 + i * 0.001, 1] for i in range(150)]
        out = parse_candles("EURUSD_otc",
                            [{"asset": "EURUSD_otc", "candles": ticks}], 60)
        # 150s of ticks → buckets [base, base+60, base+120]; forming dropped
        self.assertEqual([c.open_ts for c in out], [base, base + 60])
        self.assertAlmostEqual(out[0].open, 1.0)
        self.assertAlmostEqual(out[0].close, 1.059)
        self.assertEqual(out[0].timeframe_seconds, 60)
        # ms timestamps normalize to seconds
        ms = [[(base + i) * 1000, 2.0, 0] for i in range(130)]
        out2 = parse_candles("X", [{"candles": ms}], 60)
        self.assertEqual([c.open_ts for c in out2], [base, base + 60])

    def test_parse_candles_keeps_ohlc_rows(self):
        rows = [[1700000000, 1.0, 1.1, 1.2, 0.9],
                [1700000060, 1.1, 1.05, 1.15, 1.0]]
        out = parse_candles("EURUSD_otc", [{"asset": "EURUSD_otc", "data": rows}], 60)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[1].close, 1.05)
        self.assertAlmostEqual(out[1].high, 1.15)

    def test_parse_balance_demo_and_live(self):
        b = parse_balance([{"demoBalance": 1002.5, "liveBalance": 50.0}])
        self.assertAlmostEqual(b.balance, 1002.5)
        self.assertEqual(b.account_type, "PRACTICE")
        live = parse_balance([{"demoBalance": 1002.5, "liveBalance": 50.0}],
                             demo=False)
        self.assertAlmostEqual(live.balance, 50.0)
        self.assertEqual(live.account_type, "REAL")
        uid = parse_balance([{"demoBalance": 10, "userId": "U-42"}])
        self.assertEqual(uid.user_id, "U-42")

    def test_candle_ts_and_tf_aliases(self):
        qc = QXCandle.from_payload("A", {"index": 1700000000, "period": 300,
                                         "open": 1, "high": 2, "low": 0.5,
                                         "close": 1.5}, 60)
        self.assertEqual(qc.open_ts, 1700000000)
        self.assertEqual(qc.timeframe_seconds, 300)

    def test_is_placeholder_counts_attachments(self):
        self.assertEqual(is_placeholder([{"_placeholder": True, "num": 0}]), 1)
        self.assertEqual(is_placeholder([{"_placeholder": True, "num": 2}]), 3)
        self.assertIsNone(is_placeholder([{"asset": "X"}]))
        self.assertIsNone(is_placeholder([]))


class TestCatalog(unittest.TestCase):
    def test_static_seed(self):
        cat = AssetCatalog.from_static()
        self.assertGreater(len(cat), 8)
        self.assertGreater(cat.payout_for("EURUSD_otc"), 0.5)
        self.assertTrue(cat.is_tradable("EURUSD_otc"))
        self.assertEqual(cat.kind_of("EURUSD_otc"), "forex_otc")

    def test_server_update_overrides_payout(self):
        cat = AssetCatalog.from_static()
        cat.update([QXAsset(name="EURUSD_otc", payout=0.92, open=False, kind="forex_otc")])
        self.assertAlmostEqual(cat.payout_for("EURUSD_otc"), 0.92)
        self.assertFalse(cat.is_tradable("EURUSD_otc"))
        self.assertIn("forex_otc", cat.by_kind())
        rows = cat.to_list()
        self.assertTrue(any(r["name"] == "EURUSD_otc" and r["payout"] == 0.92 for r in rows))

    def test_unknown_asset_defaults_open(self):
        cat = AssetCatalog()
        self.assertTrue(cat.is_tradable("NOPE"))
        self.assertAlmostEqual(cat.payout_for("NOPE", 0.77), 0.77)


class TestSync(unittest.TestCase):
    def test_qxcandles_into_series_dedupe(self):
        series = CandleSeries("EURUSD_otc", 60, maxlen=100)
        candles = [
            Candle(asset="EURUSD_otc", timeframe_seconds=60, open_ts=1700000000.0 + i * 60,
                   open=1.0, high=1.1, low=0.9, close=1.05, closed=True)
            for i in range(5)
        ]
        self.assertEqual(qxcandles_into_series(candles, series), 5)
        self.assertEqual(qxcandles_into_series(candles, series), 0)  # dupe-safe
        self.assertEqual(len(series), 5)

    def test_warm_asset_swallows_errors(self):
        class Boom:
            def get_candles(self, *a, **kw):
                raise RuntimeError("venue down")

        self.assertEqual(warm_asset(Boom(), "EURUSD_otc", bars=50, wait=0.01), 0)

    def test_warm_asset_fills_from_cache(self):
        class FakeAPI:
            def get_candles(self, asset, tf=60, count=10, wait=0.0):
                return [
                    Candle(asset=asset, timeframe_seconds=tf,
                           open_ts=1700000000.0 + i * tf, open=1.0, high=1.1,
                           low=0.9, close=1.05, closed=True)
                    for i in range(count)
                ]

        series = CandleSeries("EURUSD_otc", 60, maxlen=100)
        n = warm_asset(FakeAPI(), "EURUSD_otc", bars=10, series=series, wait=0.01)
        self.assertEqual(n, 10)
        self.assertEqual(len(series), 10)

    def test_silent_warm_stays_below_info(self):
        """A venue that answers nothing must not flood the console.

        Every asset × timeframe logs one line per warmup; at INFO a dead
        session buries the one line that matters (degraded boot).
        """
        class Silent:
            def get_candles(self, *a, **kw):
                return []

        with self.assertNoLogs("cybertrade.qx.sync", level="INFO"):
            self.assertEqual(
                warm_asset(Silent(), "EURUSD_otc", bars=10, wait=0.01), 0)


class TestApiDispatcherOffline(unittest.TestCase):
    def test_instruments_event_populates_catalog(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        api._on_socket_event("instrument", [[{"EURUSD": {"payout": 88}, "GBPUSD": {"payout": 82}}]])
        self.assertIn("EURUSD", api.assets)
        self.assertAlmostEqual(api.payout_for("EURUSD"), 0.88)
        self.assertTrue(api.is_tradable("EURUSD"))
        # static fallback still works for un-synced names
        self.assertGreater(api.payout_for("EURUSD_otc"), 0.5)

    def test_qxasset_kind_extracted(self):
        a = QXAsset.from_payload("BTCUSD", {"payout": 70, "type": "crypto"})
        self.assertEqual(a.kind, "crypto")
        self.assertAlmostEqual(a.payout, 0.70)

    def test_quotes_batch_feeds_ticks(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        seen = []
        api.add_tick_handler(seen.append)
        api._on_socket_event("quotes", [[["EURUSD_otc", 1700000000, 1.08521, 1]]])
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0].price, 1.08521)
        self.assertAlmostEqual(api.last_price("EURUSD_otc"), 1.08521)

    def test_candle_generated_ingests_bar_and_tick(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        seen = []
        api.add_tick_handler(seen.append)
        api._on_socket_event("candle-generated", [{
            "asset": "EURUSD_otc", "period": 60, "index": 1700000000,
            "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1}])
        bars = api._candles.get(("EURUSD_otc", 60), [])
        self.assertEqual(len(bars), 1)
        self.assertAlmostEqual(bars[0].close, 1.1)
        self.assertEqual(len(seen), 1)

    def test_instruments_list_positional_rows(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        rows = [[1, "EURUSD", "EUR/USD", "forex", 4, 84, 60, 30, 3, 1,
                 0, 0, [], 1, True]]
        api._on_socket_event("instruments/list", [rows])
        self.assertAlmostEqual(api.payout_for("EURUSD"), 0.84)
        self.assertTrue(api.is_tradable("EURUSD"))

    def test_s_authorization_emits_authorized(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        seen = []
        api.add_listener(lambda kind, payload: seen.append(kind))
        api._on_socket_event("s_authorization", [{}])
        self.assertIn("authorized", seen)

    def test_balance_push_selects_purse_and_uid(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI(demo=True)
        self.assertFalse(api.wait_for_balance(timeout=0.01))
        api._on_socket_event("balance", [{"demoBalance": 777.0, "liveBalance": 1.0,
                                          "userId": "U-7"}])
        self.assertTrue(api.wait_for_balance(timeout=0.5))
        self.assertAlmostEqual(api.balance.balance, 777.0)
        self.assertEqual(api.session.user_id, "U-7")


class TestLoginSession(unittest.TestCase):
    def test_extract_ssid_shapes(self):
        from cybertrade.brokers.quotex.api import _extract_ssid

        self.assertEqual(_extract_ssid({"session": "s1"}), "s1")
        self.assertEqual(_extract_ssid({"ssid": "s2"}), "s2")
        self.assertEqual(_extract_ssid({"data": {"session": "s3"}}), "s3")
        self.assertEqual(_extract_ssid({"data": {"ssid": "s4"}}), "s4")
        self.assertEqual(_extract_ssid({"data": {"token": "t6"}}), "t6")
        self.assertEqual(_extract_ssid('"bare-token-123"'), "bare-token-123")
        self.assertEqual(_extract_ssid({}), "")
        self.assertEqual(_extract_ssid({"session": ""}), "")

    def test_coerce_ssid_unwraps_pasted_frame(self):
        from cybertrade.brokers.quotex.api import QuotexAPI, _coerce_ssid

        frame = '42["authorization",{"session":"FRM123","isDemo":0}]'
        self.assertEqual(_coerce_ssid(frame), "FRM123")
        self.assertEqual(_coerce_ssid("  bare123  "), "bare123")
        self.assertEqual(_coerce_ssid(""), "")
        api = QuotexAPI()
        api.set_ssid(frame)
        self.assertEqual(api.session.ssid, "FRM123")

    def test_login_bad_password_message(self):
        from cybertrade.brokers.quotex.api import QuotexAPI
        from cybertrade.exceptions import BrokerAuthError
        from cybertrade.network.http_client import HttpResponse

        api = QuotexAPI()
        api.http.post = lambda *a, **k: HttpResponse(
            status=401, headers={}, body=b'{"message":"bad"}')
        with self.assertRaises(BrokerAuthError) as ctx:
            api.login("u@x.com", "wrong")
        self.assertIn("bad email/password", str(ctx.exception))

    def test_login_challenge_message(self):
        from cybertrade.brokers.quotex.api import QuotexAPI
        from cybertrade.exceptions import BrokerAuthError
        from cybertrade.network.http_client import HttpResponse

        api = QuotexAPI()
        api.http.post = lambda *a, **k: HttpResponse(
            status=200, headers={},
            body=b"<html><body>Just a moment... cf-challenge</body></html>")
        with self.assertRaises(BrokerAuthError) as ctx:
            api.login("u@x.com", "pw")
        self.assertIn("quotex login", str(ctx.exception))

    def test_login_nested_token_and_cookie_fallback(self):
        from cybertrade.brokers.quotex.api import QuotexAPI
        from cybertrade.network.http_client import HttpResponse

        api = QuotexAPI()
        api.http.post = lambda *a, **k: HttpResponse(
            status=200, headers={}, body=b'{"data":{"token":"NESTED123456"}}')
        self.assertEqual(api.login("u@x.com", "pw").ssid, "NESTED123456")

        api2 = QuotexAPI()
        api2.http.jar.set_from_header("sessionid=COOKIE99; Path=/")
        api2.http.post = lambda *a, **k: HttpResponse(
            status=200, headers={}, body=b'{"ok":true}')
        self.assertEqual(api2.login("u@x.com", "pw").ssid, "COOKIE99")

    def test_login_prefers_live_session_cookie(self):
        from cybertrade.brokers.quotex.api import QuotexAPI
        from cybertrade.network.http_client import HttpResponse

        api = QuotexAPI()
        api.http.jar.set_from_header("sessionid=OLD; Path=/")
        api.http.jar.set_from_header("session=NEWLIVE; Path=/")
        api.http.post = lambda *a, **k: HttpResponse(
            status=200, headers={}, body=b'{"ok":true}')
        self.assertEqual(api.login("u@x.com", "pw").ssid, "NEWLIVE")

    def test_login_falls_back_to_alt_base(self):
        from cybertrade.brokers.quotex import constants as C
        from cybertrade.brokers.quotex.api import QuotexAPI
        from cybertrade.exceptions import NetworkError
        from cybertrade.network.http_client import HttpResponse

        api = QuotexAPI()
        calls = []

        def fake_post(path, **kw):
            calls.append(api.http.base_url)
            if len(calls) == 1:
                raise NetworkError("route dead")
            return HttpResponse(
                status=200, headers={}, body=b'{"session":"ALTROUTE1"}')

        api.http.post = fake_post
        sess = api.login("u@x.com", "pw")
        self.assertEqual(sess.ssid, "ALTROUTE1")
        self.assertEqual(len(calls), 2)
        self.assertEqual(api.http_base, C.HTTP_BASE_ALT)


class TestSocketAuth(unittest.TestCase):
    def test_auth_event_constant_exists(self):
        from cybertrade.brokers.quotex import constants as C

        # A misspelled attribute here once killed the reader thread on the
        # first venue frame, so every login silently went deaf.
        self.assertEqual(C.EV_AUTHORIZATION, "authorization")
        self.assertEqual(C.SV_S_AUTHORIZATION, "s_authorization")
        self.assertEqual(C.SV_AUTH_SUCCESS, "s_authorization")
        self.assertEqual(C.SV_AUTH_REJECT, "authorization/reject")

    def test_authorize_fails_fast_on_session_fault(self):
        from cybertrade.brokers.quotex.client import QuotexSocket
        from cybertrade.exceptions import BrokerAuthError

        sock = QuotexSocket("dead-session")

        class Conn:
            closed = False

            def send(self, wire):
                pass

        sock.conn = Conn()
        sock._connected.set()  # connect() always sets this before authorize()
        # The reader thread reports the fault mid-handshake; authorize()
        # must surface it instead of waiting out the window.
        timer = threading.Timer(
            0.05, sock._note_session_fault,
            args=("invalid session, please login again",))
        timer.start()
        try:
            with self.assertRaises(BrokerAuthError) as ctx:
                sock.authorize(timeout=2.0)
        finally:
            timer.join()
        self.assertIn("quotex login", str(ctx.exception))

    def test_authorize_proceeds_when_quiet(self):
        from cybertrade.brokers.quotex.client import QuotexSocket

        sock = QuotexSocket("maybe-session")

        class Conn:
            closed = False

            def send(self, wire):
                pass

        sock.conn = Conn()
        sock._connected.set()
        self.assertIsNone(sock.authorize(timeout=0.05))

    def test_authorize_aborts_when_wire_died(self):
        from cybertrade.brokers.quotex.client import QuotexSocket
        from cybertrade.exceptions import BrokerConnectionError

        sock = QuotexSocket("dead-session")

        class Conn:
            closed = False

            def send(self, wire):
                pass

        sock.conn = Conn()
        # Reader dead + namespace never connected: fail now, not after 10s.
        with self.assertRaises(BrokerConnectionError):
            sock.authorize(timeout=5.0)

    def test_error_frame_reaches_api_dispatcher(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        seen = []
        api.add_listener(lambda kind, payload: seen.append(kind))
        api._on_socket_event("error", [{"message": "invalid session"}])
        self.assertIn("error", seen)
        self.assertIn("session_stale", seen)

    def test_has_live_session_reads_api_session(self):
        import types

        from cybertrade.brokers.quotex.api import QuotexAPI
        from cybertrade.cli import _has_live_session

        def hub_for(api):
            return types.SimpleNamespace(
                engine=types.SimpleNamespace(feed=types.SimpleNamespace(api=api)))

        live = QuotexAPI()
        live.set_ssid("LIVE123")
        self.assertTrue(_has_live_session(hub_for(live)))
        self.assertFalse(_has_live_session(hub_for(QuotexAPI())))
        self.assertFalse(_has_live_session(types.SimpleNamespace(engine=None)))


class TestClientResilience(unittest.TestCase):
    def _sock(self):
        from cybertrade.brokers.quotex.client import QuotexSocket

        return QuotexSocket("ssid")

    def test_recv_timeout_keeps_waiting(self):
        from cybertrade.exceptions import NetworkError, RecvTimeoutError

        events = []
        sock = self._sock()
        sock.on_event = lambda name, args: events.append((name, args))
        calls = {"n": 0}

        class Scripted:
            closed = False

            def recv_message(self):
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise RecvTimeoutError("recv timeout")
                if calls["n"] == 3:
                    return (0x1, b'42["s_authorization",{}]')
                raise NetworkError("scripted end")

            def send(self, wire):
                pass

        sock.conn = Scripted()
        sock._running = True
        heals = []
        sock._try_reconnect_guarded = lambda: heals.append(1)
        sock._read_loop()
        self.assertEqual(events, [("s_authorization", [{}])])
        self.assertTrue(sock._authorized.is_set())
        self.assertEqual(heals, [])  # timeouts never heal the wire

    def test_classify_silence(self):
        from cybertrade.brokers.quotex.client import _classify_silence

        self.assertEqual(_classify_silence(10.0, 25.0, None), "ok")
        self.assertEqual(_classify_silence(51.0, 25.0, None), "probe")
        self.assertEqual(_classify_silence(51.0, 25.0, 5.0), "ok")  # grace
        self.assertEqual(_classify_silence(80.0, 25.0, 26.0), "dead")
        self.assertEqual(_classify_silence(100.0, 5.0, 11.0), "dead")  # 10s floor

    def test_placeholder_then_binary_dispatches_event(self):
        import json

        from cybertrade.brokers.quotex.protocol import is_placeholder, parse_event
        from cybertrade.network.socketio import EngineIOSession

        sock = self._sock()
        events = []
        sock.on_event = lambda name, args: events.append((name, args))
        eng, sio = EngineIOSession().on_raw(
            '451-["instruments/list",{"_placeholder":true,"num":0}]')
        self.assertEqual(sio.type, "5")
        self.assertEqual(sio.attachments, 1)
        name, args = parse_event(sio)
        self.assertEqual(name, "instruments/list")
        need = is_placeholder(args)
        self.assertEqual(need, 1)
        sock._pending_bin = {"event": name, "need": need, "got": []}
        rows = [[1, "EURUSD", "EUR/USD", "forex", 4, 84, 60, 30, 3, 1,
                 0, 0, [], 1, True]]
        sock._on_binary(json.dumps(rows).encode())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "instruments/list")
        self.assertEqual(events[0][1][0], rows)
        self.assertIsNone(sock._pending_bin)

    def test_unframed_quote_batch_dispatches_quotes(self):
        sock = self._sock()
        events = []
        sock.on_event = lambda name, args: events.append((name, args))
        self.assertTrue(sock._handle_unframed(
            '[["EURUSD_otc",1700000000,1.08,1]]'))
        self.assertEqual(events[0][0], "quotes")
        self.assertFalse(sock._handle_unframed("not json {{{"))
        self.assertFalse(sock._handle_unframed('{"unrelated":true}'))

    def test_binary_base64_fallback(self):
        import base64
        import json

        sock = self._sock()
        events = []
        sock.on_event = lambda name, args: events.append((name, args))
        rows = [["EURUSD_otc", 1700000000, 1.08, 1]]
        blob = base64.b64encode(b"\x04" + json.dumps(rows).encode()).decode()
        self.assertTrue(blob.startswith("BFtb"))
        sock._on_binary(blob.encode())
        self.assertEqual(events[0][0], "quotes")
        # Same blob wrapped into a *text* frame routes identically.
        events.clear()
        self.assertTrue(sock._handle_unframed(blob))
        self.assertEqual(events[0][0], "quotes")

    def test_reject_fails_fast_and_marks_error(self):
        from cybertrade.exceptions import BrokerAuthError

        sock = self._sock()

        class Conn:
            closed = False

            def send(self, wire):
                pass

        sock.conn = Conn()
        sock._connected.set()
        errors = []
        sock.on_event = lambda name, args: errors.append(name)
        timer = threading.Timer(0.05, sock._got_event,
                                args=("authorization/reject", ["nope"]))
        timer.start()
        try:
            with self.assertRaises(BrokerAuthError):
                sock.authorize(timeout=2.0)
        finally:
            timer.join()
        self.assertEqual(errors, ["error"])

    def test_pump_sends_tick_and_probes_then_dies(self):
        sock = self._sock()
        sent = []

        class Conn:
            closed = False

            def send(self, wire):
                sent.append(wire)

        sock.conn = Conn()
        sock._connected.set()
        sock._last_pong = 1000.0
        sock._next_tick = 0.0
        # healthy lap: heartbeat only
        self.assertAlmostEqual(sock._pump_once(1001.0), 5.0)
        self.assertEqual(sent, ['42["tick"]'])
        # long silence: probe, still alive
        sent.clear()
        sock._next_tick = 9999.0  # isolate the probe from the heartbeat
        sock._pump_once(1060.0)
        self.assertEqual(sent, ["2"])
        self.assertIsNotNone(sock._probe_at)
        # probe unanswered past its grace: reconnect, no death spiral
        heals = []
        sock._try_reconnect_guarded = lambda: heals.append(1)
        sent.clear()
        sock._pump_once(1090.0)
        self.assertEqual(heals, [1])
        # traffic clears the probe
        sock._last_pong = 1091.0
        sock._probe_at = 1089.0
        sock._pump_once(1092.0)
        self.assertIsNone(sock._probe_at)


class TestApiMarketData(unittest.TestCase):
    def _api(self):
        from cybertrade.brokers.quotex.api import QuotexAPI
        from cybertrade.brokers.quotex.ghost import Pacekeeper

        api = QuotexAPI(pace=Pacekeeper(enabled=False))

        class Sock:
            def __init__(self):
                self.sent = []

            def send(self, wire):
                self.sent.append(wire)

        api.socket = Sock()
        return api

    def test_subscribe_sends_trio(self):
        api = self._api()
        api.subscribe("EURUSD_otc", 60)
        wires = "\n".join(api.socket.sent)
        self.assertIn('"instruments/update"', wires)
        self.assertIn('"chart_notification/get"', wires)
        self.assertIn('"depth/follow"', wires)
        self.assertIn(("EURUSD_otc", 60), api._subs)

    def test_get_candles_subscribes_first_and_tracks_tf(self):
        api = self._api()
        api.get_candles("EURUSD_otc", 300, count=10, wait=0.0)
        wires = "\n".join(api.socket.sent)
        self.assertLess(wires.index("instruments/update"), wires.index("history/load"))
        self.assertIn('"period":300', wires)
        self.assertIn('"offset":3000', wires)
        self.assertEqual(api._hist_tf["EURUSD_otc"], 300)
        # second call reuses the subscription — no duplicate trio
        api.socket.sent.clear()
        api.get_candles("EURUSD_otc", 300, count=10, wait=0.0)
        self.assertNotIn("instruments/update", "\n".join(api.socket.sent))

    def test_bootstrap_sends_five_frames(self):
        api = self._api()
        api._bootstrap_session()
        wires = "\n".join(api.socket.sent)
        for name in ("indicator/list", "drawing/load", "pending/list",
                     "chart_notification/get", "instruments/get"):
            self.assertIn('"' + name + '"', wires)

    def test_request_balance_sends_nothing(self):
        api = self._api()
        api.request_balance()
        self.assertEqual(api.socket.sent, [])

    def test_history_response_keys_by_requested_tf(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        api._hist_tf["EURUSD_otc"] = 300
        base = 1700000000 - (1700000000 % 300)
        ticks = [[base + i * 7, 1.0 + i * 0.0001, 1] for i in range(400)]
        api._on_socket_event("history/load", [{"asset": "EURUSD_otc",
                                               "candles": ticks}])
        bars = api._candles.get(("EURUSD_otc", 300), [])
        self.assertGreater(len(bars), 5)
        self.assertTrue(all(b.timeframe_seconds == 300 for b in bars))

    def test_data_seen_set_by_each_data_event(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        rows = [[1700000000 + i * 60, 1.08, 1] for i in range(10)]
        events = [
            ("quotes", [[["EURUSD_otc", 1700000000, 1.08, 1]]]),
            ("tick", [{"asset": "EURUSD_otc", "price": 1.08, "ts": 1}]),
            ("history/load", [{"asset": "EURUSD_otc", "candles": rows}]),
            ("balance", [{"demoBalance": 1000.0}]),
            ("instruments/list", [[[1, "EURUSD_otc", "EUR/USD", 1, 0, 85]]]),
        ]
        for name, args in events:
            with self.subTest(name=name):
                api = QuotexAPI()
                self.assertFalse(api.wait_for_data(timeout=0))
                api._on_socket_event(name, args)
                self.assertTrue(api.wait_for_data(timeout=0))

    def test_first_data_arrival_is_announced_once(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        batch = [[["EURUSD_otc", 1700000000, 1.08, 1]]]
        with self.assertLogs("cybertrade.qx.api", level="INFO") as logs:
            api._on_socket_event("quotes", batch)
            api._on_socket_event("balance", [{"demoBalance": 5.0}])
        flowing = [m for m in logs.output if "venue data flowing" in m]
        self.assertEqual(len(flowing), 1)
        self.assertIn("quotes", flowing[0])

    def test_data_seen_ignores_control_events(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        api._on_socket_event("s_authorization", [])
        api._on_socket_event("s_account/change", [])
        api._on_socket_event("something/unknown", [{"x": 1}])
        self.assertFalse(api.wait_for_data(timeout=0))

    def test_s_confirms_are_logged_not_dispatched(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        api = QuotexAPI()
        seen = []
        api.add_listener(lambda kind, payload: seen.append(kind))
        with self.assertLogs("cybertrade.qx.api", level="INFO"):
            api._on_socket_event("s_account/change", [])
            api._on_socket_event("s_anything/new", [])
        self.assertEqual(seen, [])

    def test_get_candles_warns_when_venue_silent(self):
        api = self._api()
        with self.assertLogs("cybertrade.qx.api", level="WARNING") as logs:
            out = api.get_candles("EURUSD_otc", 60, count=10, wait=0.0)
        self.assertEqual(out, [])
        self.assertTrue(any("venue silent" in m for m in logs.output))

    def test_drop_counter_samples_then_counts(self):
        from cybertrade.brokers.quotex.client import QuotexSocket

        sock = QuotexSocket("x")
        with self.assertLogs("cybertrade.qx.client", level="WARNING") as logs:
            for _ in range(6):
                sock._note_drop("unroutable binary", "sample")
        # first three sampled + one "counting silently" = 4 records
        self.assertEqual(len(logs.records), 4)
        self.assertEqual(sock.stats()["drops"], {"unroutable binary": 6})

    def test_dispatcher_sets_purse_confirmed(self):
        api = self._api()
        self.assertFalse(api._purse_confirmed.is_set())
        api._on_socket_event("s_account/change", [])
        self.assertTrue(api._purse_confirmed.is_set())

    def test_ensure_purse_confirms_and_reverifies(self):
        import threading

        api = self._api()
        threading.Timer(0.02, api._purse_confirmed.set).start()
        threading.Timer(0.05, api._data_seen.set).start()
        self.assertTrue(api.ensure_purse(timeout=2.0, reverify=2.0))
        wires = "\n".join(api.socket.sent)
        self.assertIn('"account/change"', wires)
        self.assertIn('"instruments/get"', wires)

    def test_ensure_purse_fails_when_switch_starves(self):
        import threading

        from cybertrade.exceptions import BrokerConnectionError

        api = self._api()
        threading.Timer(0.02, api._purse_confirmed.set).start()
        with self.assertRaises(BrokerConnectionError) as ctx:
            api.ensure_purse(timeout=1.0, reverify=0.05)
        self.assertIn("after the purse switch", str(ctx.exception))

    def test_connect_tail_sends_no_purse_switch(self):
        # The connect burst must stay pyquotex-identical: auth (at the
        # socket layer) + bootstrap + replays, no account/change —
        # ensure_purse owns that, after data is proven.
        api = self._api()
        api._bootstrap_session()
        api._replay_subscriptions()
        self.assertNotIn("account/change", "\n".join(api.socket.sent))


class TestCookieNames(unittest.TestCase):
    def test_names_only_deduped(self):
        from cybertrade.brokers.quotex.client import _cookie_names

        self.assertEqual(_cookie_names("ssid=ABC; __cf_bm=xyz; ssid=ABC"),
                         ["ssid", "__cf_bm"])
        self.assertEqual(_cookie_names(""), [])
        self.assertEqual(_cookie_names("  ; =v; k=v "), ["k"])


class TestSniff(unittest.TestCase):
    def _ws_frame(self, method, payload, opcode=1, t=0.5):
        return {"t": t, "method": method,
                "params": {"response": {"opcode": opcode,
                                        "payloadData": payload}}}

    def test_empty_capture_reports_plainly(self):
        from cybertrade.brokers.quotex.sniff import summarize

        report = summarize([])
        self.assertIn("no websocket or HTTP traffic", report)

    def test_report_groups_and_counts(self):
        from cybertrade.brokers.quotex.sniff import summarize

        batch = '[["EURUSD",1700000000,1.08,1],["EURUSD",1700000001,1.09,1]]'
        frames = [
            {"t": 0.0, "method": "Network.webSocketCreated",
             "params": {"url": "wss://ws2.qxbroker.com/socket.io/?EIO=3"}},
            self._ws_frame("Network.webSocketFrameSent",
                           '42["authorization",{"session":"S"}]', t=0.1),
            self._ws_frame("Network.webSocketFrameReceived",
                           '42["s_authorization",{}]', t=0.2),
            self._ws_frame("Network.webSocketFrameReceived", batch, t=0.3),
            self._ws_frame("Network.webSocketFrameReceived",
                           '451-["history/load",{"_placeholder":true}]', t=0.4),
            {"t": 0.5, "method": "Network.requestWillBeSent",
             "params": {"request": {"method": "GET",
                                    "url": "https://qxbroker.com/api/x"}}},
        ]
        report = summarize(frames)
        self.assertIn('C2S event "authorization"', report)
        self.assertIn('S2C event "s_authorization"', report)
        self.assertIn("S2C quote batch (2 rows)", report)
        self.assertIn("S2C binary placeholder", report)
        self.assertIn("GET https://qxbroker.com/api/x", report)
        self.assertIn("socket open wss://ws2.qxbroker.com", report)

    def test_binary_payloads_decode(self):
        import base64

        from cybertrade.brokers.quotex.sniff import _payload

        blob = base64.b64encode(b'[["A",1,2.0,1]]').decode()
        out = _payload({"response": {"opcode": 2, "payloadData": blob}})
        self.assertIn('"A"', out)

    def test_sniff_needs_a_page_target(self):
        from cybertrade.brokers.quotex.sniff import sniff_frames

        with self.assertRaises(RuntimeError) as ctx:
            sniff_frames(9333, duration=0.01,
                         fetch=lambda *a, **k: [])
        self.assertIn("no trade tab", str(ctx.exception))

    def test_collect_loop_reads_until_deadline(self):
        import json

        from cybertrade.brokers.quotex.sniff import sniff_frames

        events = [
            json.dumps({"method": "Network.webSocketFrameSent",
                        "params": {"response": {"opcode": 1,
                                                "payloadData": '42["tick"]'}}}),
            json.dumps({"method": "Network.someNoise", "params": {}}),
        ]

        class FakeWS:
            def __init__(self, url, **kw):
                self.sent = []

            def connect(self):
                pass

            def send(self, data):
                self.sent.append(data)

            def recv_text(self):
                if events:
                    return events.pop(0)
                raise TimeoutError("read timeout")

            def close(self):
                pass

        made = []

        def factory(url, **kw):
            ws = FakeWS(url, **kw)
            made.append(ws)
            return ws

        out = sniff_frames(
            9333, duration=0.05,
            fetch=lambda url, timeout=0: (
                [{"type": "page", "url": "https://qxbroker.com/en/trade",
                  "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/x"}]
                if url.endswith("/json/list") else {}),
            ws_factory=factory)
        self.assertEqual(len(out), 1)  # noise filtered, frame kept
        self.assertEqual(out[0]["method"], "Network.webSocketFrameSent")
        self.assertTrue(made[0].sent)  # Network.enable went out


class TestTlsProbe(unittest.TestCase):
    def test_script_order(self):
        from cybertrade.brokers.quotex.tlsprobe import script_frames

        wires = "\n".join(script_frames())
        self.assertEqual(len(script_frames()), 9)
        for name in ("indicator/list", "drawing/load", "pending/list",
                     "chart_notification/get", "instruments/get",
                     "instruments/update", "depth/follow", '"tick"'):
            self.assertIn(name, wires)
        self.assertLess(wires.index("instruments/get"),
                        wires.index("instruments/update"))

    def test_is_data_label(self):
        from cybertrade.brokers.quotex.tlsprobe import is_data_label

        for label in ("quote batch (3 rows)",
                      'event "history/load"',
                      'event "instruments/list"',
                      'event "balance"',
                      'event "candle-generated"',
                      "bare dict {demoBalance,liveBalance}",
                      "bare dict {asset,candles}"):
            with self.subTest(label=label):
                self.assertTrue(is_data_label(label))
        for label in ('event "s_authorization"',
                      "EIO open", "SIO connect", "EIO ping/pong",
                      "bare dict {foo}", "(empty)"):
            with self.subTest(label=label):
                self.assertFalse(is_data_label(label))

    def test_stdlib_leg_uses_fake_socket(self):
        from cybertrade.brokers.quotex.tlsprobe import run_stdlib

        sent = []

        class FakeSock:
            def __init__(self, ssid, **kw):
                self.on_event = kw.get("on_event")

            def connect(self, authorize=True):
                self.on_event("s_authorization", [])

            def send(self, wire):
                sent.append(wire)

            def disconnect(self):
                self.on_event("quotes", [[[["A", 1, 2.0, 1]]]])

        out = run_stdlib("S", "c=d", "ws://x", "https://x", "UA", True, 0.01,
                         socket_factory=FakeSock)
        self.assertTrue(out["connected"])
        self.assertTrue(out["authorized"])
        self.assertEqual(out["events"]["s_authorization"], 1)
        self.assertEqual(out["events"]["quotes"], 1)
        self.assertEqual(len(sent), 9)  # bootstrap + trio + tick
        self.assertTrue(sent[-1].endswith('["tick"]'))

    def test_chrome_leg_without_package(self):
        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("curl_cffi installed — live path untestable offline")
        from cybertrade.brokers.quotex.tlsprobe import run_chrome

        out = run_chrome("S", "", "ws://127.0.0.1:1/", "https://x", "UA",
                         True, 0.01)
        self.assertFalse(out["available"])
        self.assertIn("curl_cffi", out["error"])

    def test_verdicts(self):
        from cybertrade.brokers.quotex.tlsprobe import run_probe

        auth_only = {'event "s_authorization"': 1}
        flowing = {"quote batch (2 rows)": 9}
        cases = [
            (flowing, flowing, True, "both legs stream"),
            (auth_only, flowing, True, "TLS-GATED"),
            (auth_only, auth_only, True, "both legs starved"),
            (auth_only, {}, False, "install curl_cffi"),
        ]
        for std_events, chrome_events, available, needle in cases:
            with self.subTest(needle=needle):
                def factory(ssid, _ev=None, **kw):
                    _ev = dict(std_events) if _ev is None else _ev
                    class FakeSock:
                        def connect(self, authorize=True):
                            for name, n in _ev.items():
                                for _ in range(n):
                                    kw["on_event"](name, [])

                        def send(self, wire):
                            pass

                        def disconnect(self):
                            pass

                    return FakeSock()

                def chrome(*a, **k):
                    return {"connected": True, "authorized": True,
                            "events": dict(chrome_events), "error": "",
                            "available": available}

                report = run_probe("S", "", "ws://x", "https://x", "UA",
                                   True, 0.01, socket_factory=factory,
                                   chrome_runner=chrome)
                self.assertIn("stdlib (python TLS)", report)
                self.assertIn("chrome (curl_cffi)", report)
                self.assertIn(needle, report)

    def test_leg_report_data_line(self):
        from cybertrade.brokers.quotex.tlsprobe import _leg_report

        yes = {"connected": True, "authorized": True,
               "events": {"quote batch (1 rows)": 1}, "error": ""}
        no = {"connected": True, "authorized": True,
              "events": {'event "s_authorization"': 1}, "error": ""}
        self.assertIn("data      : YES", "\n".join(_leg_report("t", yes)))
        self.assertIn("data      : NO", "\n".join(_leg_report("t", no)))


if __name__ == "__main__":
    unittest.main()
