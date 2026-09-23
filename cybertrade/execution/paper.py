"""Paper broker: faithful binary-option simulation with friction models.

Models payout variance per asset, latency, slippage, ATM refunds, and balance
accounting.  This is the default venue — live trading must be an explicit
operator decision.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from typing import Dict, List, Optional, Sequence

from ..constants import ASSET_CATALOG, OrderStatus, OrderType, Side
from ..data.models import (
    AccountSnapshot,
    Fill,
    Order,
    Position,
    Settlement,
    Tick,
)
from ..exceptions import OrderRejected
from ..utils import timex
from .broker import Broker

log = logging.getLogger("cybertrade.paper")


class PaperBroker(Broker):
    def __init__(
        self,
        starting_balance: float = 1000.0,
        default_payout: float = 0.85,
        latency_ms: int = 150,
        slippage_bps: float = 0.5,
        seed: int = 42,
        fill_delay: bool = False,
        salvage_rate: float = 0.25,
    ) -> None:
        super().__init__()
        self._balance = float(starting_balance)
        self._starting = float(starting_balance)
        self._peak = float(starting_balance)
        self.default_payout = default_payout
        self.latency_ms = latency_ms
        self.slippage_bps = slippage_bps
        self._rng = random.Random(seed)
        self._positions: Dict[str, Position] = {}
        self._settled: List[Settlement] = []
        self._pending: List[Settlement] = []
        self.salvage_rate = float(salvage_rate)
        self._prices: Dict[str, Tick] = {}
        self._connected = False
        self._daily_start = self._balance
        self._day = timex.next_daily_boundary(timex.now()) - 86400
        fill_delay = fill_delay  # reserved for async fills

    # -- venue -------------------------------------------------------------
    @property
    def name(self) -> str:
        return "PAPER-SIM"

    def connect(self) -> None:
        self._connected = True
        log.info("paper broker connected balance=%.2f", self._balance)
        self._notify("connect", self.name)

    def disconnect(self) -> None:
        self._connected = False
        self._notify("disconnect", self.name)

    @property
    def connected(self) -> bool:
        return self._connected

    # -- quotes ------------------------------------------------------------
    def on_tick(self, tick: Tick) -> None:
        self._prices[tick.asset] = tick

    def last_price(self, asset: str) -> Optional[float]:
        tick = self._prices.get(asset)
        return tick.price if tick else None

    def payout_for(self, asset: str, expiry_seconds: int) -> float:
        meta = ASSET_CATALOG.get(asset)
        base = float(meta["payout"]) if meta else self.default_payout
        # longer expirations pay a touch less on most venues
        if expiry_seconds >= 1800:
            base -= 0.03
        elif expiry_seconds >= 300:
            base -= 0.01
        jitter = self._rng.uniform(-0.01, 0.01)
        return max(0.5, min(0.95, base + jitter))

    # -- trading -----------------------------------------------------------
    def submit(self, order: Order) -> Fill:
        if not self._connected:
            raise OrderRejected("paper broker not connected", code="NO_CONN")
        if order.amount <= 0:
            raise OrderRejected("stake must be positive", code="BAD_STAKE")
        if order.amount > self._balance:
            raise OrderRejected("insufficient balance", code="NO_FUNDS")
        if not order.side.is_trade:
            raise OrderRejected("order side must be call/put", code="BAD_SIDE")
        tick = self._prices.get(order.asset)
        if tick is None:
            raise OrderRejected(f"no quote for {order.asset}", code="NO_QUOTE")

        self._roll_day()
        latency = self.latency_ms / 1000.0
        if latency > 0:
            time.sleep(min(latency, 0.25))

        slip = tick.price * self.slippage_bps / 10_000.0
        slip *= 1.0 + self._rng.uniform(-0.25, 0.25)
        strike = tick.price + (slip if order.side is Side.CALL else -slip)
        payout = order.payout if 0 < order.payout < 1 else self.payout_for(
            order.asset, order.expiry_seconds
        )

        fill = Fill(
            order_id=order.id,
            asset=order.asset,
            side=order.side,
            price=strike,
            amount=order.amount,
            payout=payout,
            ts=timex.now(),
            slippage=slip,
        )
        order.status = OrderStatus.ACCEPTED
        order.broker_id = fill.id
        self._balance -= order.amount  # escrow the stake

        position = Position(
            fill=fill,
            expiry_ts=fill.ts + order.expiry_seconds,
            strategy=order.strategy or order.tag,
            label=order.tag,
        )
        position.id = f"pos-{order.id}"
        self._positions[position.id] = position
        log.debug(
            "paper fill %s %s stake=%.2f strike=%.5f payout=%.2f",
            order.asset,
            order.side.value,
            order.amount,
            strike,
            payout,
        )
        self._notify("fill", fill)
        return fill

    def settle_due(self, now: Optional[float] = None) -> List[Settlement]:
        now = now if now is not None else timex.now()
        out: List[Settlement] = []
        with self._lock:
            if self._pending:
                out.extend(self._pending)
                self._pending.clear()
            due = [p for p in self._positions.values() if p.expiry_ts <= now]
            for pos in due:
                price = self.last_price(pos.asset)
                if price is None:
                    price = pos.strike  # frozen feed -> refund semantics via ATM
                settlement = pos.settle(price)
                self._balance += settlement.returned
                if self._balance > self._peak:
                    self._peak = self._balance
                self._settled.append(settlement)
                del self._positions[pos.id]
                out.append(settlement)
                self._notify("settle", settlement)
        return out

    def force_settle(self, position_id: str, expiry_price: float) -> Settlement:
        with self._lock:
            pos = self._positions.pop(position_id)
            settlement = pos.settle(expiry_price)
            self._balance += settlement.returned
            self._settled.append(settlement)
            self._notify("settle", settlement)
            return settlement

    def close_position(self, position_id: str) -> bool:
        """Lifeboat: liquidate at the mark minus the salvage haircut.

        Losing contracts salvage ``salvage_rate`` of stake back (the venue's
        sell-back quote); winners bank the modeled payout.  Cash moves now;
        the settlement rides :meth:`settle_due` like an expiry — no special
        paths downstream.
        """
        with self._lock:
            pos = self._positions.pop(position_id, None)
            if pos is None:
                return False
            price = self.last_price(pos.asset)
            if price is None:
                self._positions[position_id] = pos  # unmarkable — leave it
                return False
            settlement = pos.settle(price)
            if not settlement.won and not settlement.refunded:
                settlement.salvage = settlement.stake * self.salvage_rate
            self._balance += settlement.returned
            if self._balance > self._peak:
                self._peak = self._balance
            self._settled.append(settlement)
            self._pending.append(settlement)
            self._notify("close", settlement)
            return True

    # -- account -----------------------------------------------------------
    def account(self) -> AccountSnapshot:
        with self._lock:
            margin = sum(p.stake for p in self._positions.values())
            equity = self._balance + margin
            return AccountSnapshot(
                balance=self._balance,
                equity=equity,
                margin_used=margin,
                open_positions=len(self._positions),
                peak_balance=self._peak,
                daily_pnl=self._balance - self._daily_start,
            )

    def open_positions(self) -> List[Position]:
        with self._lock:
            return list(self._positions.values())

    def settlements(self, limit: int = 50) -> List[Settlement]:
        return self._settled[-limit:]

    def reset(self, balance: Optional[float] = None) -> None:
        with self._lock:
            self._balance = float(balance if balance is not None else self._starting)
            self._starting = self._balance
            self._peak = self._balance
            self._daily_start = self._balance
            self._positions.clear()
            self._settled.clear()

    def _roll_day(self) -> None:
        day = timex.next_daily_boundary(timex.now()) - 86400
        if day != self._day:
            self._day = day
            self._daily_start = self._balance


class DryRunBroker(PaperBroker):
    """Paper broker that refuses withdrawals of value — audit mode.

    Identical fills to :class:`PaperBroker` but tags every order and exposes
    what *would* have been sent to a live venue, for operator review.
    """

    def submit(self, order: Order) -> Fill:
        order.meta["dry_run"] = True
        order.tag = (order.tag + " | DRYRUN").strip(" |")
        fill = super().submit(order)
        log.info(
            "DRY-RUN order captured: %s %s %.2f @ %.5f exp=%ds",
            fill.asset,
            order.side.value,
            fill.amount,
            fill.price,
            order.expiry_seconds,
        )
        return fill

    @property
    def name(self) -> str:
        return "DRY-RUN"


__all__ = ["PaperBroker", "DryRunBroker"]
