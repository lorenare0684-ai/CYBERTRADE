"""Order Management System: routing, position registry, settlement loop."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from ..constants import OrderStatus, Side
from ..data.models import (
    AccountSnapshot,
    Fill,
    Order,
    Position,
    Settlement,
    TradeRecord,
)
from ..events import Topic, default_bus
from ..exceptions import OrderRejected
from ..risk.manager import RiskManager
from ..utils import timex
from .broker import Broker
from .ledger import Ledger

log = logging.getLogger("cybertrade.oms")


class OrderManager:
    """Single gateway between strategies and a venue.

    Responsibilities:
    - risk authorization (nothing reaches the broker without it),
    - order/fill/settlement bookkeeping on the ledger,
    - strategy attribution for every closed contract,
    - a periodic :meth:`pump` that settles expirations.
    """

    def __init__(
        self,
        broker: Broker,
        risk: RiskManager,
        ledger: Optional[Ledger] = None,
    ) -> None:
        self.broker = broker
        self.risk = risk
        self.ledger = ledger or Ledger(risk.config.starting_balance)
        self._lock = threading.RLock()
        self.orders: Dict[str, Order] = {}
        self.fills: Dict[str, Fill] = {}
        self.settlements: List[Settlement] = []
        self.trades: List[TradeRecord] = []
        self._on_settle: List[Callable[[TradeRecord], None]] = []

    # -- hooks -------------------------------------------------------------
    def add_settle_listener(self, fn: Callable[[TradeRecord], None]) -> None:
        self._on_settle.append(fn)

    # -- trading -----------------------------------------------------------
    def submit(
        self,
        asset: str,
        side: Side,
        stake: float,
        expiry_seconds: int,
        payout: Optional[float] = None,
        confidence: float = 0.6,
        strategy: str = "",
        tag: str = "",
        cluster: str = "",
        regime: str = "",
        slippage_bps: float = 0.0,
        votes: tuple = (),
    ) -> Optional[Order]:
        """Authorize + route one binary order. Returns None when risk vetoes."""
        if not side.is_trade:
            return None
        payout = payout if payout is not None else self.broker.payout_for(asset, expiry_seconds)
        order = Order(
            asset=asset,
            side=side,
            amount=round(stake, 2),
            expiry_seconds=expiry_seconds,
            payout=payout,
            strategy=strategy,
            tag=tag,
            meta={"cluster": cluster or asset, "regime": regime,
                  "confidence": confidence, "votes": list(votes),
                  "slippage_bps": round(float(slippage_bps), 2)},
        )
        try:
            self.risk.authorize(
                asset=asset,
                side=side,
                stake=order.amount,
                payout=payout,
                confidence=confidence,
                strategy=strategy,
                cluster=cluster or asset,
            )
        except Exception as exc:  # RiskRejection expected path
            order.reject(str(exc))
            self.orders[order.id] = order
            default_bus.publish(Topic.ORDER_UPDATE, order, source="oms")
            return None

        try:
            self.ledger.stake(order.amount, ref=order.id, note=f"{asset} {side.value}")
            fill = self.broker.submit(order)
        except OrderRejected as exc:
            self.ledger.deposit(order.amount, ref=order.id, note="stake refund")
            order.reject(str(exc))
            self.orders[order.id] = order
            log.warning("broker rejected order: %s", exc)
            default_bus.publish(Topic.ORDER_UPDATE, order, source="oms")
            return None

        with self._lock:
            self.orders[order.id] = order
            self.fills[fill.id] = fill
        self.risk.on_open(order)
        default_bus.publish(Topic.ORDER_SUBMIT, order, source="oms")
        default_bus.publish(Topic.FILL, fill, source="oms")
        return order

    # -- settlement --------------------------------------------------------
    def pump(self, now: Optional[float] = None) -> List[TradeRecord]:
        """Settle everything due at *now*; safe to call at high frequency."""
        settlements = self.broker.settle_due(now)
        records: List[TradeRecord] = []
        for settlement in settlements:
            order = self.orders.get(settlement.order_id)
            strategy = order.strategy if order else ""
            regime = (order.meta.get("regime", "") if order else "")
            record = self.ledger.record_settlement(settlement, strategy=strategy, regime=regime)
            with self._lock:
                self.settlements.append(settlement)
                self.trades.append(record)
            if order is not None:
                self.risk.on_close(
                    order,
                    won=settlement.won and not settlement.refunded,
                    pnl=settlement.pnl,
                    balance=self.ledger.balance,
                )
            else:
                self.risk.update_balance(self.ledger.balance)
            default_bus.publish(Topic.SETTLE, record, source="oms")
            for fn in self._on_settle:
                try:
                    fn(record)
                except Exception:  # noqa: BLE001
                    log.exception("settle listener failed")
            records.append(record)
        balance = self.ledger.balance
        self.risk.update_balance(balance)
        default_bus.publish(Topic.BALANCE, self.account(), source="oms")
        return records

    # -- views -------------------------------------------------------------
    def account(self) -> AccountSnapshot:
        snap = self.broker.account()
        snap.balance = self.ledger.balance
        snap.equity = self.ledger.balance + snap.margin_used
        snap.peak_balance = self.ledger.peak
        return snap

    def open_positions(self) -> List[Position]:
        return self.broker.open_positions()

    def recent_trades(self, limit: int = 25) -> List[TradeRecord]:
        return self.trades[-limit:]

    def stats(self) -> dict:
        return self.ledger.summary()

    def sync_balance_from_broker(self) -> None:
        """Align ledger cash with the venue (live reconnect)."""
        snap = self.broker.account()
        delta = snap.balance - self.ledger.balance
        if abs(delta) > 1e-9:
            self.ledger.deposit(delta, ref="sync", note="broker sync")


__all__ = ["OrderManager"]
