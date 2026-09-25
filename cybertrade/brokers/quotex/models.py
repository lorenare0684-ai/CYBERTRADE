"""Typed models for Quotex payloads (tolerant of schema drift)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...constants import Side
from ...utils.jsonx import dig


@dataclass
class QXBalance:
    account_type: str = "PRACTICE"     # PRACTICE | REAL
    balance: float = 0.0
    currency: str = "USD"
    user_id: str = ""
    demo: bool = True

    @classmethod
    def from_payload(cls, payload: Any, demo: bool = True) -> "QXBalance":
        if isinstance(payload, (int, float)):
            return cls(balance=float(payload))
        if isinstance(payload, (list, tuple)) and payload and isinstance(payload[0], (int, float)):
            return cls(balance=float(payload[0]))
        data = payload if isinstance(payload, dict) else {}
        if isinstance(payload, (list, tuple)):
            # live ``s_balance/list``: rows per purse — pick the active one
            rows = [r for r in payload if isinstance(r, dict)]
            data = _pick_purse_row(rows, demo) or {}
        if isinstance(data.get("data"), dict) and not (
                "demoBalance" in data or "liveBalance" in data or "balance" in data):
            data = data["data"]  # settings/list wraps the profile in "data"
        if "demoBalance" in data or "liveBalance" in data:
            # the venue's own push shape — pick the active purse
            balance = float(data.get("demoBalance" if demo else "liveBalance") or 0.0)
            kind = "PRACTICE" if demo else "REAL"
        else:
            kind = str(dig(data, "data.accountType", dig(data, "accountType", "PRACTICE")))
            balance = float(dig(data, "data.balance", dig(data, "balance", 0.0)) or 0.0)
            demo = kind.upper() != "REAL"
        user_id = str(
            dig(data, "uid", dig(data, "userId", dig(data, "user_id",
                dig(data, "data.userId", dig(data, "id", ""))))) or ""
        )
        if user_id and not user_id.isdigit() and user_id == str(data.get("id", "")) \
                and not any(k in data for k in ("nickname", "uid", "userId", "currencyCode")):
            user_id = ""  # an "id" on a non-profile row is not an account id
        return cls(
            account_type=kind,
            balance=balance,
            currency=str(dig(data, "data.currency", dig(data, "currency", "USD"))),
            user_id=user_id,
            demo=demo,
        )


def _pick_purse_row(rows: List[Dict[str, Any]], demo: bool) -> Optional[Dict[str, Any]]:
    """Choose the active purse from a per-account balance list.

    Rows carry ``isDemo`` / ``demo`` / ``type`` in various spellings; when
    nothing marks them, a single row wins and otherwise the first does.
    """
    if not rows:
        return None
    want = 1 if demo else 0
    for r in rows:
        for key in ("isDemo", "demo", "is_demo"):
            if key in r:
                try:
                    if int(bool(r[key])) == want:
                        return r
                except (TypeError, ValueError):
                    pass
                break
        kind = str(r.get("type", r.get("accountType", ""))).upper()
        if kind and ((kind in ("DEMO", "PRACTICE")) == bool(demo)):
            return r
    for r in rows:
        if "demoBalance" in r or "liveBalance" in r:
            return r
    return rows[0]


@dataclass
class QXAsset:
    name: str
    asset_id: str = ""
    payout: float = 0.85
    open: bool = True
    is_otc: bool = False
    precision: int = 5
    kind: str = ""

    @classmethod
    def from_payload(cls, name: str, payload: Any) -> "QXAsset":
        data = payload if isinstance(payload, dict) else {}
        payout_raw = (
            dig(data, "payout", dig(data, "profit",
                dig(data, "payoutPercent",
                    dig(data, "profitPercent",
                        dig(data, "payout_percent", 85))))) or 85
        )
        payout = float(payout_raw)
        if payout > 1:
            payout = payout / 100.0
        return cls(
            name=name,
            asset_id=str(dig(data, "id", dig(data, "assetId", name))),
            payout=payout,
            open=bool(dig(data, "open", dig(data, "isOpen",
                dig(data, "active", dig(data, "isActive",
                    dig(data, "is_active", True)))))),
            is_otc=name.endswith("_otc") or bool(dig(data, "isOTC", False)),
            kind=str(dig(data, "kind", dig(data, "type", "")) or ""),
        )


@dataclass
class QXCandle:
    asset: str
    open_ts: int
    open: float
    high: float
    low: float
    close: float
    timeframe_seconds: int = 60
    volume: float = 0.0

    @classmethod
    def from_payload(cls, asset: str, payload: Any, tf: int = 60) -> "QXCandle":
        """Community payload shapes vary: dict with o/h/l/c/t or bare list."""
        if isinstance(payload, (list, tuple)) and len(payload) >= 5:
            return cls(
                asset=asset,
                open_ts=int(payload[0]),
                open=float(payload[1]),
                close=float(payload[2]),
                high=float(payload[3]),
                low=float(payload[4]),
                timeframe_seconds=tf,
                volume=float(payload[5]) if len(payload) > 5 else 0.0,
            )
        data = payload if isinstance(payload, dict) else {}
        ts = dig(data, "time", dig(data, "t",
              dig(data, "timestamp", dig(data, "index", dig(data, "ts", 0)))))
        return cls(
            asset=asset,
            open_ts=int(ts or 0),
            open=float(dig(data, "open", dig(data, "o", 0.0))),
            high=float(dig(data, "high", dig(data, "h", 0.0))),
            low=float(dig(data, "low", dig(data, "l", 0.0))),
            close=float(dig(data, "close", dig(data, "c", 0.0))),
            timeframe_seconds=int(
                dig(data, "timeframe", dig(data, "period", dig(data, "tf", tf))) or tf
            ),
            volume=float(dig(data, "volume", dig(data, "v", 0.0)) or 0.0),
        )


@dataclass
class QXOrderRequest:
    asset: str
    amount: float
    action: str                      # "call" | "put"
    duration: int                    # seconds
    is_demo: bool = True
    option_type: int = 100           # 100 = TIMER (time=duration), 1/3 = TIME (time=expiry)
    request_id: str = ""
    tournament_id: int = 0
    time: int = 0                    # TIMER: duration seconds; TIME: aligned expiry ts
    expiry_ts: float = 0.0           # when the contract settles (epoch seconds)

    @property
    def time_mode(self) -> str:
        return "TIMER" if int(self.option_type) == 100 else "TIME"

    def to_payload(self) -> Dict[str, Any]:
        """``orders/open`` body — byte-shape of the reference client.

        ``time`` is the duration for TIMER contracts and the aligned
        expiry timestamp otherwise; ``requestId`` rides as an integer
        when it is numeric (the venue echoes an epoch int back).
        """
        wire_time = self.time
        if not wire_time:
            wire_time = int(self.duration) if self.time_mode == "TIMER" else int(self.expiry_ts)
        return {
            "asset": self.asset,
            "amount": int(self.amount) if self.amount == int(self.amount) else self.amount,
            "time": int(wire_time),
            "action": self.action,
            "isDemo": 1 if self.is_demo else 0,
            "tournamentId": self.tournament_id,
            "requestId": _wire_request_id(self.request_id),
            "optionType": int(self.option_type),
        }

    def to_legacy_payload(self) -> Dict[str, Any]:
        """buyOption shape used by older clients."""
        return {
            "asset": self.asset,
            "amount": self.amount,
            "action": self.action,
            "duration": self.duration,
            "isDemo": 1 if self.is_demo else 0,
            "requestId": _wire_request_id(self.request_id),
        }

    @property
    def side(self) -> Side:
        return Side.CALL if self.action.lower() == "call" else Side.PUT


def _wire_request_id(value: Any) -> Any:
    """Numeric request ids go out as ints (venue parity); others untouched."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    text = str(value)
    if text.isdigit():
        return int(text)
    return value


@dataclass
class QXOrderResult:
    order_id: str = ""
    request_id: str = ""
    status: str = "open"
    asset: str = ""
    amount: float = 0.0
    action: str = ""
    open_price: float = 0.0
    close_price: float = 0.0
    profit: float = 0.0
    payout: float = 0.85
    expiry_ts: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def won(self) -> bool:
        return self.status.lower() in ("win", "won", "profit")

    @property
    def lost(self) -> bool:
        return self.status.lower() in ("loss", "lost", "loose")

    @property
    def closed(self) -> bool:
        return self.status.lower() not in ("open", "", "pending")

    @classmethod
    def from_payload(cls, payload: Any, closed: Optional[bool] = None) -> "QXOrderResult":
        """Absorb an order row.

        Venue rows (``s_orders/open`` ack, ``deals`` settlement) look like
        ``{id, requestId, asset, amount, command, openPrice, closePrice,
        profit, percentProfit, openTimestamp, closeTimestamp, …}`` — no
        ``status`` field.  ``profit`` on the *open ack* is the potential
        win, so the caller says whether the row is a settlement
        (``closed=True``) or an ack (``closed=False``); when it does not,
        an explicit ``status`` key decides and anything else is ``open``.
        """
        data = payload if isinstance(payload, dict) else {}
        inner = dig(data, "data", data) or {}
        if not isinstance(inner, dict):
            inner = {}
        payout_raw = dig(inner, "percentProfit",
                         dig(inner, "payout", dig(inner, "profitPercent", 85))) or 85
        try:
            payout = float(payout_raw)
        except (TypeError, ValueError):
            payout = 85.0
        if payout > 1:
            payout = payout / 100.0
        profit = _num(dig(inner, "profit", 0.0))
        close_price = _num(dig(inner, "closePrice", dig(inner, "close_price", 0.0)))
        open_price = _num(dig(inner, "openPrice", dig(inner, "open_price", 0.0)))

        action = str(dig(inner, "action", dig(inner, "direction", "")) or "")
        if not action and "command" in inner:
            try:
                action = "put" if int(inner["command"]) == 1 else "call"
            except (TypeError, ValueError):
                action = ""

        explicit = dig(inner, "status", dig(inner, "state", None))
        if closed is None and explicit is not None:
            status = str(explicit)
        elif closed:
            if profit > 0:
                status = "win"
            elif profit < 0:
                status = "loss"
            else:
                status = "refund"
        elif closed is False:
            status = "open"
        else:
            status = "open"
        if status.lower() == "closed":
            status = "win" if profit > 0 else ("loss" if profit < 0 else "refund")

        expiry_raw = dig(inner, "closeTimestamp", dig(inner, "expiry", dig(inner, "expiryTime", 0)))
        expiry = _num(expiry_raw)
        if expiry > 1e11:
            expiry /= 1000.0

        return cls(
            order_id=str(dig(inner, "id", dig(inner, "orderId", dig(inner, "ticket", ""))) or ""),
            request_id=str(dig(inner, "requestId", "") or ""),
            status=status,
            asset=str(dig(inner, "asset", "")),
            amount=_num(dig(inner, "amount", 0.0)),
            action=action,
            open_price=open_price,
            close_price=close_price,
            profit=profit,
            payout=payout,
            expiry_ts=expiry,
            raw=data if isinstance(data, dict) else {},
        )


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class QXSession:
    ssid: str = ""
    cookies: str = ""
    user_agent: str = ""
    demo: bool = True
    user_id: str = ""
    host: str = "qxbroker.com"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ssid": self.ssid[:8] + "…" if self.ssid else "",
            "demo": self.demo,
            "user_id": self.user_id,
            "host": self.host,
        }


__all__ = [
    "QXBalance",
    "QXAsset",
    "QXCandle",
    "QXOrderRequest",
    "QXOrderResult",
    "QXSession",
]
