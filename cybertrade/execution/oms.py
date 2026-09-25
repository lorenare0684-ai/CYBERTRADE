"""Order Management System: routing, position registry, settlement loop."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import wraps
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


def _transactional(operation):
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            with self.transaction(operation):
                return fn(self, *args, **kwargs)
        return wrapped
    return decorate


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
        self._continuity = None  # attached only by durable runtime engines
        self._transaction_depth = 0
        self.orders: Dict[str, Order] = {}
        self.fills: Dict[str, Fill] = {}
        self.settlements: List[Settlement] = []
        self.trades: List[TradeRecord] = []
        self._on_settle: List[Callable[[TradeRecord], None]] = []

    @contextmanager
    def transaction(self, operation: str):
        """Serialize mutations and bracket them with durable intent/commit.

        A crash between the writes leaves an in-flight marker. On restart
        we hold for review rather than replay an uncertain execution.
        """
        with self._lock:
            outer = self._transaction_depth == 0
            if outer and self._continuity is not None:
                self._continuity.begin(operation)
            self._transaction_depth += 1
            try:
                yield
            except BaseException:
                if outer and self._continuity is not None:
                    self._continuity.abort(operation)
                raise
            else:
                if outer and self._continuity is not None:
                    self._continuity.commit()
            finally:
                self._transaction_depth -= 1

    # -- hooks -------------------------------------------------------------
    def add_settle_listener(self, fn: Callable[[TradeRecord], None]) -> None:
        self._on_settle.append(fn)

    # -- trading -----------------------------------------------------------
    @_transactional("submit")
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
        self.risk.update_balance(self.ledger.balance)
        default_bus.publish(Topic.ORDER_SUBMIT, order, source="oms")
        default_bus.publish(Topic.FILL, fill, source="oms")
        return order

    # -- settlement --------------------------------------------------------
    @_transactional("settle")
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
                    now=now,
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

    @_transactional("close")
    def close_position(self, position_id: str) -> bool:
        """Keep broker salvage + pending delivery + ledger credit one transaction."""
        if not self.broker.close_position(position_id):
            return False
        self.pump()
        return True

    def resolve_recovery(self, position_id: str, expiry_price: float) -> bool:
        """Removed with paper mode — a live venue owns its own settlements.

        Kept as an explicit refusal so old callers get ``False`` (never a
        fabricated settlement at a venue) instead of an AttributeError.
        """
        return False  # never fabricate a settlement at a live venue

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

    @_transactional("sync")
    def sync_balance_from_broker(self) -> None:
        """Align ledger cash with the venue (live reconnect)."""
        snap = self.broker.account()
        delta = snap.balance - self.ledger.balance
        if abs(delta) > 1e-9:
            self.ledger.deposit(delta, ref="sync", note="broker sync")
        self.ledger.peak = max(self.ledger.peak, self.ledger.balance)
        self.risk.update_balance(self.ledger.balance)


__all__ = ["OrderManager"]
