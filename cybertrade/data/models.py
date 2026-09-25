"""Core market data structures shared by every subsystem."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..constants import OrderStatus, OrderType, Side, SignalStrength
from ..utils import timex


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tick:
    """A single price print."""

    asset: str
    price: float
    ts: float = field(default_factory=timex.now)
    bid: float = 0.0
    ask: float = 0.0

    @property
    def spread(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return self.ask - self.bid
        return 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return 0.5 * (self.bid + self.ask)
        return self.price


@dataclass
class Candle:
    """OHLCV bar.  ``closed`` marks finalized candles."""

    asset: str
    timeframe_seconds: int
    open_ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    closed: bool = False

    @property
    def close_ts(self) -> float:
        return self.open_ts + self.timeframe_seconds

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def direction(self) -> int:
        if self.close > self.open:
            return 1
        if self.close < self.open:
            return -1
        return 0

    def update(self, price: float, ts: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += 1.0

    def copy(self) -> "Candle":
        return Candle(
            asset=self.asset,
            timeframe_seconds=self.timeframe_seconds,
            open_ts=self.open_ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            closed=self.closed,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset": self.asset,
            "tf": self.timeframe_seconds,
            "t": self.open_ts,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
            "closed": self.closed,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Candle":
        return cls(
            asset=str(data.get("asset", "")),
            timeframe_seconds=int(data.get("tf", 60)),
            open_ts=float(data.get("t", 0.0)),
            open=float(data["o"]),
            high=float(data["h"]),
            low=float(data["l"]),
            close=float(data["c"]),
            volume=float(data.get("v", 0.0)),
            closed=bool(data.get("closed", True)),
        )


def candles_to_series(candles: Sequence[Candle]) -> Dict[str, List[float]]:
    """Split a candle list into parallel OHLCV lists for the indicators."""
    return {
        "open": [c.open for c in candles],
        "high": [c.high for c in candles],
        "low": [c.low for c in candles],
        "close": [c.close for c in candles],
        "volume": [c.volume for c in candles],
        "ts": [c.open_ts for c in candles],
    }


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    """A directional trading idea produced by a strategy."""

    asset: str
    side: Side
    confidence: float                    # 0..1
    strategy: str
    strength: SignalStrength = SignalStrength.NONE
    timeframe_seconds: int = 60
    ts: float = field(default_factory=timex.now)
    reason: str = ""
    expiry_seconds: int = 60
    price: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("sig"))

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.strength is SignalStrength.NONE:
            self.strength = confidence_to_strength(self.confidence)

    @property
    def is_trade(self) -> bool:
        return self.side.is_trade and self.confidence > 0

    @property
    def quality(self) -> float:
        """Blend of confidence and strength used by the ensemble."""
        return self.confidence * (1.0 + 0.1 * int(self.strength))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "asset": self.asset,
            "side": self.side.value,
            "confidence": round(self.confidence, 4),
            "strength": int(self.strength),
            "strategy": self.strategy,
            "tf": self.timeframe_seconds,
            "ts": self.ts,
            "reason": self.reason,
            "expiry": self.expiry_seconds,
            "price": self.price,
            "meta": self.meta,
        }


def confidence_to_strength(confidence: float) -> SignalStrength:
    if confidence >= 0.9:
        return SignalStrength.EXTREME
    if confidence >= 0.75:
        return SignalStrength.STRONG
    if confidence >= 0.55:
        return SignalStrength.MODERATE
    if confidence > 0.3:
        return SignalStrength.WEAK
    return SignalStrength.NONE


FLAT_SIGNAL = Signal(
    asset="",
    side=Side.FLAT,
    confidence=0.0,
    strategy="none",
    reason="no edge",
)


# ---------------------------------------------------------------------------
# Orders, fills, contracts, positions
# ---------------------------------------------------------------------------
@dataclass
class Order:
    """Order intent.  Binary orders settle automatically at expiry."""

    asset: str
    side: Side
    amount: float                        # stake in account currency
    order_type: OrderType = OrderType.BINARY
    expiry_seconds: int = 60
    payout: float = 0.85                 # expected payout fraction at entry
    limit_price: float = 0.0
    ts: float = field(default_factory=timex.now)
    status: OrderStatus = OrderStatus.PENDING
    broker_id: str = ""
    id: str = field(default_factory=lambda: _new_id("ord"))
    tag: str = ""
    strategy: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_binary(self) -> bool:
        return self.order_type is OrderType.BINARY

    @property
    def to_expiry(self) -> float:
        return self.ts + self.expiry_seconds

    def reject(self, reason: str) -> None:
        self.status = OrderStatus.REJECTED
        self.meta["reject_reason"] = reason


@dataclass
class Fill:
    """Execution report for an order (entry of a binary contract)."""

    order_id: str
    asset: str
    side: Side
    price: float                         # entry strike
    amount: float                        # stake
    payout: float
    ts: float = field(default_factory=timex.now)
    fee: float = 0.0
    slippage: float = 0.0
    broker_id: str = ""
    id: str = field(default_factory=lambda: _new_id("fill"))

    @property
    def gross_payout(self) -> float:
        """Total return if the contract wins (stake + profit)."""
        return self.amount * (1.0 + self.payout)


@dataclass
class Settlement:
    """Binary contract result."""

    fill_id: str
    order_id: str
    asset: str
    side: Side
    strike: float
    expiry_price: float
    stake: float
    payout: float
    won: bool
    refunded: bool = False
    salvage: float = 0.0
    ts: float = field(default_factory=timex.now)
    id: str = field(default_factory=lambda: _new_id("set"))

    @property
    def pnl(self) -> float:
        if self.refunded:
            return self.salvage
        return (self.stake * self.payout if self.won else -self.stake) + self.salvage

    @property
    def returned(self) -> float:
        """Cash returned to the account (stake + pnl — covers salvage marks)."""
        return self.stake + self.pnl


@dataclass
class Position:
    """An open binary contract (or a synthetic exposure record)."""

    fill: Fill
    expiry_ts: float
    strategy: str = ""
    id: str = field(default_factory=lambda: _new_id("pos"))
    label: str = ""

    @property
    def asset(self) -> str:
        return self.fill.asset

    @property
    def side(self) -> Side:
        return self.fill.side

    @property
    def stake(self) -> float:
        return self.fill.amount

    @property
    def strike(self) -> float:
        return self.fill.price

    def is_win(self, expiry_price: float) -> bool:
        if math.isclose(expiry_price, self.strike, rel_tol=0.0, abs_tol=1e-12):
            return False  # at-the-money => refund handled separately
        if self.side is Side.CALL:
            return expiry_price > self.strike
        if self.side is Side.PUT:
            return expiry_price < self.strike
        return False

    def is_atm(self, expiry_price: float) -> bool:
        return math.isclose(expiry_price, self.strike, rel_tol=0.0, abs_tol=1e-12)

    def unrealized(self, price: float) -> float:
        """Mark-to-market approximation for open contracts."""
        if self.is_win(price):
            return self.stake * self.fill.payout
        return -self.stake

    def settle(self, expiry_price: float) -> Settlement:
        atm = self.is_atm(expiry_price)
        return Settlement(
            fill_id=self.fill.id,
            order_id=self.fill.order_id,
            asset=self.asset,
            side=self.side,
            strike=self.strike,
            expiry_price=expiry_price,
            stake=self.stake,
            payout=self.fill.payout,
            won=self.is_win(expiry_price) and not atm,
            refunded=atm,
        )


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
@dataclass
class AccountSnapshot:
    """Point-in-time account state."""

    balance: float
    equity: float
    margin_used: float
    open_positions: int
    ts: float = field(default_factory=timex.now)
    currency: str = "USD"
    peak_balance: float = 0.0
    daily_pnl: float = 0.0

    @property
    def drawdown(self) -> float:
        if self.peak_balance <= 0:
            return 0.0
        return max(0.0, (self.peak_balance - self.balance) / self.peak_balance)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "margin_used": round(self.margin_used, 2),
            "open_positions": self.open_positions,
            "ts": self.ts,
            "currency": self.currency,
            "peak_balance": round(self.peak_balance, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "drawdown": round(self.drawdown, 5),
        }


@dataclass
class TradeRecord:
    """Flattened record for the journal/UI after settlement."""

    settlement: Settlement
    strategy: str = ""
    regime: str = ""
    duration_seconds: float = 0.0
    tags: Tuple[str, ...] = ()

    @property
    def pnl(self) -> float:
        return self.settlement.pnl

    @property
    def won(self) -> bool:
        return self.settlement.won

    def to_dict(self) -> Dict[str, Any]:
        s = self.settlement
        return {
            "id": s.id,
            "ts": s.ts,
            "asset": s.asset,
            "side": s.side.value,
            "strike": s.strike,
            "expiry_price": s.expiry_price,
            "stake": s.stake,
            "payout": s.payout,
            "won": s.won,
            "refunded": s.refunded,
            "pnl": round(self.pnl, 4),
            "strategy": self.strategy,
            "regime": self.regime,
        }


def summarize_trades(trades: Sequence[TradeRecord]) -> Dict[str, Any]:
    wins = [t for t in trades if t.won and not t.settlement.refunded]
    losses = [t for t in trades if not t.won and not t.settlement.refunded]
    total = len([t for t in trades if not t.settlement.refunded])
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    return {
        "count": total,
        "wins": len(wins),
        "losses": len(losses),
        "refunds": len([t for t in trades if t.settlement.refunded]),
        "win_rate": (len(wins) / total) if total else 0.0,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "net": gross_win - gross_loss,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
    }


__all__ = [
    "Tick",
    "Candle",
    "candles_to_series",
    "Signal",
    "FLAT_SIGNAL",
    "confidence_to_strength",
    "Order",
    "Fill",
    "Settlement",
    "Position",
    "AccountSnapshot",
    "TradeRecord",
    "summarize_trades",
]
