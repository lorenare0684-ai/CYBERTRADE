"""High-level Quotex API: website login, market data, trading, settlement.

This is *unofficial* integration against community-documented endpoints and
event names (see docs/QUOTEX_PROTOCOL.md).  Default account is the broker's
PRACTICE purse.  Live (REAL) order flow can violate the broker's Terms of
Service — see DISCLAIMER.md before ever enabling it.
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
from .models import QXAsset, QXBalance, QXCandle, QXOrderRequest, QXOrderResult, QXSession
from .protocol import (
    build_balance,
    build_candle_history,
    build_change_balance,
    build_order,
    build_portfolio,
    build_sell_option,
    build_subscribe_candles,
    make_request_id,
    parse_balance,
    parse_candles,
    parse_order_result,
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
    ) -> None:
        self.http_base = http_base
        self.ws_url = ws_url
        self.user_agent = user_agent
        self.demo = demo
        self.timeout = timeout
        self.legacy_orders = legacy_orders

        self.http = HttpClient(base_url=http_base, user_agent=user_agent, timeout=timeout)
        self.session = QXSession(user_agent=user_agent, demo=demo)
        self.socket: Optional[QuotexSocket] = None

        self.balance = QXBalance()
        self.assets: Dict[str, QXAsset] = {}
        self._last_tick: Dict[str, Tick] = {}
        self._candles: Dict[Tuple[str, int], List[Candle]] = {}
        self._orders: Dict[str, QXOrderResult] = {}
        self._listeners: List[Callable[[str, Any], None]] = []
        self._lock = threading.RLock()
        self._tick_handlers: List[Callable[[Tick], None]] = []

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
        return self.session

    # -- socket ------------------------------------------------------------
    def connect(self, authorize: bool = True) -> bool:
        if not self.session.ssid:
            raise BrokerAuthError("no session — call login() or set_ssid() first")
        self.socket = QuotexSocket(
            self.session.ssid,
            ws_url=self.ws_url,
            cookies=self.session.cookies,
            user_agent=self.user_agent,
            is_demo=self.demo,
            on_event=self._on_socket_event,
            timeout=self.timeout,
        )
        self.socket.connect(authorize=authorize)
        self.change_balance(C.ACCOUNT_DEMO if self.demo else C.ACCOUNT_REAL)
        self.request_balance()
        return True

    def close(self) -> None:
        if self.socket is not None:
            self.socket.disconnect()
            self.socket = None

    @property
    def connected(self) -> bool:
        return self.socket is not None and self.socket.connected

    # -- market data -------------------------------------------------------
    def subscribe(self, asset: str, timeframe_seconds: int = 60) -> None:
        self._send(build_subscribe_candles(asset, timeframe_seconds))

    def get_candles(
        self, asset: str, timeframe_seconds: int = 60, count: int = 200, wait: float = 3.0
    ) -> List[Candle]:
        key = (asset, timeframe_seconds)
        with self._lock:
            self._candles.setdefault(key, [])
        self._send(build_candle_history(asset, timeframe_seconds, count))
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
        from ...constants import ASSET_CATALOG

        catalog = ASSET_CATALOG.get(asset)
        return float(catalog["payout"]) if catalog else 0.85

    # -- account -----------------------------------------------------------
    def request_balance(self) -> QXBalance:
        self._send(build_balance())
        return self.balance

    def change_balance(self, account_type: str = C.ACCOUNT_DEMO) -> None:
        self.demo = account_type.upper() != C.ACCOUNT_REAL
        self._send(build_change_balance(account_type))

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
        self._send(build_order(req, legacy=self.legacy_orders))
        log.info(
            "qx order sent %s %s %.2f exp=%ds demo=%s rid=%s",
            asset, action, amount, duration, self.demo, req.request_id,
        )
        self._emit("order_sent", req.to_payload())
        return req

    def sell_option(self, order_id: str) -> None:
        self._send(build_sell_option(order_id))

    def open_trades(self) -> List[QXOrderResult]:
        self._send(build_portfolio())
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

    def _send(self, wire: str) -> None:
        if self.socket is None:
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

        elif name in (C.SV_ORDER, C.SV_ORDERS, C.SV_ORDER_RESULT, "profit"):
            result = parse_order_result(args)
            if result.order_id or result.request_id:
                key = result.order_id or result.request_id
                with self._lock:
                    self._orders[key] = result
                self._emit("order", result)

        elif name in (C.SV_ERROR, "error"):
            log.warning("qx error event: %s", args)
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
            "balance": self.balance.balance,
            "account_type": self.balance.account_type,
            "socket": self.socket.stats() if self.socket else {},
            "tracked_assets": len(self._last_tick),
        }


__all__ = ["QuotexAPI"]
