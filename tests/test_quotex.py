"""Quotex integration tests: wire fixtures, catalog sync, history warm-start.

Everything runs offline against recorded/documented wire shapes distilled in
docs/QUOTEX_PROTOCOL.md — no sockets are opened.
"""

from __future__ import annotations

import unittest

from cybertrade.brokers.quotex.catalog import AssetCatalog
from cybertrade.brokers.quotex.models import QXAsset, QXCandle, QXOrderRequest
from cybertrade.brokers.quotex.protocol import (
    build_authorization,
    build_candle_history,
    build_change_balance,
    build_instruments,
    build_order,
    build_sell_option,
    parse_balance,
    parse_instruments,
    parse_order_result,
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
        self.assertIn('"changeBalance"', build_change_balance("REAL"))
        wire = build_instruments()
        self.assertIn('42["instrument"', wire)
        hist = build_candle_history("EURUSD_otc", 60, 100)
        self.assertIn("candleHistory", hist)
        self.assertIn("EURUSD_otc", hist)


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


if __name__ == "__main__":
    unittest.main()
