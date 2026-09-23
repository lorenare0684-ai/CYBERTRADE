"""Wire message builders/parsers for the Quotex Socket.IO event dialect."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ...network.socketio import (
    SocketPacket,
    encode_event,
    event_args,
    event_name,
)
from ...utils.jsonx import dig
from . import constants as C
from .models import QXAsset, QXBalance, QXCandle, QXOrderRequest, QXOrderResult


def make_request_id() -> str:
    """Quotex request ids look like compact epoch-ish integers/strings."""
    return str(int(time.time() * 1000))[-12:] + uuid.uuid4().hex[:4]


# ---------------------------------------------------------------------------
# Builders (client → server)
# ---------------------------------------------------------------------------
def build_authorization(ssid: str, is_demo: bool = True, tournament_id: int = 0) -> str:
    """Authenticate the socket with the website session id.

    Confirmed shape (community docs / stable_api):
        42["authorization",{"session":"<ssid>","isDemo":1,"tournamentId":0}]
    """
    return encode_event(
        C.EV_AUTHORIZATION,
        {
            "session": ssid,
            "isDemo": 1 if is_demo else 0,
            "tournamentId": tournament_id,
        },
    )


def build_order(req: QXOrderRequest, legacy: bool = False) -> str:
    """Place a binary option.

    Modern shape:
        42["orders/open",{asset,amount,time,action,isDemo,requestId,optionType}]
    Legacy shape:
        42["buyOption",{asset,amount,action,duration,isDemo,requestId}]
    """
    if legacy:
        return encode_event(C.EV_ORDER_OPEN_ALT, req.to_legacy_payload())
    return encode_event(C.EV_ORDERS_OPEN, req.to_payload())


def build_sell_option(order_id: str) -> str:
    """Early-sale / close of an open option (sell_option in stable_api)."""
    return encode_event(C.EV_SELL_OPTION, {"id": order_id})


def build_orders_close(order_id: str) -> str:
    return encode_event(C.EV_ORDERS_CANCEL, {"id": order_id})


def build_balance() -> str:
    return encode_event(C.EV_BALANCE, {})


def build_change_balance(account_type: str = C.ACCOUNT_DEMO) -> str:
    """Switch the active purse: PRACTICE or REAL."""
    return encode_event(C.EV_CHANGE_BALANCE, {"accountType": account_type})


def build_candle_history(
    asset: str,
    timeframe_seconds: int = 60,
    count: int = 200,
    request_id: Optional[str] = None,
) -> str:
    """Request historical candles.  Community shape (tolerant server variants):
        42["candleHistory",{"asset":..., "timeframe":..., "count":..., "requestId":...}]
    """
    return encode_event(
        C.EV_CANDLE_HISTORY,
        {
            "asset": asset,
            "timeframe": timeframe_seconds,
            "time": timeframe_seconds,
            "count": count,
            "requestId": request_id or make_request_id(),
        },
    )


def build_subscribe_candles(asset: str, timeframe_seconds: int = 60) -> str:
    return encode_event(
        C.EV_SUBSCRIBE_CANDLE,
        {"asset": asset, "timeframe": timeframe_seconds},
    )


def build_unsubscribe_candles(asset: str, timeframe_seconds: int = 60) -> str:
    return encode_event(
        C.EV_UNSUBSCRIBE_CANDLE,
        {"asset": asset, "timeframe": timeframe_seconds},
    )


def build_portfolio() -> str:
    return encode_event(C.EV_PORTFOLIO, {})


def build_instruments() -> str:
    return encode_event(C.EV_INSTRUMENT, {})


# ---------------------------------------------------------------------------
# Parsers (server → client)
# ---------------------------------------------------------------------------
def parse_event(packet: SocketPacket) -> Tuple[Optional[str], List[Any]]:
    """Uniform (name, args) extraction regardless of namespace/id framing."""
    name = event_name(packet)
    return name, event_args(packet)


def parse_instruments(args: List[Any]) -> List[QXAsset]:
    """Absorb the instrument listing — community payloads vary wildly:

    - ``[{name: {payout…}, …}]``          mapping of names to descriptors
    - ``[{asset/name: "EURUSD", …}, …]``  list of descriptor rows
    - ``[[name, {…}], …]``                pair rows
    - nested lists wrapping any of the above
    """
    out: List[QXAsset] = []

    def _row(item: Any) -> None:
        if isinstance(item, (list, tuple)):
            if len(item) >= 2 and isinstance(item[1], dict):
                out.append(QXAsset.from_payload(str(item[0]), item[1]))
            else:
                for sub in item:
                    _row(sub)
        elif isinstance(item, dict):
            name_key = None
            for key in ("asset", "name", "symbol"):
                if key in item and isinstance(item[key], str):
                    name_key = item[key]
                    break
            if name_key is not None:
                out.append(QXAsset.from_payload(name_key, item))
            else:
                # mapping form: {EURUSD: {...}, GBPUSD: {...}}
                for k, v in item.items():
                    if isinstance(v, dict):
                        out.append(QXAsset.from_payload(str(k), v))
                    elif isinstance(v, (list, tuple)):
                        _row(v)

    for entry in args or []:
        _row(entry)
    return out


def parse_candles(asset: str, args: List[Any], tf: int = 60) -> List[QXCandle]:
    """Extract candles from any known payload envelope."""
    out: List[QXCandle] = []
    if not args:
        return out
    payload = args[0]

    def _absorb(obj: Any) -> None:
        if isinstance(obj, dict):
            maybe_list = (
                obj.get("candles")
                or obj.get("data")
                or obj.get("history")
                or obj.get("list")
            )
            if isinstance(maybe_list, list):
                for item in maybe_list:
                    out.append(QXCandle.from_payload(asset, item, tf))
            elif "open" in obj or "o" in obj:
                out.append(QXCandle.from_payload(asset, obj, tf))
        elif isinstance(obj, (list, tuple)):
            # a bare candle row [t, o, c, h, l(, v)]
            if len(obj) >= 5 and all(isinstance(x, (int, float)) for x in obj[:5]):
                out.append(QXCandle.from_payload(asset, obj, tf))
                return
            for item in obj:
                _absorb(item)

    _absorb(payload)
    return out


def parse_tick(args: List[Any], default_asset: str = "") -> Tuple[str, float, int]:
    """(asset, price, ts_ms) from a tick/quote event.

    Tolerated shapes: dict rows (``{asset, price, time}`` with short keys too),
    bare price scalars, and ``[asset, price, ts?]`` rows.
    """
    if not args:
        return default_asset, 0.0, 0
    head = args[0]
    if isinstance(head, (int, float)):
        price = float(head)
        ts_raw = args[1] if len(args) > 1 and isinstance(args[1], (int, float)) else 0
        return default_asset, price, int(ts_raw or time.time() * 1000)
    if isinstance(head, (list, tuple)):
        if head and isinstance(head[0], str) and len(head) >= 2:
            ts_raw = head[2] if len(head) > 2 and isinstance(head[2], (int, float)) else 0
            return str(head[0]), float(head[1]), int(ts_raw or time.time() * 1000)
        return default_asset, 0.0, 0
    data = head if isinstance(head, dict) else {}
    asset = str(dig(data, "asset", dig(data, "s", default_asset)))
    price = float(dig(data, "price", dig(data, "p", dig(data, "close", dig(data, "c", 0.0)))) or 0.0)
    ts = int(dig(data, "ts", dig(data, "time", dig(data, "t", time.time() * 1000))) or 0)
    return asset, price, ts


def parse_balance(args: List[Any]) -> QXBalance:
    return QXBalance.from_payload(args[0] if args else {})


def parse_order_result(args: List[Any]) -> QXOrderResult:
    return QXOrderResult.from_payload(args[0] if args else {})


def parse_error(args: List[Any]) -> str:
    if not args:
        return "unknown venue error"
    data = args[0]
    if isinstance(data, dict):
        return str(dig(data, "message", dig(data, "error", data)))
    return str(data)


__all__ = [
    "make_request_id",
    "build_authorization",
    "build_order",
    "build_sell_option",
    "build_orders_close",
    "build_balance",
    "build_change_balance",
    "build_candle_history",
    "build_subscribe_candles",
    "build_unsubscribe_candles",
    "build_portfolio",
    "build_instruments",
    "parse_event",
    "parse_candles",
    "parse_tick",
    "parse_balance",
    "parse_order_result",
    "parse_error",
]
