"""Quotex socket client: session, heartbeats, reconnect, event dispatch.

Transport = stdlib WebSocket (RFC6455) + Engine.IO v3 / Socket.IO codec.
Everything about reconnection and heartbeat timing is centralized here so the
API layer can stay synchronous and boring.

Phase-30 ghost wire: the handshake carries browser-parity headers (the same
Chrome identity used for pairing — locale, no-cache; no faked capabilities),
and reconnect backoff is jittered so the pattern never lands on a fixed grid.
After a client-level reconnect the ``on_reconnected`` hook replays candle
subscriptions through the API layer.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...exceptions import BrokerAuthError, BrokerConnectionError, NetworkError
from ...network.socketio import (
    EngineIOSession,
    SocketPacket,
    encode_connect,
    encode_pong,
)
from ...network.websocket import WebSocketConnection
from ...utils import timex
from . import constants as C
from .ghost import parity_headers, reconnect_delay
from .protocol import parse_event

log = logging.getLogger("cybertrade.qx.client")

EventHandler = Callable[[str, List[Any]], None]


class QuotexSocket:
    """Owns the websocket + engine.io session for one Quotex connection."""

    def __init__(
        self,
        session_ssid: str,
        *,
        ws_url: str = C.WS_URL,
        cookies: str = "",
        user_agent: str = C.USER_AGENT,
        origin: str = C.ORIGIN,
        is_demo: bool = True,
        on_event: Optional[EventHandler] = None,
        on_reconnected: Optional[Callable[[], None]] = None,
        reconnect_max: int = 12,
        timeout: float = 15.0,
    ) -> None:
        self.session_ssid = session_ssid
        self.ws_url = ws_url
        self.cookies = cookies
        self.user_agent = user_agent
        self.origin = origin
        self.is_demo = is_demo
        self.on_event = on_event
        self.on_reconnected = on_reconnected
        self.reconnect_max = reconnect_max
        self.timeout = timeout

        self.conn: Optional[WebSocketConnection] = None
        self.eio = EngineIOSession()
        self._send_lock = threading.Lock()
        self._running = False
        self._reader: Optional[threading.Thread] = None
        self._pumper: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._authorized = threading.Event()
        self._last_pong = time.time()
        self.reconnects = 0
        self.messages_in = 0
        self.messages_out = 0
        self.last_error = ""

    # -- lifecycle ---------------------------------------------------------
    def connect(self, authorize: bool = True) -> None:
        """Open the socket, complete the engine handshake, authorize."""
        self._open_socket()
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="qx-read")
        self._reader.start()
        # engine.io open must arrive quickly
        deadline = time.time() + self.timeout
        while not self.eio.sid and time.time() < deadline:
            time.sleep(0.02)
        if not self.eio.sid:
            raise BrokerConnectionError("engine.io handshake timeout")
        self._raw_send(encode_connect())
        self._connected.set()
        if authorize:
            self.authorize()
        self._pumper = threading.Thread(target=self._heartbeat_loop, daemon=True, name="qx-beat")
        self._pumper.start()
        log.info("quotex socket connected sid=%s", self.eio.sid[:8])

    def _open_socket(self) -> None:
        # Browser-parity handshake: same identity as the paired Chrome tab.
        # Origin rides the dedicated parameter (the transport writes it
        # once); headers carry UA + locale + no-cache only — we never
        # advertise websocket extensions we do not implement.
        headers = parity_headers(self.user_agent)
        self.conn = WebSocketConnection(
            self.ws_url,
            headers=headers,
            cookie=self.cookies,
            origin=self.origin,
            timeout=self.timeout,
        )
        try:
            self.conn.connect()
        except (NetworkError, OSError) as exc:
            raise BrokerConnectionError(f"ws connect failed: {exc}") from exc

    def authorize(self, timeout: float = 10.0) -> None:
        from .protocol import build_authorization

        self._raw_send(build_authorization(self.session_ssid, self.is_demo))
        deadline = time.time() + timeout
        # authorization is confirmed by an event OR just by absence of error;
        # optimistically proceed after a short grace period.
        while time.time() < deadline:
            if self._authorized.is_set():
                return
            time.sleep(0.05)
        log.warning("authorization ack not observed within %.1fs — proceeding", timeout)

    def disconnect(self) -> None:
        self._running = False
        self._connected.clear()
        if self.conn is not None:
            self.conn.close()
        if self._reader:
            self._reader.join(timeout=2.0)
        if self._pumper:
            self._pumper.join(timeout=2.0)
        log.info("quotex socket disconnected")

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and self.conn is not None and not self.conn.closed

    # -- io ----------------------------------------------------------------
    def send(self, wire: str) -> None:
        self._raw_send(wire)

    def _raw_send(self, wire: str) -> None:
        if self.conn is None or self.conn.closed:
            raise BrokerConnectionError("socket not connected")
        with self._send_lock:
            try:
                self.conn.send(wire)
            except NetworkError as exc:
                self.last_error = str(exc)
                raise BrokerConnectionError(str(exc)) from exc
        self.messages_out += 1

    def _read_loop(self) -> None:
        while self._running and self.conn is not None and not self.conn.closed:
            try:
                raw = self.conn.recv_text()
            except NetworkError as exc:
                self.last_error = str(exc)
                self._connected.clear()
                if self._running:
                    self._try_reconnect()
                return
            self.messages_in += 1
            try:
                eng, sio = self.eio.on_raw(raw)
            except Exception as exc:  # noqa: BLE001 - tolerate schema drift
                self.last_error = f"parse: {exc}"
                log.debug("drop undecodable packet: %r", raw[:80])
                continue
            if eng is not None and eng.type == "2":  # engine ping -> pong
                try:
                    self._raw_send(encode_pong(eng.payload))
                except BrokerConnectionError:
                    return
                self._last_pong = time.time()
            if sio is None:
                continue
            if sio.type == "0":  # namespace connected
                self._connected.set()
                if sio.data and isinstance(sio.data, dict) and "sid" in sio.data:
                    pass
            if sio.type == "4":  # error frame
                from .protocol import parse_error

                self.last_error = parse_error(sio.data if isinstance(sio.data, list) else [sio.data])
                log.warning("venue error: %s", self.last_error)
            name, args = parse_event(sio)
            if name:
                if name in (C.EV_AUTH_SUCCESS, "authorization", "authorized"):
                    self._authorized.set()
                self._dispatch(name, args)

    def _dispatch(self, name: str, args: List[Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(name, args)
        except Exception:  # noqa: BLE001
            log.exception("event handler crashed name=%s", name)

    def _heartbeat_loop(self) -> None:
        """Engine.IO v3: server pings, client pongs (handled in read loop).

        Application-level watchdog: if nothing arrived for 2 ping intervals,
        nudge with an engine ping; if that fails, reconnect.
        """
        interval = self.eio.handshake.ping_interval if self.eio.handshake else 25.0
        while self._running:
            time.sleep(max(5.0, interval / 2.0))
            if not self._running:
                return
            silence = time.time() - self._last_pong
            if silence > interval * 2:
                log.warning("socket silent %.0fs — probing", silence)
                try:
                    self._raw_send("2")  # engine.io ping probe
                except BrokerConnectionError:
                    self._try_reconnect()
                    return
                self._last_pong = time.time()

    def _try_reconnect(self) -> None:
        """Jittered exponential backoff; replay hook fires on success."""
        for attempt in range(1, self.reconnect_max + 1):
            if not self._running:
                return
            delay = reconnect_delay(attempt)
            log.warning(
                "reconnect attempt %d/%d in %.1fs (jittered)",
                attempt, self.reconnect_max, delay,
            )
            time.sleep(delay)
            if not self._running:
                return
            try:
                self._open_socket()
                self.eio = EngineIOSession()
                self._authorized.clear()
                deadline = time.time() + self.timeout
                while not self.eio.sid and time.time() < deadline:
                    time.sleep(0.02)
                if not self.eio.sid:
                    continue
                self._raw_send(encode_connect())
                self.authorize(timeout=5.0)
                self._connected.set()
                self._last_pong = time.time()
                self.reconnects += 1
                self._reader = threading.Thread(
                    target=self._read_loop, daemon=True, name="qx-read"
                )
                self._reader.start()
                log.info("reconnected (attempt %d)", attempt)
                if self.on_reconnected is not None:
                    try:
                        self.on_reconnected()
                    except Exception:  # noqa: BLE001 — restore must not kill wire
                        log.exception("on_reconnected hook crashed")
                return
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                continue
        log.critical("reconnect budget exhausted — socket dead")
        self._running = False

    # -- stats -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "sid": self.eio.sid[:8] if self.eio.sid else "",
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "authorized": self._authorized.is_set(),
        }


__all__ = ["QuotexSocket"]
