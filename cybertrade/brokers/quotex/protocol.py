"""Wire message builders/parsers for the Quotex Socket.IO event dialect."""

from __future__ import annotations

import itertools
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
        42["authorization",{"session":"<ssid>","isDemo":1,"tournamentId":0,
                            "isFastHistory":true}]
    """
    return encode_event(
        C.EV_AUTHORIZATION,
        {
            "session": ssid,
            "isDemo": 1 if is_demo else 0,
            "tournamentId": tournament_id,
            "isFastHistory": True,
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


def build_tick() -> str:
    """Application heartbeat — the venue expects ``42["tick"]`` ~every 5s."""
    return encode_event(C.EV_TICK)


def build_indicator_list() -> str:
    """Post-auth bootstrap (what the trade tab sends on every open)."""
    return encode_event(C.EV_INDICATOR_LIST)


def build_drawing_load() -> str:
    return encode_event(C.EV_DRAWING_LOAD)


def build_pending_list() -> str:
    return encode_event(C.EV_PENDING_LIST)


def build_chart_notification(asset: Optional[str] = None) -> str:
    """Bare on bootstrap; per-asset (with version) as part of a subscribe."""
    if asset is None:
        return encode_event(C.EV_CHART_NOTIFICATION_GET)
    return encode_event(
        C.EV_CHART_NOTIFICATION_GET, {"asset": asset, "version": "1.0.0"}
    )


def build_instruments() -> str:
    """Ask the venue to push its instrument listing (``instruments/list``)."""
    return encode_event(C.EV_INSTRUMENTS_GET)


def build_subscribe_candles(asset: str, timeframe_seconds: int = 60) -> str:
    """Realtime subscribe, part 1 of 3.

    The venue only streams an asset after ``instruments/update`` *plus*
    ``chart_notification/get`` *plus* ``depth/follow`` (see
    :meth:`QuotexAPI.subscribe`) — any one alone leaves the wire silent.
    """
    return encode_event(
        C.EV_INSTRUMENTS_UPDATE,
        {"asset": asset, "period": int(timeframe_seconds)},
    )


def build_depth_follow(asset: str) -> str:
    return encode_event(C.EV_DEPTH_FOLLOW, asset)


def build_depth_unfollow(asset: str) -> str:
    return encode_event(C.EV_DEPTH_UNFOLLOW, asset)


def build_unsubscribe_candles(asset: str, timeframe_seconds: int = 60) -> str:
    return encode_event(C.EV_INSTRUMENTS_UNSUBSCRIBE, {"asset": asset})


_history_index = itertools.count(1)


def build_candle_history(
    asset: str,
    timeframe_seconds: int = 60,
    count: int = 200,
    request_id: Optional[str] = None,
    now: Optional[float] = None,
) -> str:
    """Request historical ticks (the venue aggregates nothing for you).

    Wire shape (pyquotex ``history/load``):
        42["history/load",{"asset":...,"index":...,"time":...,
                           "offset":...,"period":...}]
    ``time`` = window end (epoch seconds), ``offset`` = lookback in
    *seconds* (``count * period``), ``index`` echoes back in the reply for
    correlation.  The reply is a binary attachment: ``{asset, candles:
    [[ts, price, direction], …]}`` — see :func:`parse_candles`.
    """
    tf = max(1, int(timeframe_seconds))
    try:
        index = int(request_id) if request_id is not None else next(_history_index)
    except (TypeError, ValueError):
        index = next(_history_index)
    end = int(now if now is not None else time.time())
    return encode_event(
        C.EV_HISTORY_LOAD,
        {
            "asset": asset,
            "index": index,
            "time": end,
            "offset": max(tf, int(count) * tf),
            "period": tf,
        },
    )


def build_change_balance(account_type: str = C.ACCOUNT_DEMO) -> str:
    """Switch the active purse: PRACTICE (demo=1) or REAL (demo=0)."""
    demo = 0 if str(account_type).upper() == C.ACCOUNT_REAL else 1
    return encode_event(C.EV_ACCOUNT_CHANGE, {"demo": demo, "tournamentId": 0})


def build_portfolio() -> str:
    return encode_event(C.EV_PORTFOLIO, {})


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
            if _is_positional_row(item):
                out.append(_positional_asset(item))
            elif len(item) >= 2 and isinstance(item[1], dict):
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


def _is_positional_row(item: Any) -> bool:
    """True for a venue instrument row (``item[1]`` is the symbol string).

    Quote rows (``[asset, ts, price, …]``) also start with a string, but
    their second element is numeric — that is the discriminator.
    """
    return (
        isinstance(item, (list, tuple))
        and len(item) >= 6
        and isinstance(item[1], str)
    )


def _positional_asset(item: Any) -> QXAsset:
    name = str(item[1])
    pay_raw = item[5] if len(item) > 5 and isinstance(item[5], (int, float)) else 85
    payout = float(pay_raw)
    if payout > 1:
        payout = payout / 100.0
    kind = str(item[3]) if len(item) > 3 and isinstance(item[3], str) else ""
    if name.endswith("_otc") and kind and not kind.endswith("_otc"):
        kind += "_otc"
    return QXAsset(
        name=name,
        asset_id=str(item[0]),
        payout=payout,
        open=bool(item[14]) if len(item) > 14 else True,
        is_otc=name.endswith("_otc"),
        kind=kind,
    )


def parse_quotes(data: Any) -> List[Tuple[str, float, int]]:
    """``(asset, price, ts)`` rows from a bare quote batch.

    Quotes arrive outside any Socket.IO event — a bare
    ``[[asset, ts, price, direction], …]`` frame.  Timestamps pass
    through raw (the dispatcher normalizes ms → s).
    """
    rows = data if isinstance(data, list) else [data]
    out: List[Tuple[str, float, int]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        asset, ts_raw, px_raw = row[0], row[1], row[2]
        if not isinstance(asset, str) or not asset:
            continue
        try:
            price = float(px_raw)
            ts = int(ts_raw)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        out.append((asset, price, ts))
    return out


def is_placeholder(args: List[Any]) -> Optional[int]:
    """Binary attachments announced by a ``451-[event, {"_placeholder"…}]``.

    Returns how many binary frames to expect (``num + 1``), or None when
    ``args`` is not a placeholder at all.
    """
    if args and isinstance(args[0], dict) and args[0].get("_placeholder"):
        try:
            num = int(args[0].get("num", 0))
        except (TypeError, ValueError):
            num = 0
        return max(1, num + 1)
    return None


def _norm_ts(raw: Any) -> int:
    ts = int(raw)
    return ts // 1000 if ts > 1e11 else ts


def _aggregate_ticks(asset: str, ticks: List[Tuple[int, float]],
                     tf: int) -> List[QXCandle]:
    """Bucket ``history/load`` tick rows into OHLC bars (upstream parity).

    Buckets align to ``(ts // tf) * tf``; the last (still-forming) bucket
    is dropped, exactly like the reference client's ``calculate_candles``.
    """
    buckets: Dict[int, List[Tuple[int, float]]] = {}
    for ts, px in ticks:
        buckets.setdefault((ts // tf) * tf, []).append((ts, px))
    if not buckets:
        return []
    forming = max(buckets)
    out: List[QXCandle] = []
    for start in sorted(buckets):
        if start == forming:
            continue
        rows = buckets[start]
        pxs = [px for _, px in rows]
        out.append(QXCandle(
            asset=asset, open_ts=start, open=pxs[0], high=max(pxs),
            low=min(pxs), close=pxs[-1], timeframe_seconds=tf,
            volume=float(len(rows)),
        ))
    return out


def parse_candles(asset: str, args: List[Any], tf: int = 60) -> List[QXCandle]:
    """Extract candles from any known payload envelope."""
    out: List[QXCandle] = []
    ticks: List[Tuple[int, float]] = []
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
                    if (isinstance(item, (list, tuple)) and len(item) >= 3
                            and len(item) < 5
                            and all(isinstance(x, (int, float)) for x in item[:3])):
                        # history/load tick row [ts, price, direction]
                        ticks.append((_norm_ts(item[0]), float(item[1])))
                    else:
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
    out.extend(_aggregate_ticks(asset, ticks, tf))
    out.sort(key=lambda c: c.open_ts)
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


def parse_balance(args: List[Any], demo: bool = True) -> QXBalance:
    return QXBalance.from_payload(args[0] if args else {}, demo=demo)


def parse_portfolio(args: List[Any]) -> List[QXOrderResult]:
    """Absorb a portfolio snapshot into order rows (schema-tolerant).

    Community payloads vary: ``[{orders: [...]}]``, ``[{data: [...]}]``,
    bare lists of rows, or mappings ``{orderId: {…}}``.  Rows without any
    identity (no id / requestId / asset) are dropped rather than guessed.
    """
    out: List[QXOrderResult] = []

    def _absorb(obj: Any) -> None:
        if isinstance(obj, (list, tuple)):
            for item in obj:
                _absorb(item)
        elif isinstance(obj, dict):
            looks_row = "amount" in obj and any(
                k in obj for k in ("asset", "id", "orderId", "requestId")
            )
            if looks_row:
                row = QXOrderResult.from_payload(obj)
                if row.order_id or row.request_id or row.asset:
                    out.append(row)
                return
            for key in ("orders", "data", "portfolio", "list", "items", "open"):
                nested = obj.get(key)
                if nested:
                    _absorb(nested)
            for value in obj.values():
                if isinstance(value, dict):
                    _absorb(value)

    for entry in args or []:
        _absorb(entry)
    return out


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
    "build_tick",
    "build_indicator_list",
    "build_drawing_load",
    "build_pending_list",
    "build_chart_notification",
    "build_change_balance",
    "build_candle_history",
    "build_subscribe_candles",
    "build_unsubscribe_candles",
    "build_depth_follow",
    "build_depth_unfollow",
    "build_portfolio",
    "build_instruments",
    "parse_event",
    "parse_candles",
    "parse_tick",
    "parse_quotes",
    "is_placeholder",
    "parse_balance",
    "parse_portfolio",
    "parse_order_result",
    "parse_error",
]
