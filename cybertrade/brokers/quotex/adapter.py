"""Broker adapter: wire :class:`QuotexAPI` into the engine's Broker interface."""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from ...constants import OrderStatus, Side
from ...data.models import AccountSnapshot, Fill, Order, Position, Settlement, Tick
from ...exceptions import BrokerConnectionError, OrderRejected
from ...execution.broker import Broker
from ...utils import timex
from .api import QuotexAPI
from .models import QXOrderRequest, QXOrderResult

log = logging.getLogger("cybertrade.qx.adapter")


class QuotexBroker(Broker):
    """Production adapter.  Binary contracts settle locally at expiry using
    streamed quotes (the venue settles server-side too; we reconcile PnL from
    its order events when they arrive).
    """

    def __init__(self, api: QuotexAPI, allow_orders: bool = True,
                 salvage_rate: float = 0.25) -> None:
        super().__init__()
        self.api = api
        self.allow_orders = allow_orders  # dry-run rail: False = data only
        self.salvage_rate = float(salvage_rate)
        self._positions: Dict[str, Position] = {}
        self._fills: Dict[str, Fill] = {}
        self._by_request: Dict[str, str] = {}
        self._settled: List[Settlement] = []
        self._pending: List[Settlement] = []
        self._balance_override: Optional[float] = None
        self.api.add_listener(self._on_api_event)

    @property
    def name(self) -> str:
        return "QUOTEX-" + ("DEMO" if self.api.demo else "LIVE")

    def connect(self) -> None:
        if not self.api.session.ssid:
            raise BrokerConnectionError("QuotexBroker requires an authenticated API session")
        self.api.connect()
        self._notify("connect", self.name)

    def disconnect(self) -> None:
        self.api.close()
        self._notify("disconnect", self.name)

    @property
    def connected(self) -> bool:
        return self.api.connected

    # -- market ------------------------------------------------------------
    def on_tick(self, tick: Tick) -> None:
        pass  # api keeps its own quote cache

    def last_price(self, asset: str) -> Optional[float]:
        return self.api.last_price(asset)

    def payout_for(self, asset: str, expiry_seconds: int) -> float:
        return self.api.payout_for(asset, expiry_seconds)

    # -- trading -----------------------------------------------------------
    def submit(self, order: Order) -> Fill:
        if not self.allow_orders:
            raise OrderRejected("dry-run: live orders are disabled", code="DRY_RUN")
        if not self.connected:
            raise OrderRejected("not connected to Quotex", code="NO_CONN")
        price = self.last_price(order.asset)
        if price is None:
            raise OrderRejected(f"no quote for {order.asset}", code="NO_QUOTE")

        req: QXOrderRequest = self.api.buy(
            asset=order.asset,
            amount=order.amount,
            action=order.side.value,
            duration=order.expiry_seconds,
        )
        fill = Fill(
            order_id=order.id,
            asset=order.asset,
            side=order.side,
            price=price,
            amount=order.amount,
            payout=order.payout or self.payout_for(order.asset, order.expiry_seconds),
            ts=timex.now(),
            broker_id=req.request_id,
        )
        order.status = OrderStatus.SUBMITTED
        order.broker_id = req.request_id
        self._fills[fill.id] = fill
        self._by_request[req.request_id] = fill.id
        position = Position(
            fill=fill,
            expiry_ts=fill.ts + order.expiry_seconds,
            strategy=order.strategy,
            label=order.tag,
        )
        self._positions[position.id] = position
        self._notify("fill", fill)
        return fill

    def settle_due(self, now: Optional[float] = None) -> List[Settlement]:
        now = now if now is not None else timex.now()
        out: List[Settlement] = []
        with self._lock:
            if self._pending:
                out.extend(self._pending)
                self._pending.clear()
            for pos in list(self._positions.values()):
                if pos.expiry_ts > now:
                    continue
                price = self.last_price(pos.asset)
                if price is None:
                    continue
                settlement = pos.settle(price)
                self._settled.append(settlement)
                del self._positions[pos.id]
                out.append(settlement)
                self._notify("settle", settlement)
        return out

    def force_settle(self, position_id: str, expiry_price: float) -> Settlement:
        with self._lock:
            pos = self._positions.pop(position_id)
            settlement = pos.settle(expiry_price)
            self._settled.append(settlement)
            self._notify("settle", settlement)
            return settlement

    def close_position(self, position_id: str) -> bool:
        """Lifeboat: venue sell-back (``sell_option``) + local salvage mark.

        The wire is best-effort — if the venue refuses or is unreachable we
        still salvage locally at the mark (a dropped wire must not strand
        risk).  Settlement rides :meth:`settle_due` like an expiry.
        """
        with self._lock:
            pos = self._positions.get(position_id)
            if pos is None:
                return False
            broker_id = pos.fill.broker_id
        try:
            self.api.sell_option(broker_id)
        except Exception as exc:  # noqa: BLE001 — salvage locally anyway
            log.warning("venue sell-back failed (%s) — local salvage mark", exc)
        price = self.last_price(pos.asset)
        if price is None:
            return False
        settlement = pos.settle(price)
        if not settlement.won and not settlement.refunded:
            settlement.salvage = settlement.stake * self.salvage_rate
        with self._lock:
            self._positions.pop(position_id, None)
            self._settled.append(settlement)
            self._pending.append(settlement)
        self._notify("close", settlement)
        return True

    # -- account -----------------------------------------------------------
    def account(self) -> AccountSnapshot:
        snap = self.api.account_snapshot()
        snap.open_positions = len(self._positions)
        return snap

    def open_positions(self) -> List[Position]:
        with self._lock:
            return list(self._positions.values())

    # -- venue events ------------------------------------------------------
    def _on_api_event(self, kind: str, payload: object) -> None:
        if kind == "order" and isinstance(payload, QXOrderResult):
            self._reconcile(payload)
        self._notify(kind, payload)

    def _reconcile(self, result: QXOrderResult) -> None:
        """Fold venue settlement events into local position accounting."""
        fill_id = self._by_request.get(result.request_id or "")
        if not fill_id:
            return
        for pos_id, pos in list(self._positions.items()):
            if pos.fill.id != fill_id:
                continue
            if result.status.lower() in ("open", ""):
                return
            refunded = not (result.won or result.lost)
            settlement = Settlement(
                fill_id=fill_id,
                order_id=pos.fill.order_id,
                asset=pos.asset,
                side=pos.side,
                strike=pos.strike,
                expiry_price=result.close_price or pos.strike,
                stake=pos.stake,
                payout=pos.fill.payout,
                won=result.won or (result.profit > 0),
                refunded=refunded,
                ts=timex.now(),
            )
            with self._lock:
                self._settled.append(settlement)
                self._positions.pop(pos_id, None)
            self._notify("settle", settlement)
            return


__all__ = ["QuotexBroker"]
