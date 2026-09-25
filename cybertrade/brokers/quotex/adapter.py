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
        self.allow_orders = allow_orders  # safety rail: False = data only
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
        # Reuse an already-open wire (Phase-30: no ghost sockets from
        # double-connect); open one only when actually down.
        if not getattr(self.api, "connected", False):
            self.api.connect()
        self.reconcile_venue()
        self._notify("connect", self.name)

    def reconcile_venue(self) -> int:
        """Adopt venue-open contracts the engine doesn't know (Phase-30).

        Boot pulls the portfolio wire and folds in any open contract with
        enough metadata (id + asset + plausible expiry) that we did not
        place this process — crash restarts and multi-tab sessions stay
        reconciled.  Orphans without expiry metadata are logged loudly for
        manual review rather than guessed at.  Returns contracts adopted.
        """
        opener = getattr(self.api, "open_trades", None)
        if opener is None:
            return 0
        try:
            trades = list(opener() or [])
        except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
            log.debug("venue portfolio pull failed: %s", exc)
            return 0
        now = timex.now()
        known_ids = {p.fill.broker_id for p in self._positions.values()}
        known_ids.update(self._by_request.keys())
        adopted = 0
        for t in trades:
            rid = t.order_id or t.request_id
            if not rid or rid in known_ids:
                continue
            if t.request_id and t.request_id in known_ids:
                continue
            expiry = _venue_expiry(t, now)
            if expiry <= now - 60.0:
                log.warning(
                    "venue-open orphan %s %s amount=%s — no usable expiry "
                    "metadata; manual review required", rid, t.asset, t.amount,
                )
                continue
            side = Side.CALL if (t.action or "call").lower() in ("call", "buy") else Side.PUT
            fill = Fill(
                order_id=f"venue:{rid}",
                asset=t.asset or "UNKNOWN",
                side=side,
                price=t.open_price,
                amount=t.amount,
                payout=t.payout if 0 < t.payout < 1 else 0.85,
                ts=now,
                broker_id=rid,
            )
            pos = Position(
                fill=fill,
                expiry_ts=expiry,
                strategy="venue",
                label="reconciled",
            )
            with self._lock:
                self._positions[pos.id] = pos
                self._by_request[rid] = fill.id
                if t.request_id:
                    self._by_request[t.request_id] = fill.id
            adopted += 1
            log.info(
                "reconciled venue-open %s %s x%s exp=%.0f",
                rid, t.asset, t.amount, expiry,
            )
        if adopted:
            self._notify("reconcile", {"adopted": adopted})
        return adopted

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
            raise OrderRejected("live orders are disabled on this wire",
                                 code="ORDERS_DISABLED")
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


def _venue_expiry(result: QXOrderResult, now: float) -> float:
    """Best-effort expiry timestamp from venue portfolio rows.

    Community payloads carry the close timestamp under assorted keys
    (``time`` on orders/open echoes expiry).  Accept seconds or
    milliseconds; return 0 when nothing plausible is present (caller then
    refuses to guess and logs the orphan for manual review).
    """
    raw = result.raw or {}

    def _scan(obj: object) -> float:
        if isinstance(obj, dict):
            for key in ("time", "expiry", "expiryTime", "closeTime", "endTime",
                        "duration", "expireAt"):
                if key in obj and key != "duration":
                    try:
                        val = float(obj[key])
                    except (TypeError, ValueError):
                        continue
                    if val > 1e11:  # milliseconds
                        val /= 1000.0
                    if now - 60.0 <= val <= now + 86400.0 * 2.0:
                        return val
            nested = obj.get("data")
            if isinstance(nested, dict):
                return _scan(nested)
        return 0.0

    found = _scan(raw)
    if found:
        return found
    # duration-only rows: open now + duration is too loose to trust — refuse
    return 0.0


__all__ = ["QuotexBroker"]
