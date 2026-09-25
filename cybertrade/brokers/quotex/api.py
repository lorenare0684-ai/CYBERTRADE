"""High-level Quotex API: website login, market data, trading, settlement.

This is *unofficial* integration against community-documented endpoints and
event names (see docs/QUOTEX_PROTOCOL.md).  Default account is the broker's
PRACTICE purse.  Live (REAL) order flow can violate the broker's Terms of
Service — see DISCLAIMER.md before ever enabling it.

Phase-30 "ghost wire": every venue frame rides a :class:`~...ghost.Pacekeeper`
(human jitter, order think-time, sliding orders/min window), subscriptions are
replayed after any reconnect (client-level or supervisor-level), portfolio
snapshots fold into order state, and session-fault venue errors are classified
loudly (re-pair via ``cybertrade quotex login``) instead of retried blindly.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...data.models import AccountSnapshot, Candle, Tick
from ...exceptions import (
    BrokerAuthError,
    BrokerConnectionError,
    NetworkError,
    OrderRejected,
)
from ...network.http_client import HttpClient
from ...utils import timex
from ...utils.jsonx import dig
from . import constants as C
from .client import QuotexSocket
from .ghost import Pacekeeper, is_session_fault, parity_headers
from .models import QXAsset, QXBalance, QXCandle, QXOrderRequest, QXOrderResult, QXSession
from .protocol import (
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
    build_portfolio,
    build_sell_option,
    build_subscribe_candles,
    build_unsubscribe_candles,
    make_request_id,
    parse_balance,
    parse_candles,
    parse_order_result,
    parse_portfolio,
    parse_quotes,
    parse_tick,
)

log = logging.getLogger("cybertrade.qx.api")

# Sign-in responses drift between site deployments: the session token has
# been seen under all of these keys (and sometimes as a bare JSON string).
_SSID_PATHS = (
    "session", "ssid", "token", "access_token", "id",
    "data.session", "data.ssid", "data.token", "data.access_token", "data.id",
    "result.session", "result.ssid", "payload.session", "payload.ssid",
)

# Session cookie names worth trying when the JSON body carries no token
# (same priority as pairing: the live site's names first, legacy last).
_SSID_COOKIES = ("session", "ssid", "qx_session", "sessionid", "PHPSESSID")

# Markers that say "a browser challenge ate your login", not "bad password".
_CHALLENGE_MARKERS = (
    "cf-challenge", "just a moment", "cloudflare", "captcha",
    "__cf_bm", "attention required", "verify you are human",
)


def _extract_ssid(data: Any) -> str:
    """Harvest the websocket session token from a signin response body."""
    if isinstance(data, str):
        candidate = data.strip().strip('"')
        return candidate if len(candidate) >= 8 else ""
    if not isinstance(data, dict):
        return ""
    for path in _SSID_PATHS:
        raw = dig(data, path, "")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if type(raw) is int and raw:
            return str(raw)
    return ""


def _coerce_ssid(raw: str) -> str:
    """Unwrap a pasted authorization frame down to the session token.

    Community guides tell operators to copy the ``42["authorization",…]``
    frame from DevTools; feeding that whole frame in as the session used to
    fail auth opaquely. Bare tokens pass through untouched.
    """
    text = (raw or "").strip()
    if not text or ("authorization" not in text and not text.startswith("42[")):
        return text
    start = text.find("[")
    try:
        data = json.loads(text[start:] if start != -1 else text)
    except ValueError:
        return text
    rows = [data[1]] if isinstance(data, list) and len(data) >= 2 else []
    if isinstance(data, dict):
        rows.append(data)
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("session", "ssid", "token"):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if type(val) is int and val:
                return str(val)
    return text


def _looks_like_challenge(status: int, text: str) -> bool:
    """True when the signin reply is a challenge page, not a session."""
    lowered = (text or "").lower()
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return True
    # A 200 that is HTML rather than JSON is a challenge/error page.
    if status == 200 and "<html" in lowered:
        return True
    return False


class QuotexAPI:
    """Session facade used by the broker adapter and the CLI."""

    def __init__(
        self,
        *,
        http_base: str = C.HTTP_BASE,
        ws_url: str = C.WS_URL,
        user_agent: str = C.USER_AGENT,
        demo: bool = True,
        timeout: float = 15.0,
        legacy_orders: bool = False,
        ghost: bool = True,
        order_think_ms: int = 140,
        order_min_gap_ms: int = 350,
        max_orders_per_min: int = 10,
        reconnect_max: int = 12,
        pace: Optional[Pacekeeper] = None,
    ) -> None:
        self.http_base = http_base
        self.ws_url = ws_url
        self.user_agent = user_agent
        self.demo = demo
        self.timeout = timeout
        self.legacy_orders = legacy_orders
        self.reconnect_max = max(1, int(reconnect_max))

        self.pace = pace or Pacekeeper(
            enabled=ghost,
            order_think_ms=order_think_ms,
            order_min_gap_ms=order_min_gap_ms,
            max_orders_per_min=max_orders_per_min,
        )
        self.http = HttpClient(
            base_url=http_base,
            user_agent=user_agent,
            timeout=timeout,
            headers=parity_headers(user_agent, origin=http_base),
        )
        self.session = QXSession(user_agent=user_agent, demo=demo)
        self.socket: Optional[QuotexSocket] = None

        self.balance = QXBalance()
        self.assets: Dict[str, QXAsset] = {}
        from .catalog import AssetCatalog

        self.catalog = AssetCatalog.from_static()
        self._last_tick: Dict[str, Tick] = {}
        self._candles: Dict[Tuple[str, int], List[Candle]] = {}
        self._orders: Dict[str, QXOrderResult] = {}
        self._listeners: List[Callable[[str, Any], None]] = []
        self._lock = threading.RLock()
        self._tick_handlers: List[Callable[[Tick], None]] = []
        # Phase-30: replayable candle subscriptions + session health.
        self._subs: Dict[Tuple[str, int], None] = {}
        self._hist_tf: Dict[str, int] = {}
        self._session_stale = False
        # Set by the dispatcher on the first balance push — the cheapest
        # proof that the venue accepted the session (login verification
        # waits on this instead of reading a balance that is still 0.0).
        self._balance_seen = threading.Event()

    # -- website session ---------------------------------------------------
    def login(self, email: str, password: str, is_demo: Optional[bool] = None) -> QXSession:
        """Sign in on the website to harvest session cookies + ssid.

        Flow (community-documented):
            POST /api/signin  {email,password}  -> session cookies + ssid
            WS authorization frame carries the ssid.
        """
        if is_demo is not None:
            self.demo = is_demo
        payload = {"email": email, "password": password, "remember": 1}
        # qxbroker.com and quotex.com are the same venue behind different
        # front doors; a dead route must not read as a dead account.
        bases = list(dict.fromkeys([self.http_base, C.HTTP_BASE, C.HTTP_BASE_ALT]))
        resp = None
        last_net_error: Optional[Exception] = None
        for base in bases:
            try:
                self.http.base_url = base.rstrip("/")
                resp = self.http.post(
                    C.SIGNIN_PATH,
                    json_body=payload,
                    headers={"Origin": base, "Referer": f"{base}/en/login"},
                )
                last_net_error = None
                break
            except NetworkError as exc:
                last_net_error = exc
                continue
            except Exception as exc:  # noqa: BLE001
                raise BrokerAuthError(f"signin request failed: {exc}") from exc
        if resp is None:
            tried = ", ".join(bases)
            raise BrokerAuthError(
                f"signin request failed ({last_net_error}); tried {tried}"
            ) from last_net_error
        self.http_base = self.http.base_url

        data = resp.json(default={}) or {}
        ssid = _extract_ssid(data)
        if not ssid and resp.ok:
            # cookie-only flows: fall back to the session cookie value
            for name in _SSID_COOKIES:
                ssid = self.http.jar.cookies.get(name, "") or ""
                if ssid:
                    break
        if not ssid:
            if resp.status == 401:
                raise BrokerAuthError("login rejected: bad email/password (HTTP 401)")
            if _looks_like_challenge(resp.status, resp.text()):
                raise BrokerAuthError(
                    "login blocked by a Cloudflare/CAPTCHA browser challenge "
                    f"(HTTP {resp.status}) — headless sign-in cannot solve it; "
                    "pair instead: `cybertrade quotex login` (Chrome opens; "
                    "you sign in and solve the CAPTCHA once)"
                )
            if resp.status in (403, 429):
                raise BrokerAuthError(
                    f"login rejected (HTTP {resp.status}): bad credentials, "
                    "rate limit, or CF challenge"
                )
            detail = str(dig(data, "message", dig(data, "error", "")) or "").strip()
            if detail and len(detail) < 200:
                raise BrokerAuthError(f"login failed (HTTP {resp.status}): {detail}")
            raise BrokerAuthError(
                f"login failed (HTTP {resp.status}): no session in response — "
                "site may require browser CF challenge; use --ssid instead"
            )
        user_id = str(
            dig(data, "user_id", dig(data, "userId", dig(data, "data.user_id", ""))) or ""
        )
        self.session = QXSession(
            ssid=ssid,
            cookies=self.http.jar.header(),
            user_agent=self.user_agent,
            demo=self.demo,
            user_id=user_id,
            host=self.http_base.split("//")[-1],
        )
        self._session_stale = False
        log.info("website login ok host=%s demo=%s", self.session.host, self.demo)
        self._emit("login", self.session.to_dict())
        return self.session

    def set_ssid(self, ssid: str, cookies: str = "") -> QXSession:
        """Bypass credentials: inject a session id obtained from a browser.

        Accepts a bare session token *or* a pasted Socket.IO authorization
        frame (``42["authorization",{"session":"…",…}]``) — the format
        community guides tell operators to copy from DevTools.
        """
        self.session = QXSession(
            ssid=_coerce_ssid(ssid),
            cookies=cookies,
            user_agent=self.user_agent,
            demo=self.demo,
            host=self.http_base.split("//")[-1],
        )
        self._session_stale = False
        return self.session

    # -- socket ------------------------------------------------------------
    def connect(self, authorize: bool = True) -> bool:
        if not self.session.ssid:
            raise BrokerAuthError("no session — call login() or set_ssid() first")
        # Phase-30: never leave a ghost socket behind (double-connect must
        # replace the old wire, not fork a second one).
        if self.socket is not None:
            try:
                self.close()
            except Exception:  # noqa: BLE001
                pass
        self._balance_seen.clear()
        errors: List[str] = []
        for url in dict.fromkeys([self.ws_url, C.WS_URL, C.WS_URL_ALT]):
            sock = QuotexSocket(
                self.session.ssid,
                ws_url=url,
                cookies=self.session.cookies,
                user_agent=self.user_agent,
                origin=self.http_base,
                is_demo=self.demo,
                on_event=self._on_socket_event,
                on_reconnected=self._on_reconnected,
                reconnect_max=self.reconnect_max,
                timeout=self.timeout,
            )
            try:
                sock.connect(authorize=authorize)
            except BrokerAuthError:
                try:
                    sock.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                raise  # a rejected session is final — no host will accept it
            except (BrokerConnectionError, NetworkError, OSError) as exc:
                errors.append(f"{url}: {exc}")
                try:
                    sock.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                continue
            self.socket = sock
            self.ws_url = url
            break
        else:
            raise BrokerConnectionError(
                "quotex websocket unreachable (" + "; ".join(errors) + ")"
            )
        # Supervisor heals call api.connect() directly — replay here too so
        # either reconnect path restores the chart stream.
        self.change_balance(C.ACCOUNT_DEMO if self.demo else C.ACCOUNT_REAL)
        self._bootstrap_session()
        self._replay_subscriptions()
        return True

    def wait_for_balance(self, timeout: float = 6.0) -> bool:
        """Block until the venue confirms the session with a balance push."""
        return self._balance_seen.wait(timeout)

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.disconnect()
            finally:
                self.socket = None

    @property
    def connected(self) -> bool:
        return self.socket is not None and self.socket.connected

    # -- market data -------------------------------------------------------
    def subscribe(self, asset: str, timeframe_seconds: int = 60) -> None:
        """Stream an asset: the venue needs the full subscribe trio.

        ``instruments/update`` alone leaves the wire silent — the trade
        tab always follows it with ``chart_notification/get`` and
        ``depth/follow``, so we send all three, every time.
        """
        self._subs[(asset, int(timeframe_seconds))] = None
        self._send(build_subscribe_candles(asset, timeframe_seconds), kind="frame")
        self._send(build_chart_notification(asset), kind="frame")
        self._send(build_depth_follow(asset), kind="frame")

    def unsubscribe(self, asset: str, timeframe_seconds: int = 60) -> None:
        self._subs.pop((asset, int(timeframe_seconds)), None)
        try:
            self._send(build_unsubscribe_candles(asset, timeframe_seconds), kind="frame")
            self._send(build_depth_unfollow(asset), kind="frame")
        except Exception as exc:  # noqa: BLE001 — unsubscribe is courtesy
            log.debug("unsubscribe failed %s: %s", asset, exc)

    def _bootstrap_session(self) -> None:
        """Post-auth bootstrap: what the trade tab sends on every open."""
        for label, wire in (
            ("indicator/list", build_indicator_list()),
            ("drawing/load", build_drawing_load()),
            ("pending/list", build_pending_list()),
            ("chart_notification/get", build_chart_notification()),
            ("instruments/get", build_instruments()),
        ):
            try:
                self._send(wire, kind="frame")
            except Exception as exc:  # noqa: BLE001 — bootstrap is best-effort
                log.debug("bootstrap %s failed: %s", label, exc)

    def _replay_subscriptions(self) -> int:
        """Re-send every candle subscription after a reconnect (Phase-30)."""
        sent = 0
        for asset, tf in list(self._subs.keys()):
            try:
                self.subscribe(asset, tf)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("resubscribe failed %s@%ss: %s", asset, tf, exc)
        if sent:
            log.info("replayed %d candle subscriptions", sent)
        return sent

    def _on_reconnected(self) -> None:
        """Client-level reconnect hook: restore stream + session bootstrap."""
        try:
            sent = self._replay_subscriptions()
            self._bootstrap_session()
            self._emit("reconnected", self.stats())
            log.info("venue wire restored — replayed %d subscriptions", sent)
        except Exception as exc:  # noqa: BLE001
            log.warning("post-reconnect restore failed: %s", exc)

    def get_candles(
        self, asset: str, timeframe_seconds: int = 60, count: int = 200, wait: float = 3.0
    ) -> List[Candle]:
        key = (asset, timeframe_seconds)
        with self._lock:
            self._candles.setdefault(key, [])
        # History only flows for subscribed assets (the chart subscribes
        # before it ever asks), so make sure the trio went out first. The
        # reply carries no timeframe, so remember what we asked for.
        if key not in self._subs:
            self.subscribe(asset, timeframe_seconds)
        self._hist_tf[asset] = int(timeframe_seconds)
        self._send(build_candle_history(asset, timeframe_seconds, count), kind="history")
        deadline = time.time() + wait
        while time.time() < deadline:
            with self._lock:
                if len(self._candles.get(key, [])) >= min(count, 5):
                    break
            time.sleep(0.05)
        with self._lock:
            return list(self._candles.get(key, []))

    def last_price(self, asset: str) -> Optional[float]:
        tick = self._last_tick.get(asset)
        return tick.price if tick else None

    def payout_for(self, asset: str, expiry_seconds: int = 60) -> float:
        meta = self.assets.get(asset)
        if meta:
            return meta.payout
        return self.catalog.payout_for(asset, 0.85)

    def is_tradable(self, asset: str) -> bool:
        with self._lock:
            live = self.assets.get(asset)
        if live is not None:
            return bool(live.open)
        return self.catalog.is_tradable(asset)

    def request_instruments(self) -> None:
        """Ask the venue to push its instrument listing (fills the catalog)."""
        self._send(build_instruments(), kind="history")

    # -- account -----------------------------------------------------------
    def request_balance(self) -> QXBalance:
        """Freshest known balance — the venue pushes it, there is no query.

        (Older community docs named a ``balance`` request frame; the live
        venue never answered it. Balance pushes arrive after auth and
        after every ``account/change``.)
        """
        return self.balance

    def change_balance(self, account_type: str = C.ACCOUNT_DEMO) -> None:
        self.demo = account_type.upper() != C.ACCOUNT_REAL
        self._send(build_change_balance(account_type), kind="frame")

    def account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            balance=self.balance.balance,
            equity=self.balance.balance,
            margin_used=0.0,
            open_positions=len([o for o in self._orders.values() if o.status == "open"]),
            currency=self.balance.currency,
        )

    # -- trading -----------------------------------------------------------
    def buy(
        self,
        asset: str,
        amount: float,
        action: str,
        duration: int,
        option_type: int = C.OPTION_TYPE_DIGITAL,
    ) -> QXOrderRequest:
        action = action.lower()
        if action not in ("call", "put"):
            raise OrderRejected(f"action must be call/put, got {action!r}", code="BAD_SIDE")
        if duration not in C.DURATIONS:
            # round to nearest allowed duration
            duration = min(C.DURATIONS, key=lambda d: abs(d - duration))
        req = QXOrderRequest(
            asset=asset,
            amount=amount,
            action=action,
            duration=duration,
            is_demo=self.demo,
            option_type=option_type,
            request_id=make_request_id(),
            time=int(timex.now()) + duration,
        )
        # Phase-30: think-time + min gap + orders/min window all ride here.
        self._send(build_order(req, legacy=self.legacy_orders), kind="order")
        log.info(
            "qx order sent %s %s %.2f exp=%ds demo=%s rid=%s",
            asset, action, amount, duration, self.demo, req.request_id,
        )
        self._emit("order_sent", req.to_payload())
        return req

    def sell_option(self, order_id: str) -> None:
        # sell-backs are trade actions too — same organic gate as orders.
        self._send(build_sell_option(order_id), kind="order")

    def open_trades(self) -> List[QXOrderResult]:
        self._send(build_portfolio(), kind="poll")
        return [o for o in self._orders.values() if o.status == "open"]

    # -- event plumbing ----------------------------------------------------
    def add_listener(self, fn: Callable[[str, Any], None]) -> None:
        self._listeners.append(fn)

    def add_tick_handler(self, fn: Callable[[Tick], None]) -> None:
        self._tick_handlers.append(fn)

    def _emit(self, kind: str, payload: Any) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def _send(self, wire: str, kind: str = "frame") -> None:
        if self.socket is None:
            raise BrokerConnectionError("socket not connected")
        self.pace.wait(kind)
        if self.socket is None:  # pacing may have waited through a close()
            raise BrokerConnectionError("socket not connected")
        self.socket.send(wire)

    def _handle_tick(self, asset: str, price: float, ts: int) -> None:
        """One quote into the tick pipeline (books, discovery, handlers)."""
        tick = Tick(asset=asset, price=price,
                    ts=ts / 1000.0 if ts > 1e11 else float(ts))
        discovered = None
        with self._lock:
            self._last_tick[asset] = tick
            if asset not in self.assets and self.catalog.get(asset) is None:
                # Quote-driven discovery: the venue sometimes streams
                # quotes for assets its listing never named.  A bare
                # sighting still joins the catalog (kind inferred,
                # payout static) so the boards show everything alive.
                from .catalog import infer_kind
                from .models import QXAsset

                discovered = QXAsset(
                    name=asset, asset_id=asset,
                    payout=self.catalog.payout_for(asset),
                    open=True,
                    is_otc=asset.endswith("_otc"),
                    kind=infer_kind(asset))
                self.assets[asset] = discovered
                self.catalog.upsert(discovered)
        if discovered is not None:
            self._emit("instruments", [discovered])
        for fn in list(self._tick_handlers):
            try:
                fn(tick)
            except Exception:  # noqa: BLE001
                pass
        self._emit("tick", tick)

    def _handle_live_candle(self, payload: Any) -> None:
        """A ``candle-generated`` push: a closed bar, plus its closing tick."""
        if not isinstance(payload, dict):
            return
        asset = str(payload.get("asset") or "")
        if not asset:
            return
        tf = int(payload.get("period") or self._hist_tf.get(asset, 0) or 60)
        qc = QXCandle.from_payload(asset, payload, tf)
        if qc.open > 0 and qc.close > 0:
            self._ingest_candle(qc)
        if qc.close > 0:
            try:
                ts = int(qc.open_ts)
            except (TypeError, ValueError):
                ts = 0
            self._handle_tick(asset, qc.close, ts)

    def _on_socket_event(self, name: str, args: List[Any]) -> None:
        """Central dispatcher — tolerant of unknown/renamed venue events."""
        if name == C.SV_QUOTES:
            for asset, price, ts in parse_quotes(args[0] if args else []):
                self._handle_tick(asset, price, ts)
        elif name == C.SV_CANDLE_GENERATED:
            self._handle_live_candle(args[0] if args else {})
        elif name == C.SV_S_AUTHORIZATION:
            log.info("venue authorized the session")
            self._emit("authorized", {})
        elif name in (C.SV_TICK, "quote", C.SV_CANDLE):
            asset, price, ts = parse_tick(args)
            if asset and price > 0:
                self._handle_tick(asset, price, ts)

        elif name in (C.SV_CANDLE_HISTORY, C.SV_CANDLES, C.SV_HISTORY_LOAD,
                      C.SV_HISTORY_LIST_V2):
            asset = str(dig(args[0] if args else {}, "asset", "")) if args else ""
            tf = self._hist_tf.get(asset, 60) if asset else 60
            qlist = parse_candles(asset, args, tf)
            for qc in qlist:
                self._ingest_candle(qc)

        elif name in (C.SV_BALANCE, C.SV_BALANCE_UPDATE):
            self.balance = parse_balance(args, demo=self.demo)
            self._balance_seen.set()
            if self.balance.user_id and not self.session.user_id:
                # A paired session carries no identity of its own — adopt
                # the venue-confirmed account id for continuity scoping.
                self.session.user_id = self.balance.user_id
                log.info("venue confirmed account %s", self.balance.user_id)
            self._emit("balance", self.balance)

        elif name in C.INSTRUMENT_EVENTS:
            from .protocol import parse_instruments

            listing = parse_instruments(args)
            if listing:
                with self._lock:
                    for meta in listing:
                        self.assets[meta.name] = meta
                self.catalog.update(listing)
                self._emit("instruments", listing)

        elif name in (C.SV_ORDER, C.SV_ORDERS, C.SV_ORDER_RESULT, "profit"):
            result = parse_order_result(args)
            if result.order_id or result.request_id:
                key = result.order_id or result.request_id
                with self._lock:
                    self._orders[key] = result
                self._emit("order", result)

        elif name in (C.SV_PORTFOLIO, C.EV_PORTFOLIO, "portfolio", "openOrders"):
            rows = parse_portfolio(args)
            if rows:
                with self._lock:
                    for row in rows:
                        key = row.order_id or row.request_id or row.asset
                        if key:
                            self._orders[key] = row
                self._emit("portfolio", rows)

        elif name in (C.SV_ERROR, "error", C.SV_AUTH_REJECT):
            from .protocol import parse_error

            msg = parse_error(args)
            log.warning("qx error event: %s", msg)
            if is_session_fault(msg) and not self._session_stale:
                # Loud, not blind: a dead session must be re-paired by a
                # human (Chrome + CAPTCHA), never hammered with retries.
                self._session_stale = True
                log.critical(
                    "venue session fault: %s — run `cybertrade quotex login` "
                    "and solve the CAPTCHA once", msg,
                )
                self._emit("session_stale", msg)
            self._emit("error", args)

        else:
            log.debug("qx event %s ignored", name)
            self._emit("other", {"name": name, "args": args})

    def _ingest_candle(self, qc: QXCandle) -> None:
        key = (qc.asset, qc.timeframe_seconds)
        candle = Candle(
            asset=qc.asset,
            timeframe_seconds=qc.timeframe_seconds,
            open_ts=float(qc.open_ts if qc.open_ts > 1e11 else qc.open_ts),
            open=qc.open,
            high=qc.high,
            low=qc.low,
            close=qc.close,
            volume=qc.volume,
            closed=True,
        )
        with self._lock:
            series = self._candles.setdefault(key, [])
            series.append(candle)
            if len(series) > 2000:
                del series[:-2000]
        self._emit("candle", candle)

    # -- stats -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "session": self.session.to_dict(),
            "session_stale": self._session_stale,
            "balance": self.balance.balance,
            "account_type": self.balance.account_type,
            "socket": self.socket.stats() if self.socket else {},
            "tracked_assets": len(self._last_tick),
            "subscriptions": len(self._subs),
            "pace": self.pace.stats(),
        }


__all__ = ["QuotexAPI"]
