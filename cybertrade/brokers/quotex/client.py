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

import base64
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...exceptions import (
    BrokerAuthError,
    BrokerConnectionError,
    NetworkError,
    RecvTimeoutError,
)
from ...network.socketio import (
    EngineIOSession,
    SocketPacket,
    encode_pong,
)
from ...network.websocket import OP_BINARY, WebSocketConnection
from ...utils import timex
from ...utils.jsonx import loads
from . import constants as C
from .ghost import is_session_fault, parity_headers, reconnect_delay
from .protocol import (
    build_authorization,
    build_tick,
    is_placeholder,
    parse_error,
    parse_event,
)

log = logging.getLogger("cybertrade.qx.client")

EventHandler = Callable[[str, List[Any]], None]

_MISS: Any = object()


_EIO_TYPE_BYTES = frozenset(range(0, 7))  # engine.io packet types 0..6


def _decode_binary_json(payload: bytes) -> Any:
    """JSON from an Engine.IO v3 binary attachment (raw or base64-wrapped).

    Over websocket, Engine.IO v3 prefixes every binary frame with a single
    packet-type byte (``0x04`` = message) before the Socket.IO attachment,
    so the wire carries ``\x04[["EURGBP",…]]`` / ``\x04{"liveBalance":…}``.
    That byte must be stripped before parsing — with it in place every
    attachment (quotes, instruments, balance) fails to decode, and the
    session looks authorized-but-starving.
    """
    raw = bytes(payload)
    if raw[:1] and raw[0] in _EIO_TYPE_BYTES and raw[1:2] in (b"[", b"{", b'"'):
        raw = raw[1:]
    try:
        text = raw.decode("utf-8")
    except ValueError:
        return _MISS
    obj = loads(text, default=_MISS)
    if obj is not _MISS:
        return obj
    # Some transports base64 the attachment ("BFtb…" == "\\x04[[…" — a
    # quote batch; "\\x04{" would be a dict payload instead).
    try:
        b64 = base64.b64decode(text.strip(), validate=True)
        if b64[:1] and b64[0] in _EIO_TYPE_BYTES:
            b64 = b64[1:]
        return loads(b64.decode("utf-8"), default=_MISS)
    except Exception:  # noqa: BLE001 — undecodable is routine
        return _MISS


def _cookie_names(header: str) -> List[str]:
    """Cookie *names* from a header value — values never leave the wire."""
    names: List[str] = []
    for part in (header or "").split(";"):
        name = part.split("=", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def _classify_silence(silence: float, interval: float,
                      probe_age: Optional[float]) -> str:
    """Pure watchdog policy: ``ok`` | ``probe`` | ``dead``.

    Past two quiet ping intervals the wire earns an engine ping probe; a
    probe unanswered for a full interval (floor 10s) condemns the wire.
    """
    if silence <= interval * 2:
        return "ok"
    if probe_age is None:
        return "probe"
    if probe_age > max(10.0, interval):
        return "dead"
    return "ok"  # probe outstanding, inside its grace window


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
        self._pending_bin: Optional[Dict[str, Any]] = None  # placeholder → attachments
        self._drop_counts: Dict[str, int] = {}  # venue frames dropped, by cause
        self._probe_at: Optional[float] = None  # engine probe outstanding since
        self._next_tick = 0.0  # next 42["tick"] heartbeat due
        self._stream_names: List[str] = []  # first venue events (field diagnosis)
        self.reconnects = 0
        self.messages_in = 0
        self.messages_out = 0
        self.last_error = ""

    # -- lifecycle ---------------------------------------------------------
    def connect(self, authorize: bool = True) -> None:
        """Open the socket, complete the engine handshake, authorize."""
        self._open_socket()
        # Fresh wire, fresh state: retire any lingering reader from a
        # previous life, and forget its auth/binary bookkeeping so a stale
        # placeholder can never eat the new wire's first attachment.
        self._gen += 1
        self._authorized.clear()
        self._pending_bin = None
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
        # No ``40`` from us: with EIO=3 the server auto-connects the
        # default namespace (socket.io-client v2 and pyquotex never send
        # it); a client CONNECT adds a *second* server-side socket on the
        # same transport — a structural difference from the trade tab.
        self._connected.set()
        if authorize:
            self.authorize()
        self._pumper = threading.Thread(target=self._heartbeat_loop, daemon=True, name="qx-beat")
        self._pumper.start()
        # Only NOW is the wire live: a reader death before this point must
        # surface through connect()'s waiter (and the API's host fallback),
        # never fork a background reconnect behind connect()'s back.
        self.was_connected = True
        self._probe_at = None
        self._next_tick = 0.0  # heartbeat on the first pumper lap
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
        # Referer truthfully names the page this session was paired on —
        # browsers (and pyquotex) send it on the WS upgrade.
        headers = parity_headers(self.user_agent,
                                 referer=f"{self.origin}/en/trade")
        conn = WebSocketConnection(
            self.ws_url,
            headers=headers,
            cookie=self.cookies,
            origin=self.origin,
            timeout=self.timeout,
        )
        log.info("ws handshake cookies: %s",
                 ", ".join(_cookie_names(self.cookies)) or "(none sent)")
        try:
            conn.connect()
        except (NetworkError, OSError) as exc:
            raise BrokerConnectionError(f"ws connect failed: {exc}") from exc
        self.conn = conn

    def authorize(self, timeout: float = 10.0) -> None:
        """Send the session frame; fail fast when the venue rejects it.

        The venue confirms with ``s_authorization`` (or implicitly, with
        data instead of an error), so a quiet window still proceeds — but
        an explicit session fault (``authorization/reject`` / ``invalid
        session`` / ``unauthorized`` / …) raises :class:`BrokerAuthError`
        instead of masquerading as a live login.
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
            if not self._connected.is_set() and (
                self._reader is None or not self._reader.is_alive()
            ):
                raise BrokerConnectionError(
                    f"wire died during authorization: {self.last_error or 'reader died'}"
                )
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
                opcode, payload = conn.recv_message()
            except RecvTimeoutError:
                # Quiet wire, not a dead one — keep waiting. The pumper's
                # watchdog owns the dead-or-alive decision; treating an
                # idle read window as a drop flapped every healthy-but-
                # quiet connection (the venue pings slower than we read).
                if gen != self._gen or not self._running:
                    return
                continue
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
            if opcode == OP_BINARY:
                self._on_binary(bytes(payload))
                continue
            raw = bytes(payload).decode("utf-8", errors="replace")
            try:
                eng, sio = self.eio.on_raw(raw)
            except Exception as exc:  # noqa: BLE001 - tolerate schema drift
                if self._handle_unframed(raw):
                    continue
                self.last_error = f"parse: {exc}"
                self._note_drop("undecodable frame", f"{raw[:160]} ({exc})")
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
            if sio.type == "4":  # error frame
                err_payload = sio.data if isinstance(sio.data, list) else [sio.data]
                self.last_error = parse_error(err_payload)
                log.warning("venue error: %s", self.last_error)
                # Error frames used to die here silently, so a rejected
                # session looked exactly like a slow one. Surface them to
                # the API layer (session-fault detection lives there) and
                # wake authorize() when the session itself is at fault.
                self._note_session_fault(self.last_error)
                self._dispatch("error", err_payload)
                continue
            try:
                name, args = parse_event(sio)
            except Exception as exc:  # noqa: BLE001 — a bad frame is not fatal
                self.last_error = f"parse: {exc}"
                self._note_drop("unparsable event", f"{raw[:160]} ({exc})")
                continue
            if sio.type == "5":  # binary event: placeholder, attachments follow
                need = is_placeholder(args)
                if need is not None:
                    if self._pending_bin is not None:
                        self._note_drop("stale binary placeholder",
                                        str(self._pending_bin.get("event")))
                    self._pending_bin = {"event": name or "binary",
                                         "need": need, "got": []}
                    continue
            if name:
                self._got_event(name, args)

    def _note_drop(self, kind: str, sample: str) -> None:
        """Count a dropped venue frame; warn (sampled) so silence is visible.

        The venue's framing drifts without notice — when it does, the old
        code dropped the evidence at debug level and the wire just looked
        dead.  The first three drops of each kind log with a sample, then
        only the counter grows (see ``stats()["drops"]``).
        """
        n = self._drop_counts.get(kind, 0) + 1
        self._drop_counts[kind] = n
        if n <= 3:
            log.warning("venue %s dropped (#%d): %.160s", kind, n, sample)
        elif n == 4:
            log.warning("venue %s drops repeat — counting silently", kind)

    def _note_session_fault(self, message: str) -> None:
        """Record a venue error that condemns the session pre-authorization."""
        if not self._authorized.is_set() and is_session_fault(message or ""):
            self._auth_failed = message or "unauthorized"
            self._auth_failed_event.set()

    def _got_event(self, name: str, args: List[Any]) -> None:
        """Auth bookkeeping + first-frames log + dispatch, one choke point.

        Every venue event — Socket.IO text, binary attachment, or bare
        quote batch — lands here so authorization state can never depend
        on which framing the venue chose.
        """
        try:
            if name == C.SV_S_AUTHORIZATION:
                self._authorized.set()
            elif name == C.SV_AUTH_REJECT:
                # Fail fast AND loud: wake authorize() with BrokerAuthError
                # and mark the session stale at the API layer.
                self._note_session_fault("authorization/reject")
                self._dispatch("error", ["authorization/reject", *args])
                return
            elif name in (C.SV_ERROR, "error") and not self._authorized.is_set():
                self._note_session_fault(parse_error(args))
            elif not self._authorized.is_set():
                # Implicit authorization: the venue answered the session
                # frame with data instead of an error.
                self._authorized.set()
        except Exception:  # noqa: BLE001 — bookkeeping never kills the wire
            log.debug("auth bookkeeping failed", exc_info=True)
        if name not in self._stream_names and len(self._stream_names) < 6:
            self._stream_names.append(name)
            log.info("venue stream: %s", name)
        self._dispatch(name, args)

    def _on_binary(self, payload: bytes) -> None:
        """Route a binary websocket frame (Socket.IO attachments)."""
        obj = _decode_binary_json(payload)
        if obj is _MISS:
            self._note_drop("undecodable binary attachment",
                            f"{len(payload)}B {bytes(payload[:48])!r}")
            return
        pend = self._pending_bin
        if pend is not None:
            pend["got"].append(obj)
            if len(pend["got"]) >= int(pend["need"]):
                self._pending_bin = None
                self._got_event(str(pend["event"]), list(pend["got"]))
            return
        # Unsolicited binary (the venue sometimes pushes batches bare).
        if not self._route_payload(obj):
            self._note_drop("unroutable binary", repr(payload[:80]))

    def _handle_unframed(self, raw: str) -> bool:
        """Route a frame outside Engine.IO framing.

        Bare quote batches arrive as raw JSON; some transports base64-wrap
        binary attachments into text frames ("BFtb…") — try both.
        """
        text = raw.strip()
        if not text:
            return False
        if text[0] in "[{":
            obj = loads(text, default=_MISS)
            return obj is not _MISS and self._route_payload(obj)
        obj = _decode_binary_json(text.encode("utf-8", errors="replace"))
        return obj is not _MISS and self._route_payload(obj)

    def _route_payload(self, obj: Any) -> bool:
        """Heuristic routing for payload-shaped frames. True when routed."""
        if isinstance(obj, list):
            rows = obj if obj and all(isinstance(r, (list, tuple)) for r in obj) else [obj]
            if all(len(r) >= 3 and isinstance(r[0], str)
                   and isinstance(r[1], (int, float))
                   and isinstance(r[2], (int, float)) for r in rows):
                self._got_event(C.SV_QUOTES, [obj])
                return True
            return False
        if isinstance(obj, dict):
            if "liveBalance" in obj or "demoBalance" in obj:
                self._got_event(C.SV_BALANCE, [obj])
                return True
            if obj.get("asset") and any(k in obj for k in ("candles", "data", "history", "list")):
                self._got_event(C.SV_CANDLES, [obj])
                return True
            if isinstance(obj.get("deals"), list):
                # settlement push: closed contracts with realised profit
                self._got_event(C.SV_DEALS, [obj])
                return True
            if "id" in obj and ("requestId" in obj or "asset" in obj) and "amount" in obj:
                # bare order confirmation (the ack sometimes arrives
                # without its placeholder): venue id + echoed requestId
                self._got_event(C.SV_ORDERS_OPENED, [obj])
                return True
            return False
        return False

    def _dispatch(self, name: str, args: List[Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(name, args)
        except Exception:  # noqa: BLE001
            log.exception("event handler crashed name=%s", name)

    def _heartbeat_loop(self) -> None:
        """Engine.IO pongs ride the read loop; this pumper owns the rest:

        - the application heartbeat (the venue expects ``42["tick"]`` ~5s),
        - the silence watchdog (probe, then reconnect when unanswered).

        It persists across reconnects — triggering one never stops it.
        """
        while self._running:
            try:
                sleep_for = self._pump_once(time.time())
            except Exception:  # noqa: BLE001 — the pumper never dies loud
                log.debug("heartbeat lap failed", exc_info=True)
                sleep_for = 5.0
            time.sleep(max(1.0, min(sleep_for, 10.0)))

    def _pump_once(self, now: float) -> float:
        """One watchdog/heartbeat lap; returns seconds until the next."""
        if self._reconnect_lock.locked():
            return 5.0  # recovery already owns the wire — stay quiet
        # Re-read every lap: a reconnect replaces the handshake.
        interval = self.eio.handshake.ping_interval if self.eio.handshake else 25.0
        silence = now - self._last_pong
        if silence < interval:
            self._probe_at = None  # traffic (or a fresh wire) clears probes
        probe_age = None if self._probe_at is None else now - self._probe_at
        action = _classify_silence(silence, interval, probe_age)
        if action == "probe":
            log.warning("socket silent %.0fs — probing", silence)
            try:
                self._raw_send("2")  # engine.io ping probe
            except BrokerConnectionError:
                self._try_reconnect_guarded()
                return 5.0
            self._probe_at = now
        elif action == "dead":
            log.warning("socket silent %.0fs, probe unanswered — reconnecting",
                        silence)
            self._probe_at = None
            self._try_reconnect_guarded()
            return 5.0
        if self._connected.is_set() and now >= self._next_tick:
            try:
                self._raw_send(build_tick())
            except BrokerConnectionError:
                self._try_reconnect_guarded()
                return 5.0
            self._next_tick = now + 5.0
        return min(5.0, max(2.0, interval / 2.0))

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
                self._pending_bin = None
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
                self.authorize(timeout=5.0)
                self._connected.set()
                self.was_connected = True
                self._last_pong = time.time()
                self._probe_at = None
                self._next_tick = 0.0  # heartbeat on the next pumper lap
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
            "stream_events": list(self._stream_names),
            "drops": dict(self._drop_counts),
        }


__all__ = ["QuotexSocket"]
