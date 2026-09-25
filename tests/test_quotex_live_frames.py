"""Regression: the live venue's binary attachments (captured Sep-2026).

Engine.IO v3 prefixes every websocket binary frame with the packet-type
byte (``0x04``).  The decoder used to strip it only on the base64 path,
so every raw attachment — quotes, instruments, balance — was dropped at
DEBUG level and a healthy session read as "authorized but starving".
Frames below are verbatim from a trade-tab sniff.
"""

from __future__ import annotations

import json
import unittest

from cybertrade.brokers.quotex.api import QuotexAPI
from cybertrade.brokers.quotex.client import QuotexSocket, _decode_binary_json
from cybertrade.brokers.quotex.ghost import Pacekeeper
from cybertrade.exceptions import NetworkError
from cybertrade.network.websocket import OP_BINARY, OP_TEXT


class TestAttachmentDecode(unittest.TestCase):
    def test_raw_with_eio_type_byte(self):
        self.assertEqual(_decode_binary_json(b'\x04[["EURGBP",1790332550.026,0.86053,1]]'),
                         [["EURGBP", 1790332550.026, 0.86053, 1]])
        self.assertEqual(_decode_binary_json(b'\x04{"liveBalance":0,"demoBalance":1e4}'),
                         {"liveBalance": 0, "demoBalance": 10000.0})

    def test_raw_without_prefix_and_base64(self):
        self.assertEqual(_decode_binary_json(b'[["EURGBP",4]]'), [["EURGBP", 4]])
        import base64
        b64 = base64.b64encode(b'\x04[["EURGBP",4]]')
        self.assertEqual(_decode_binary_json(b64), [["EURGBP", 4]])

    def test_garbage_is_miss_not_crash(self):
        from cybertrade.brokers.quotex.client import _MISS
        self.assertIs(_decode_binary_json(b'\x04\xff\xfe'), _MISS)
        self.assertIs(_decode_binary_json(b'\x04'), _MISS)


def _run(frames, api=None):
    api = api or QuotexAPI(pace=Pacekeeper(enabled=False))
    sock = QuotexSocket("ssid", on_event=api._on_socket_event)
    sent = []

    class Conn:
        closed = False

        def __init__(self):
            self.q = list(frames)

        def recv_message(self):
            if not self.q:
                sock._running = False
                self.closed = True
                raise NetworkError("eof")
            return self.q.pop(0)

        def send(self, wire):
            sent.append(wire)

    sock.conn = Conn()
    sock._running = True
    sock._read_loop()
    return api, sock, sent


OPEN = (OP_TEXT, b'0{"sid":"abc","upgrades":[],"pingInterval":25000,"pingTimeout":5000}')


class TestSniffedWire(unittest.TestCase):
    def test_trade_tab_sequence_feeds_the_api(self):
        row = [1, "EURUSD", "EUR/USD", "currency", 0, 85, 0, 0, 0, 0, 0, 0, 0, 0,
               1, 0, 0, 0, 80, 0, 0, 0, 0, 85, 85, 0, 0]
        frames = [
            OPEN, (OP_TEXT, b"40"),
            (OP_TEXT, b'451-["s_authorization",{"_placeholder":true,"num":0}]'),
            (OP_BINARY, b'\x04{"liveBalance":0,"demoBalance":10000,"uid":7}'),
            (OP_TEXT, b'451-["quotes/stream",{"_placeholder":true,"num":0}]'),
            (OP_BINARY, b'\x04[["EURGBP",1790332550.026,0.86053,1]]'),
            (OP_TEXT, b'451-["depth/change",{"_placeholder":true,"num":0}]'),
            (OP_BINARY, b'\x04[["EURGBP",4]]'),
            (OP_TEXT, b'451-["instruments/list",{"_placeholder":true,"num":0}]'),
            (OP_BINARY, b"\x04" + json.dumps([row]).encode()),
        ]
        api, sock, _ = _run(frames)
        self.assertTrue(api._data_seen.is_set())
        self.assertTrue(api._balance_seen.is_set())
        self.assertAlmostEqual(api.balance.balance, 10000)
        self.assertEqual(api.session.user_id, "7")
        tick = api._last_tick["EURGBP"]
        self.assertAlmostEqual(tick.price, 0.86053)
        self.assertEqual(int(tick.ts), 1790332550)
        self.assertIn("EURUSD", api.assets)
        self.assertEqual(sock.stats()["drops"], {})
        self.assertEqual(sock.stats()["stream_events"][:4],
                         ["s_authorization", "quotes/stream", "depth/change", "instruments/list"])

    def test_undecodable_attachment_is_loud(self):
        frames = [OPEN, (OP_TEXT, b"40"),
                  (OP_TEXT, b'451-["quotes/stream",{"_placeholder":true,"num":0}]'),
                  (OP_BINARY, b"\x04\xff\xfe\x00")]
        with self.assertLogs("cybertrade.qx.client", level="WARNING") as cm:
            _, sock, _ = _run(frames)
        self.assertIn("undecodable binary attachment", "".join(cm.output))
        self.assertEqual(sock.stats()["drops"], {"undecodable binary attachment": 1})


class TestNoClientConnectPacket(unittest.TestCase):
    def test_connect_never_sends_40(self):
        import threading
        import time

        api = QuotexAPI(pace=Pacekeeper(enabled=False))
        sock = QuotexSocket("ssid", on_event=api._on_socket_event, timeout=2.0)
        sent = []
        gate = threading.Event()

        class Conn:
            closed = False

            def __init__(self):
                self.q = [OPEN, (OP_TEXT, b"40"),
                          (OP_TEXT, b'42["s_authorization",{}]')]

            def recv_message(self):
                if self.q:
                    return self.q.pop(0)
                gate.wait(0.05)
                from cybertrade.exceptions import RecvTimeoutError
                raise RecvTimeoutError("idle")

            def send(self, wire):
                sent.append(wire)

            def close(self):
                self.closed = True

        sock._open_socket = lambda: setattr(sock, "conn", Conn())  # type: ignore[assignment]
        sock.connect()
        try:
            self.assertNotIn("40", sent)
            self.assertTrue(any(w.startswith('42["authorization"') for w in sent))
            self.assertTrue(sock.connected)
        finally:
            sock.disconnect()
            gate.set()


if __name__ == "__main__":
    unittest.main()


class TestLiveBalanceEvents(unittest.TestCase):
    """``s_balance/list`` / ``settings/list`` (live Sep-2026) carry the purse
    balance and the account id — they used to fall into the generic
    ``s_*`` confirm branch, booting the engine with balance 0 and no user id
    (RECOVERY HOLD)."""

    def _api(self):
        return QuotexAPI(pace=Pacekeeper(enabled=False))

    def test_s_balance_list_dict(self):
        api = self._api()
        api._on_socket_event("s_balance/list", [{"liveBalance": 0, "demoBalance": 10000, "uid": 42}])
        self.assertAlmostEqual(api.balance.balance, 10000)
        self.assertEqual(api.session.user_id, "42")
        self.assertTrue(api._balance_seen.is_set())

    def test_s_balance_list_rows_pick_active_purse(self):
        api = self._api()
        api._on_socket_event("s_balance/list", [[{"isDemo": 0, "balance": 3.5},
                                                 {"isDemo": 1, "balance": 9950.5}]])
        self.assertAlmostEqual(api.balance.balance, 9950.5)
        api.demo = False
        api._on_socket_event("s_balance/list", [[{"isDemo": 0, "balance": 3.5},
                                                 {"isDemo": 1, "balance": 9950.5}]])
        self.assertAlmostEqual(api.balance.balance, 3.5)

    def test_settings_list_profile_gives_identity(self):
        api = self._api()
        api._on_socket_event("settings/list", [{"data": {"id": 777, "nickname": "n",
                                                          "demoBalance": 500, "liveBalance": 0,
                                                          "currencyCode": "USD"}}])
        self.assertEqual(api.session.user_id, "777")
        self.assertAlmostEqual(api.balance.balance, 500)

    def test_orders_lists_fold_into_state(self):
        api = self._api()
        api._on_socket_event("orders/opened/list", [[{"id": 1, "asset": "EURUSD_otc", "amount": 5, "profit": 4}]])
        api._on_socket_event("orders/closed/list", [[{"id": 2, "asset": "EURUSD_otc", "amount": 5, "profit": -5}]])
        self.assertEqual(api.order_result("1").status, "open")
        self.assertEqual(api.order_result("2").status, "loss")
        self.assertEqual(api.account_snapshot().open_positions, 1)
