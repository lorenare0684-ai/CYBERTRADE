"""Order wire + lifecycle parity with the live venue (reference-client shapes).

Covers the regressions that made live orders silently fail:
- ``time`` on ``orders/open`` (TIMER = duration / TIME = aligned expiry)
- integer epoch ``requestId``
- ``orders/cancel`` + venue ticket for sell-back
- ``s_orders/open`` acks and ``deals`` settlements reaching the adapter
"""

from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timedelta, timezone

from cybertrade.brokers.quotex import constants as C
from cybertrade.brokers.quotex.adapter import QuotexBroker
from cybertrade.brokers.quotex.api import QuotexAPI
from cybertrade.brokers.quotex.client import QuotexSocket
from cybertrade.brokers.quotex.ghost import Pacekeeper
from cybertrade.brokers.quotex.models import QXOrderRequest, QXOrderResult
from cybertrade.brokers.quotex.protocol import (
    build_order,
    build_sell_option,
    build_settings_apply,
    expiration_for,
    make_request_id,
    parse_order_rows,
)
from cybertrade.constants import Side
from cybertrade.data.models import Order


def _reference_expiry(ts: int, duration: int) -> int:
    """Literal port of pyquotex ``get_expiration_time_quotex`` (UTC)."""
    now = datetime.fromtimestamp(ts, timezone.utc)
    if duration < 60:
        shift = 1 if now.second >= 30 else 0
        return int((now.replace(second=0, microsecond=0)
                    + timedelta(minutes=shift + 1)).timestamp())
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = int((now - midnight).total_seconds())
    remainder = since % duration
    step = 2 if remainder > (duration / 2) else 1
    return int((midnight + timedelta(seconds=((since // duration) + step) * duration)).timestamp())


def _frame(wire: str):
    return json.loads(wire[2:])


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


def _api(**kw) -> QuotexAPI:
    api = QuotexAPI(pace=Pacekeeper(enabled=False), **kw)
    api.socket = FakeSock()
    return api


class TestExpiry(unittest.TestCase):
    def test_matches_reference_grid(self):
        cases = [(1700000000, d) for d in C.DURATIONS]
        cases += [(1700000029, 5), (1700000030, 30), (1700003599, 3600),
                  (1700001799, 1800), (1700001801, 1800), (1699999999, 60)]
        for ts, d in cases:
            with self.subTest(ts=ts, d=d):
                self.assertEqual(expiration_for(ts, d), _reference_expiry(ts, d))

    def test_expiry_is_in_the_future_and_aligned(self):
        now = 1700000047
        for d in (60, 300, 900):
            exp = expiration_for(now, d)
            self.assertGreater(exp, now)
            self.assertEqual(exp % d, 0)


class TestRequestId(unittest.TestCase):
    def test_epoch_seconds_and_monotonic(self):
        import cybertrade.brokers.quotex.protocol as proto
        saved = proto._rid_last
        self.addCleanup(setattr, proto, "_rid_last", saved)
        proto._rid_last = 0
        a = make_request_id(1700000000.7)
        b = make_request_id(1700000000.9)
        self.assertTrue(a.isdigit() and b.isdigit())
        self.assertEqual(int(a), 1700000000)
        self.assertEqual(int(b), int(a) + 1)      # same second never collides
        c = make_request_id(1800000000)
        self.assertEqual(int(c), 1800000000)
        proto._rid_last = 0

    def test_numeric_request_id_rides_as_int(self):
        req = QXOrderRequest("EURUSD_otc", 5, "call", 60, request_id="1700000000")
        body = _frame(build_order(req))[1]
        self.assertIsInstance(body["requestId"], int)
        self.assertEqual(body["requestId"], 1700000000)
        # non-numeric ids (tests / legacy) stay strings
        req2 = QXOrderRequest("EURUSD_otc", 5, "call", 60, request_id="r1")
        self.assertEqual(_frame(build_order(req2))[1]["requestId"], "r1")


class TestOrderWire(unittest.TestCase):
    def test_timer_mode_default(self):
        api = _api()
        req = api.buy("EURUSD_otc", 5, "call", 60)
        order_wire = [w for w in api.socket.sent if '"orders/open"' in w][-1]
        name, body = _frame(order_wire)
        self.assertEqual(name, "orders/open")
        self.assertEqual(body["optionType"], C.OPTION_TYPE_TIMER)
        self.assertEqual(body["time"], 60)             # TIMER: duration, not a timestamp
        self.assertEqual(body["action"], "call")
        self.assertEqual(body["isDemo"], 1)
        self.assertEqual(body["tournamentId"], 0)
        self.assertIsInstance(body["requestId"], int)
        self.assertEqual(set(body), {"asset", "amount", "time", "action", "isDemo",
                                     "tournamentId", "requestId", "optionType"})
        self.assertAlmostEqual(req.expiry_ts, int(time.time()) + 60, delta=2)

    def test_time_mode_sends_aligned_expiry(self):
        api = _api(time_mode="TIME")
        before = int(time.time())
        req = api.buy("EURUSD", 10, "put", 300)
        _, body = _frame([w for w in api.socket.sent if '"orders/open"' in w][-1])
        self.assertEqual(body["optionType"], C.OPTION_TYPE_FAST)
        self.assertEqual(body["time"] % 300, 0)
        self.assertGreater(body["time"], before)
        self.assertNotEqual(body["time"], before + 300)   # the old, refused shape
        self.assertEqual(req.expiry_ts, float(body["time"]))
        # settings/apply carries the same expiry
        _, settings = _frame([w for w in api.socket.sent if '"settings/apply"' in w][0])
        self.assertEqual(settings["endTime"], body["time"])
        self.assertTrue(settings["settings"]["isFastOption"])

    def test_settings_apply_precedes_order(self):
        api = _api()
        api.buy("XAUUSD_otc", 2.5, "put", 30)
        names = [_frame(w)[0] for w in api.socket.sent]
        self.assertEqual(names, ["settings/apply", "orders/open"])
        _, settings = _frame(api.socket.sent[0])
        self.assertEqual(settings["settings"]["currentAsset"], {"symbol": "XAUUSD_otc"})
        self.assertEqual(settings["settings"]["timePeriod"], 30)
        self.assertEqual(settings["settings"]["dealValue"], 2.5)
        self.assertNotIn("endTime", settings)              # TIMER: no fixed end

    def test_settings_apply_shape(self):
        _, body = _frame(build_settings_apply("EURUSD_otc", 60, now=1700000000))
        self.assertEqual(body["chartId"], "graph")
        self.assertTrue(body["settings"]["isOneClickTrade"])
        self.assertEqual(body["settings"]["currentExpirationTime"], 1700000000)

    def test_sell_option_uses_venue_ticket(self):
        api = _api()
        req = api.buy("EURUSD_otc", 5, "call", 60)
        api.socket.sent.clear()
        # venue ack names the ticket for our requestId
        api._on_socket_event("s_orders/open", [{
            "id": 987654, "requestId": int(req.request_id), "asset": "EURUSD_otc",
            "amount": 5, "command": 0, "profit": 4.25, "percentProfit": 85,
            "openPrice": 1.1, "closePrice": 0,
        }])
        api.sell_option(req.request_id)                     # caller still holds requestId
        self.assertEqual(api.socket.sent[-1], '42["orders/cancel",{"ticket":987654}]')
        self.assertEqual(api.order_id_for(req.request_id), "987654")


class TestOrderLifecycleDispatch(unittest.TestCase):
    def _ack(self, req, order_id=555):
        return [{
            "id": order_id, "requestId": int(req.request_id), "asset": req.asset,
            "amount": req.amount, "command": 1 if req.action == "put" else 0,
            "profit": 4.25, "percentProfit": 85, "openPrice": 1.2345,
            "closePrice": 0, "openTimestamp": 1700000000,
            "closeTimestamp": 1700000060, "isDemo": 1,
        }]

    def test_s_orders_open_is_an_ack_not_a_win(self):
        api = _api()
        events = []
        api.add_listener(lambda k, p: events.append((k, p)))
        req = api.buy("EURUSD_otc", 5, "put", 60)
        api._on_socket_event("s_orders/open", self._ack(req))
        orders = [p for k, p in events if k == "order"]
        self.assertEqual(len(orders), 1)
        row = orders[0]
        self.assertEqual(row.order_id, "555")
        self.assertEqual(row.request_id, req.request_id)
        self.assertEqual(row.status, "open")               # potential profit != a win
        self.assertFalse(row.won)
        self.assertEqual(row.action, "put")
        self.assertAlmostEqual(row.payout, 0.85)
        self.assertEqual(row.expiry_ts, 1700000060.0)
        self.assertEqual(api.account_snapshot().open_positions, 1)
        self.assertIs(api.order_result(req.request_id), row)

    def test_deals_settle_by_venue_id(self):
        api = _api()
        events = []
        api.add_listener(lambda k, p: events.append((k, p)))
        req = api.buy("EURUSD_otc", 5, "call", 60)
        api._on_socket_event("s_orders/open", self._ack(req, 777))
        api._on_socket_event("deals", [{"deals": [
            {"id": 777, "profit": -5, "closePrice": 1.2, "closeTimestamp": 1700000060},
        ]}])
        final = api.order_result(req.request_id)
        self.assertEqual(final.status, "loss")
        self.assertTrue(final.lost)
        self.assertEqual(final.request_id, req.request_id)  # recovered from the ack
        self.assertEqual(final.asset, "EURUSD_otc")
        self.assertEqual(api.account_snapshot().open_positions, 0)
        api._on_socket_event("orders/closed", [[{"id": 777, "profit": 4.25, "closePrice": 1.3}]])
        self.assertTrue(api.order_result("777").won)

    def test_zero_profit_is_a_refund(self):
        rows = parse_order_rows([{"id": 1, "profit": 0, "closePrice": 1.1}], closed=True)
        self.assertEqual(rows[0].status, "refund")
        self.assertFalse(rows[0].won or rows[0].lost)

    def test_auth_ack_balance_is_absorbed(self):
        api = _api()
        api._on_socket_event("s_authorization", [{"liveBalance": 12.5, "demoBalance": 10000, "uid": 42}])
        self.assertTrue(api.wait_for_balance(0))
        self.assertAlmostEqual(api.balance.balance, 10000)
        self.assertEqual(api.session.user_id, "42")


class TestClientRoutesOrderPayloads(unittest.TestCase):
    def _sock(self):
        seen = []
        sock = QuotexSocket("ssid", on_event=lambda n, a: seen.append((n, a)))
        sock._authorized.set()
        return sock, seen

    def test_bare_deals_payload_routes(self):
        sock, seen = self._sock()
        self.assertTrue(sock._route_payload({"deals": [{"id": 1, "profit": -5}]}))
        self.assertEqual(seen[0][0], "deals")

    def test_bare_order_ack_routes(self):
        sock, seen = self._sock()
        self.assertTrue(sock._route_payload(
            {"id": 9, "requestId": 1700000000, "asset": "EURUSD", "amount": 5}))
        self.assertEqual(seen[0][0], "orders/opened")

    def test_placeholder_event_reaches_dispatcher(self):
        sock, seen = self._sock()
        sock._pending_bin = {"event": "s_orders/open", "need": 1, "got": []}
        sock._on_binary(b'{"id":9,"requestId":1700000000,"asset":"EURUSD","amount":5}')
        self.assertEqual(seen[0][0], "s_orders/open")
        self.assertEqual(seen[0][1][0]["id"], 9)


class TestAdapterLifecycle(unittest.TestCase):
    def _broker(self):
        api = _api()
        broker = QuotexBroker(api)
        api._last_tick["EURUSD_otc"] = type("T", (), {"price": 1.1})()
        return api, broker

    def _order(self):
        return Order(asset="EURUSD_otc", side=Side.CALL, amount=5.0,
                     expiry_seconds=60, payout=0.85, strategy="t")

    def test_ack_adopts_ticket_and_settlement_closes(self):
        api, broker = self._broker()
        fill = broker.submit(self._order())
        pos = broker.open_positions()[0]
        self.assertEqual(fill.broker_id, pos.fill.broker_id)
        rid = fill.broker_id
        self.assertTrue(rid.isdigit())
        # TIMER: local expiry == fill + 60
        self.assertAlmostEqual(pos.expiry_ts, fill.ts + 60, delta=1.5)
        api._on_socket_event("s_orders/open", [{
            "id": 31337, "requestId": int(rid), "asset": "EURUSD_otc", "amount": 5,
            "command": 0, "profit": 4.25, "percentProfit": 85, "openPrice": 1.1001,
            "closePrice": 0,
        }])
        self.assertEqual(broker.open_positions()[0].fill.broker_id, "31337")
        self.assertAlmostEqual(broker.open_positions()[0].fill.price, 1.1001)
        settled = []
        broker.add_listener(lambda k, p: settled.append((k, p)))
        api._on_socket_event("deals", [{"deals": [{"id": 31337, "profit": 4.25, "closePrice": 1.2}]}])
        self.assertEqual(broker.open_positions(), [])
        kinds = [k for k, _ in settled]
        self.assertIn("settle", kinds)
        settlement = [p for k, p in settled if k == "settle"][0]
        self.assertTrue(settlement.won)
        self.assertAlmostEqual(settlement.pnl, 5 * 0.85)

    def test_close_position_sells_with_venue_ticket(self):
        api, broker = self._broker()
        fill = broker.submit(self._order())
        api._on_socket_event("s_orders/open", [{
            "id": 4242, "requestId": int(fill.broker_id), "asset": "EURUSD_otc",
            "amount": 5, "command": 0, "profit": 4, "closePrice": 0,
        }])
        api.socket.sent.clear()
        pos_id = broker.open_positions()[0].id
        self.assertTrue(broker.close_position(pos_id))
        self.assertEqual(api.socket.sent[-1], '42["orders/cancel",{"ticket":4242}]')

    def test_loss_settlement(self):
        api, broker = self._broker()
        fill = broker.submit(self._order())
        api._on_socket_event("s_orders/open", [{
            "id": 1, "requestId": int(fill.broker_id), "asset": "EURUSD_otc",
            "amount": 5, "profit": 4, "closePrice": 0}])
        api._on_socket_event("deals", [{"deals": [{"id": 1, "profit": -5, "closePrice": 1.0}]}])
        self.assertEqual(broker.open_positions(), [])
        self.assertFalse(broker._settled[-1].won)
        self.assertAlmostEqual(broker._settled[-1].pnl, -5)


if __name__ == "__main__":
    unittest.main()
