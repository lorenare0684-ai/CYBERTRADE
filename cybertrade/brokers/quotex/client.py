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
from .ghost import is_session_fault, parity_headers, reconnect_delay
from .protocol import build_authorization, parse_error, parse_event

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
        self.was_connected = False  # latch: a live wire self-heals, a new one reports
        self._authorized = threading.Event()
        self._auth_failed = ""
        self._auth_failed_event = threading.Event()
        self._reconnect_lock = threading.Lock()
        self._gen = 0  # connection generation: stale readers exit, never steal frames
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
        # engine.io open must arrive quickly; abort early if the reader
        # already died (a dead wire must not burn the whole timeout).
        deadline = time.time() + self.timeout
        while not self.eio.sid and time.time() < deadline:
            if self._reader is not None and not self._reader.is_alive():
                raise BrokerConnectionError(
                    f"engine.io handshake failed: {self.last_error or 'reader died'}"
                )
            time.sleep(0.02)
        if not self.eio.sid:
            raise BrokerConnectionError("engine.io handshake timeout")
        self._raw_send(encode_connect())
        self._connected.set()
        self.was_connected = True
        if authorize:
            self.authorize()
        self._pumper = threading.Thread(target=self._heartbeat_loop, daemon=True, name="qx-beat")
        self._pumper.start()
        log.info("quotex socket connected sid=%s", self.eio.sid[:8])

    def _open_socket(self) -> None:
        # A reconnect replaces the wire: retire the old connection first so
        # no stale reader keeps a dead socket (or steals the new one's
        # frames) — the generation counter below is the second half of that.
        old, self.conn = self.conn, None
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass
        # Browser-parity handshake: same identity as the paired Chrome tab.
        # Origin rides the dedicated parameter (the transport writes it
        # once); headers carry UA + locale + no-cache only — we never
        # advertise websocket extensions we do not implement.
        headers = parity_headers(self.user_agent)
        conn = WebSocketConnection(
            self.ws_url,
            headers=headers,
            cookie=self.cookies,
            origin=self.origin,
            timeout=self.timeout,
        )
        try:
            conn.connect()
        except (NetworkError, OSError) as exc:
            raise BrokerConnectionError(f"ws connect failed: {exc}") from exc
        self.conn = conn

    def authorize(self, timeout: float = 10.0) -> None:
        """Send the session frame; fail fast when the venue rejects it.

        The venue usually confirms authorization implicitly (balance and
        data start flowing) rather than with an explicit ack, so a quiet
        window still proceeds — but an explicit session fault
        (``invalid session`` / ``unauthorized`` / …) raises
        :class:`BrokerAuthError` instead of masquerading as a live login.
        """
        self._auth_failed = ""
        self._auth_failed_event.clear()
        self._raw_send(build_authorization(self.session_ssid, self.is_demo))
        deadline = time.time() + timeout
        # authorization is confirmed by an event OR just by absence of error;
        # optimistically proceed after a short grace period.
        while time.time() < deadline:
            self._raise_if_auth_failed()
            if self._authorized.is_set():
                return
            time.sleep(0.05)
        self._raise_if_auth_failed()
        log.warning("authorization ack not observed within %.1fs — proceeding", timeout)

    def _raise_if_auth_failed(self) -> None:
        if self._auth_failed_event.is_set():
            detail = self._auth_failed or "unauthorized"
            raise BrokerAuthError(
                f"venue rejected the session ({detail}) — "
                "re-pair via `cybertrade quotex login` (Chrome opens; "
                "log in and solve the CAPTCHA once)"
            )

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
        gen = self._gen
        while self._running and gen == self._gen:
            conn = self.conn
            if conn is None or conn.closed:
                return
            try:
                raw = conn.recv_text()
            except NetworkError as exc:
                if gen != self._gen:
                    return  # superseded by a reconnect — exit quietly
                self.last_error = str(exc)
                self._connected.clear()
                # Only a *live* wire self-heals. During the initial
                # handshake the waiter above owns the failure (and the
                # API layer owns host fallback) — healing here would fork
                # a second connection behind connect()'s back.
                if self._running and self.was_connected:
                    self._try_reconnect_guarded()
                return
            self.messages_in += 1
            self._last_pong = time.time()  # any traffic proves liveness
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
            if sio is None:
                continue
            if sio.type == "0":  # namespace connected
                self._connected.set()
                self.was_connected = True
                if sio.data and isinstance(sio.data, dict) and "sid" in sio.data:
                    pass
            if sio.type == "4":  # error frame
                payload = sio.data if isinstance(sio.data, list) else [sio.data]
                self.last_error = parse_error(payload)
                log.warning("venue error: %s", self.last_error)
                # Error frames used to die here silently, so a rejected
                # session looked exactly like a slow one. Surface them to
                # the API layer (session-fault detection lives there) and
                # wake authorize() when the session itself is at fault.
                self._note_session_fault(self.last_error)
                self._dispatch("error", payload)
                continue
            try:
                name, args = parse_event(sio)
            except Exception as exc:  # noqa: BLE001 — a bad frame is not fatal
                self.last_error = f"parse: {exc}"
                continue
            if name:
                try:
                    if name in (C.EV_AUTHORIZATION, C.EV_AUTH_SUCCESS, "authorized"):
                        self._authorized.set()
                    elif name in (C.SV_ERROR, "error") and not self._authorized.is_set():
                        self._note_session_fault(parse_error(args))
                    elif not self._authorized.is_set():
                        # Implicit authorization: the venue answered the
                        # session frame with data instead of an error, which
                        # is how it usually confirms (no explicit ack).
                        self._authorized.set()
                except Exception:  # noqa: BLE001 — bookkeeping never kills the wire
                    log.debug("auth bookkeeping failed", exc_info=True)
                self._dispatch(name, args)

    def _note_session_fault(self, message: str) -> None:
        """Record a venue error that condemns the session pre-authorization."""
        if not self._authorized.is_set() and is_session_fault(message or ""):
            self._auth_failed = message or "unauthorized"
            self._auth_failed_event.set()

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
        while self._running:
            # Re-read every lap: a reconnect replaces the handshake.
            interval = self.eio.handshake.ping_interval if self.eio.handshake else 25.0
            time.sleep(max(5.0, interval / 2.0))
            if not self._running:
                return
            silence = time.time() - self._last_pong
            if silence > interval * 2:
                log.warning("socket silent %.0fs — probing", silence)
                try:
                    self._raw_send("2")  # engine.io ping probe
                except BrokerConnectionError:
                    self._try_reconnect_guarded()
                    return
                self._last_pong = time.time()

    def _try_reconnect_guarded(self) -> None:
        """Single-owner entry: reader and watchdog race here on a drop."""
        if not self._reconnect_lock.acquire(blocking=False):
            return  # another thread already owns the reconnect loop
        try:
            self._try_reconnect()
        finally:
            try:
                self._reconnect_lock.release()
            except RuntimeError:  # pragma: no cover — defensive
                pass

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
                self._connected.clear()
                # The reader is what fills eio.sid from the handshake, so it
                # must be running BEFORE we wait for it (same order as
                # connect()). Waiting first deadlocked every reconnect.
                self._gen += 1
                self._reader = threading.Thread(
                    target=self._read_loop, daemon=True, name="qx-read"
                )
                self._reader.start()
                deadline = time.time() + self.timeout
                while not self.eio.sid and time.time() < deadline:
                    if not self._running:
                        return
                    if self._reader is not None and not self._reader.is_alive():
                        break  # wire died mid-handshake — next attempt, no wait
                    time.sleep(0.02)
                if not self.eio.sid:
                    continue
                self._raw_send(encode_connect())
                self.authorize(timeout=5.0)
                self._connected.set()
                self.was_connected = True
                self._last_pong = time.time()
                self.reconnects += 1
                log.info("reconnected (attempt %d)", attempt)
                if self.on_reconnected is not None:
                    try:
                        self.on_reconnected()
                    except Exception:  # noqa: BLE001 — restore must not kill wire
                        log.exception("on_reconnected hook crashed")
                return
            except BrokerAuthError as exc:
                # A dead session never heals by retrying — a human must
                # re-pair it. Burning the backoff budget here only delays
                # that message by minutes.
                self.last_error = str(exc)
                log.critical("reconnect aborted: %s", exc)
                self._running = False
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
            "auth_error": self._auth_failed,
        }


__all__ = ["QuotexSocket"]
