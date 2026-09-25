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

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...data.models import AccountSnapshot, Candle, Tick
from ...exceptions import BrokerAuthError, BrokerConnectionError, OrderRejected
from ...network.http_client import HttpClient
from ...utils import timex
from ...utils.jsonx import dig
from . import constants as C
from .client import QuotexSocket
from .ghost import Pacekeeper, is_session_fault, parity_headers
from .models import QXAsset, QXBalance, QXCandle, QXOrderRequest, QXOrderResult, QXSession
from .protocol import (
    build_balance,
    build_candle_history,
    build_change_balance,
    build_instruments,
    build_order,
    build_portfolio,
    build_sell_option,
    build_subscribe_candles,
    make_request_id,
    parse_balance,
    parse_candles,
    parse_order_result,
    parse_portfolio,
    parse_tick,
)

log = logging.getLogger("cybertrade.qx.api")


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
        pace: Optional[Pacekeeper] = None,
    ) -> None:
        self.http_base = http_base
        self.ws_url = ws_url
        self.user_agent = user_agent
        self.demo = demo
        self.timeout = timeout
        self.legacy_orders = legacy_orders

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
        self._session_stale = False

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
        try:
            resp = self.http.post(
                C.SIGNIN_PATH,
                json_body=payload,
                headers={"Origin": self.http_base, "Referer": f"{self.http_base}/en/login"},
            )
        except Exception as exc:  # noqa: BLE001
            raise BrokerAuthError(f"signin request failed: {exc}") from exc

        if resp.status == 401 or resp.status == 403:
            raise BrokerAuthError("login rejected: bad credentials or CF challenge")
        data = resp.json(default={}) or {}
        ssid = str(dig(data, "session", dig(data, "ssid", dig(data, "data.session", ""))))
        if not ssid and resp.ok:
            # cookie-only flows: fall back to sessionid cookie value
            ssid = self.http.jar.cookies.get("sessionid", "")
        if not ssid:
            raise BrokerAuthError(
                f"login failed (HTTP {resp.status}): no session in response — "
                "site may require browser CF challenge; use --ssid instead"
            )
        self.session = QXSession(
            ssid=ssid,
            cookies=self.http.jar.header(),
            user_agent=self.user_agent,
            demo=self.demo,
            host=self.http_base.split("//")[-1],
        )
        self._session_stale = False
        log.info("website login ok host=%s demo=%s", self.session.host, self.demo)
        self._emit("login", self.session.to_dict())
        return self.session

    def set_ssid(self, ssid: str, cookies: str = "") -> QXSession:
        """Bypass credentials: inject a session id obtained from a browser."""
        self.session = QXSession(
            ssid=ssid,
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
        self.socket = QuotexSocket(
            self.session.ssid,
            ws_url=self.ws_url,
            cookies=self.session.cookies,
            user_agent=self.user_agent,
            is_demo=self.demo,
            on_event=self._on_socket_event,
            on_reconnected=self._on_reconnected,
            timeout=self.timeout,
        )
        self.socket.connect(authorize=authorize)
        # Supervisor heals call api.connect() directly — replay here too so
        # either reconnect path restores the chart stream.
        self._replay_subscriptions()
        self.change_balance(C.ACCOUNT_DEMO if self.demo else C.ACCOUNT_REAL)
        self.request_balance()
        return True

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
        self._subs[(asset, int(timeframe_seconds))] = None
        self._send(build_subscribe_candles(asset, timeframe_seconds), kind="frame")

    def _replay_subscriptions(self) -> int:
        """Re-send every candle subscription after a reconnect (Phase-30)."""
        sent = 0
        for asset, tf in list(self._subs.keys()):
            try:
                self._send(build_subscribe_candles(asset, tf), kind="frame")
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("resubscribe failed %s@%ss: %s", asset, tf, exc)
        if sent:
            log.info("replayed %d candle subscriptions", sent)
        return sent

    def _on_reconnected(self) -> None:
        """Client-level reconnect hook: restore stream + session heartbeat."""
        try:
            self._replay_subscriptions()
            self.request_balance()
            self._emit("reconnected", self.stats())
            log.info("venue wire restored — subscriptions replayed")
        except Exception as exc:  # noqa: BLE001
            log.warning("post-reconnect restore failed: %s", exc)

    def get_candles(
        self, asset: str, timeframe_seconds: int = 60, count: int = 200, wait: float = 3.0
    ) -> List[Candle]:
        key = (asset, timeframe_seconds)
        with self._lock:
            self._candles.setdefault(key, [])
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
        self._send(build_balance(), kind="poll")
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

    def _on_socket_event(self, name: str, args: List[Any]) -> None:
        """Central dispatcher — tolerant of unknown/renamed venue events."""
        if name in (C.SV_TICK, "quotes", "quote", C.SV_CANDLE):
            asset, price, ts = parse_tick(args)
            if asset and price > 0:
                tick = Tick(asset=asset, price=price, ts=ts / 1000.0 if ts > 1e11 else float(ts))
                with self._lock:
                    self._last_tick[asset] = tick
                for fn in list(self._tick_handlers):
                    try:
                        fn(tick)
                    except Exception:  # noqa: BLE001
                        pass
                self._emit("tick", tick)

        elif name in (C.SV_CANDLE_HISTORY, C.SV_CANDLES):
            asset = str(dig(args[0] if args else {}, "asset", "")) if args else ""
            qlist = parse_candles(asset, args)
            for qc in qlist:
                self._ingest_candle(qc)

        elif name in (C.SV_BALANCE, C.SV_BALANCE_UPDATE):
            self.balance = parse_balance(args)
            self._emit("balance", self.balance)

        elif name in (C.EV_INSTRUMENT, "instruments", "assets", "asset_list"):
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

        elif name in (C.SV_ERROR, "error"):
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
