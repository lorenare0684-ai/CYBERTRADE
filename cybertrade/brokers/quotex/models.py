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
        if "demoBalance" in data or "liveBalance" in data:
            # the venue's own push shape — pick the active purse
            balance = float(data.get("demoBalance" if demo else "liveBalance") or 0.0)
            kind = "PRACTICE" if demo else "REAL"
        else:
            kind = str(dig(data, "data.accountType", dig(data, "accountType", "PRACTICE")))
            balance = float(dig(data, "data.balance", dig(data, "balance", 0.0)) or 0.0)
            demo = kind.upper() != "REAL"
        user_id = str(
            dig(data, "userId", dig(data, "user_id",
                dig(data, "data.userId", dig(data, "id", "")))) or ""
        )
        return cls(
            account_type=kind,
            balance=balance,
            currency=str(dig(data, "data.currency", dig(data, "currency", "USD"))),
            user_id=user_id,
            demo=demo,
        )


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
    option_type: int = 1
    request_id: str = ""
    tournament_id: int = 0
    time: int = 0                    # unix expiry timestamp (orders/open style)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "asset": self.asset,
            "amount": int(self.amount) if self.amount == int(self.amount) else self.amount,
            "time": self.time,
            "action": self.action,
            "isDemo": 1 if self.is_demo else 0,
            "requestId": self.request_id,
            "optionType": self.option_type,
            "tournamentId": self.tournament_id,
        }

    def to_legacy_payload(self) -> Dict[str, Any]:
        """buyOption shape used by older clients."""
        return {
            "asset": self.asset,
            "amount": self.amount,
            "action": self.action,
            "duration": self.duration,
            "isDemo": 1 if self.is_demo else 0,
            "requestId": self.request_id,
        }

    @property
    def side(self) -> Side:
        return Side.CALL if self.action.lower() == "call" else Side.PUT


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
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def won(self) -> bool:
        return self.status.lower() in ("win", "won", "profit")

    @property
    def lost(self) -> bool:
        return self.status.lower() in ("loss", "lost", "loose")

    @classmethod
    def from_payload(cls, payload: Any) -> "QXOrderResult":
        data = payload if isinstance(payload, dict) else {}
        inner = dig(data, "data", data) or {}
        if not isinstance(inner, dict):
            inner = {}
        payout_raw = dig(inner, "payout", dig(inner, "profit", 85)) or 85
        payout = float(payout_raw)
        if payout > 1:
            payout = payout / 100.0
        return cls(
            order_id=str(dig(inner, "id", dig(inner, "orderId", ""))),
            request_id=str(dig(inner, "requestId", "")),
            status=str(dig(inner, "status", dig(inner, "state", "open"))),
            asset=str(dig(inner, "asset", "")),
            amount=float(dig(inner, "amount", 0.0) or 0.0),
            action=str(dig(inner, "action", dig(inner, "direction", ""))),
            open_price=float(dig(inner, "openPrice", dig(inner, "open_price", 0.0)) or 0.0),
            close_price=float(dig(inner, "closePrice", dig(inner, "close_price", 0.0)) or 0.0),
            profit=float(dig(inner, "profit", 0.0) or 0.0),
            payout=payout,
            raw=data if isinstance(data, dict) else {},
        )


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
