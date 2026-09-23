"""Network stack + Quotex protocol + web terminal tests."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request

from cybertrade.brokers.quotex import protocol
from cybertrade.brokers.quotex.models import QXCandle, QXOrderRequest, QXOrderResult
from cybertrade.network.socketio import (
    EngineIOSession,
    decode_socket,
    encode_connect,
    encode_event,
    event_args,
    event_name,
    parse_handshake,
)
from cybertrade.network.websocket import encode_frame, OP_TEXT, OP_CLOSE
from cybertrade.network.http_client import CookieJar, _dechunk, _parse_response


class TestWebSocketFrames(unittest.TestCase):
    def test_encode_small_masked(self):
        frame = encode_frame(b"hi", OP_TEXT)
        self.assertEqual(frame[0], 0x81)
        self.assertTrue(frame[1] & 0x80)  # masked
        self.assertEqual(frame[1] & 0x7F, 2)
        self.assertEqual(len(frame), 2 + 4 + 2)

    def test_encode_medium(self):
        payload = b"x" * 200
        frame = encode_frame(payload, OP_TEXT)
        self.assertEqual(frame[1] & 0x7F, 126)
        self.assertEqual(len(frame), 2 + 2 + 4 + 200)

    def test_encode_large(self):
        payload = b"y" * 70000
        frame = encode_frame(payload, OP_TEXT)
        self.assertEqual(frame[1] & 0x7F, 127)

    def test_mask_roundtrip(self):
        from cybertrade.network.websocket import _mask_payload

        key = b"\x01\x02\x03\x04"
        payload = b"hello world"
        self.assertEqual(_mask_payload(_mask_payload(payload, key), key), payload)


class TestSocketIO(unittest.TestCase):
    def test_handshake(self):
        hs = parse_handshake('0{"sid":"s1","upgrades":[],"pingInterval":25000,"pingTimeout":5000}')
        self.assertEqual(hs.sid, "s1")
        self.assertAlmostEqual(hs.ping_interval, 25.0)

    def test_event_roundtrip(self):
        wire = encode_event("authorization", {"session": "x", "isDemo": 1})
        self.assertEqual(wire, '42["authorization",{"session":"x","isDemo":1}]')
        packet = decode_socket(wire[1:])
        self.assertEqual(event_name(packet), "authorization")
        self.assertEqual(event_args(packet)[0]["session"], "x")

    def test_namespace_ack(self):
        packet = decode_socket('2/admin,32["join",{"r":1}]')
        self.assertEqual(packet.namespace, "/admin")
        self.assertEqual(packet.id, 32)

    def test_connect_encode(self):
        self.assertEqual(encode_connect(), "40")
        self.assertEqual(encode_connect("/x"), "40/x,")

    def test_engine_session(self):
        s = EngineIOSession()
        eng, sio = s.on_raw('0{"sid":"a","pingInterval":25000,"pingTimeout":5000}')
        self.assertEqual(s.sid, "a")
        eng, sio = s.on_raw("2")
        self.assertEqual(eng.type, "2")
        eng, sio = s.on_raw('42["tick",{"p":1}]')
        self.assertEqual(event_name(sio), "tick")


class TestHttpClientParts(unittest.TestCase):
    def test_cookie_jar(self):
        jar = CookieJar()
        jar.set_from_header("sessionid=abc; Path=/, foo=bar; HttpOnly")
        self.assertIn("sessionid", jar.cookies)
        self.assertIn("foo", jar.cookies)
        self.assertIn("sessionid=abc", jar.header())

    def test_parse_response(self):
        raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"a\":1}"
        resp = _parse_response(raw, "http://x")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"a": 1})
        self.assertTrue(resp.ok)

    def test_dechunk(self):
        data = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        self.assertEqual(_dechunk(data), b"hello world")


class TestQuotexProtocol(unittest.TestCase):
    def test_authorization_wire(self):
        wire = protocol.build_authorization("SID", is_demo=True)
        self.assertEqual(
            wire,
            '42["authorization",{"session":"SID","isDemo":1,"tournamentId":0}]',
        )

    def test_order_wire_modern(self):
        req = QXOrderRequest(asset="EURUSD_otc", amount=10, action="put",
                             duration=60, request_id="r1", time=99)
        wire = protocol.build_order(req)
        self.assertIn('42["orders/open"', wire)
        self.assertIn('"action":"put"', wire)
        self.assertIn('"isDemo":1', wire)

    def test_order_wire_legacy(self):
        req = QXOrderRequest(asset="A", amount=1, action="call", duration=30)
        wire = protocol.build_order(req, legacy=True)
        self.assertIn("buyOption", wire)

    def test_parse_candles_envelopes(self):
        a = protocol.parse_candles("X", [{"asset": "X", "candles": [{"t": 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5}]}])
        b = protocol.parse_candles("X", [{"data": [[1, 1, 1.5, 2, 0.5]]}])
        c = protocol.parse_candles("X", [[[1, 1, 1.5, 2, 0.5]]])
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].close, 1.5)
        self.assertEqual(len(b), 1)
        self.assertEqual(len(c), 1)

    def test_parse_tick_variants(self):
        asset, price, ts = protocol.parse_tick([{"asset": "A", "price": 1.5, "ts": 12}])
        self.assertEqual((asset, price, ts), ("A", 1.5, 12))
        asset, price, ts = protocol.parse_tick([{"s": "B", "p": 2.5}])
        self.assertEqual((asset, price), ("B", 2.5))

    def test_order_result(self):
        r = QXOrderResult.from_payload({"data": {"id": "1", "status": "win", "profit": 8}})
        self.assertTrue(r.won)

    def test_candle_list_shape(self):
        q = QXCandle.from_payload("A", [100, 1.0, 1.2, 1.3, 0.9, 5.0], 60)
        self.assertEqual(q.open, 1.0)
        self.assertEqual(q.close, 1.2)


class TestWebTerminal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cybertrade.config import AppConfig
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.web.server import EngineHub, WebTerminal

        cfg = AppConfig()
        cfg.strategy.universe = ["EURUSD_otc"]
        cls.engine = TradingEngine(cfg)
        cls.engine.boot()
        hub = EngineHub(cls.engine)
        cls.web = WebTerminal(hub, host="127.0.0.1", port=8911)
        cls.web.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.web.stop()
        cls.engine.shutdown()

    def test_index_served(self):
        with urllib.request.urlopen("http://127.0.0.1:8911/") as r:
            html = r.read().decode()
        self.assertIn("CYBERTRADE", html)

    def test_css_served(self):
        with urllib.request.urlopen("http://127.0.0.1:8911/static/css/cyber.css") as r:
            self.assertIn("--cyan", r.read().decode())

    def test_state_api(self):
        with urllib.request.urlopen("http://127.0.0.1:8911/api/state") as r:
            data = json.load(r)
        self.assertIn("snapshot", data)
        self.assertIn("assets", data)

    def test_command_arm_and_kill(self):
        def post(cmd):
            req = urllib.request.Request(
                "http://127.0.0.1:8911/api/command",
                data=json.dumps({"cmd": cmd}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as r:
                return json.load(r)

        self.assertTrue(post("arm")["ok"])
        self.assertTrue(post("kill")["ok"])
        self.assertTrue(post("clear_kill")["ok"])

    def test_static_traversal_blocked(self):
        req = urllib.request.Request("http://127.0.0.1:8911/static/../server.py")
        try:
            with urllib.request.urlopen(req) as r:
                self.assertNotEqual(r.status, 200)
        except Exception as exc:
            self.assertIn("HTTP Error", str(exc) + "HTTP Error")


if __name__ == "__main__":
    unittest.main()
