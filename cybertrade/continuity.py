"""Crash checkpoints for runtime engines, not backtests or score-bay jobs.

The OMS serializes a durable BEFORE (in-flight intent) and AFTER (commit)
record around each mutation. An interrupted operation is held for review,
never replayed. This is not a distributed transaction with Quotex: live
balances remain venue-owned, and unresolved live exposure requires manual
reconciliation. Paper cash, open contracts, pending salvage, order metadata,
and the risk governor are validated as one book before any restore.
"""
from __future__ import annotations

import logging
import math
import os
import time
from collections import Counter
from typing import Any, Dict

from .constants import EngineState, OrderStatus
from .execution.paper import PaperBroker
from .risk.manager import _cluster_of
from .statestore import (StateError, StateLease, load_continuity, pack_order,
                         save_continuity, text, unpack_order)

log = logging.getLogger("cybertrade.continuity")


class Continuity:
    def __init__(self, engine, path: str) -> None:
        self.engine = engine
        self.path = os.path.realpath(os.path.expanduser(path))
        self.lease = StateLease(self.path)
        self.active = False
        self.restored = False
        self.fault = ""
        self.saved_ts = 0.0
        self.in_flight = ""
        self.scope = None  # pinned at boot; runtime account swaps require a new engine

    def acquire(self) -> None:
        self.lease.acquire()
        self.active = True

    def release(self) -> None:
        self.lease.release()
        self.active = False

    def _scope(self) -> dict:
        broker = self.engine.broker
        scope = {"mode": self.engine.config.broker.mode, "broker": broker.name}
        if not isinstance(broker, PaperBroker):
            # Never bind financial memory to a rotating session cookie.
            api = getattr(broker, "api", None)
            session_uid = getattr(getattr(api, "session", None), "user_id", "")
            balance_uid = getattr(getattr(api, "balance", None), "user_id", "")
            if session_uid and balance_uid and str(session_uid) != str(balance_uid):
                raise StateError("venue session/balance account identities disagree")
            uid = balance_uid or session_uid
            if not uid:
                raise StateError("live continuity needs an authenticated venue user ID")
            scope["account"] = str(uid)
        if self.scope is not None and scope != self.scope:
            raise StateError("runtime mode/account changed; stop and reconcile before switching")
        return scope

    def _validate(self, data: dict):
        if data["scope"] != self._scope():
            raise StateError("continuity belongs to a different mode/account; use a separate path")
        text(data["pending_operation"], "pending_operation")
        risk, _ = self.engine.risk.decode_state(data["risk"])
        ledger = self.engine.oms.ledger.decode_state(data["ledger"])
        if not math.isclose(risk.current_balance, ledger["balance"], abs_tol=1e-7):
            raise StateError("risk/ledger cash mismatch")
        if not isinstance(data["orders"], list):
            raise StateError("invalid saved order registry")
        orders = [unpack_order(row) for row in data["orders"]]
        if len(orders) != len({o.id for o in orders}):
            raise StateError("duplicate saved order")
        if not isinstance(self.engine.broker, PaperBroker):
            if data["paper"] is not None or orders:
                raise StateError("paper executions must never be restored into a live venue")
            return None, orders

        book = PaperBroker.decode_state(data["paper"])
        self._validate_book(book, ledger, risk, orders)
        return book, orders

    @staticmethod
    def _validate_book(book, ledger, risk, orders):
        items = [(p.fill.order_id, p.asset, p.stake, p.side, p.fill.payout)
                 for p in book["positions"]]
        items += [(s.order_id, s.asset, s.stake, s.side, s.payout) for s in book["pending"]]
        registry = {o.id: o for o in orders}
        if set(registry) != {row[0] for row in items}:
            raise StateError("incomplete order/position linkage")
        for oid, asset, stake, side, payout in items:
            o = registry[oid]
            if (o.asset != asset or o.side != side or abs(o.amount - stake) > 1e-7
                    or abs(o.payout - payout) > 1e-7
                    or o.status not in (OrderStatus.ACCEPTED, OrderStatus.SUBMITTED)):
                raise StateError("order/fill disagreement")
        for p in book["positions"]:
            if abs(p.expiry_ts - p.fill.ts - registry[p.fill.order_id].expiry_seconds) > 1e-6:
                raise StateError("order/contract expiry disagreement")
        # Pending salvage already moved broker cash, but not ledger cash.
        expected_cash = ledger["balance"] + sum(s.returned for s in book["pending"])
        if not math.isclose(book["balance"], expected_cash, abs_tol=1e-7):
            raise StateError("paper/ledger cash mismatch")
        assets = Counter(o.asset for o in orders)
        clusters = Counter(o.meta.get("cluster") or _cluster_of(o.asset) for o in orders)
        if (risk.open_count != len(orders)
                or abs(risk.open_stake_sum - sum(o.amount for o in orders)) > 1e-7
                or {k: v for k, v in risk.open_assets.items() if v} != dict(assets)
                or {k: v for k, v in risk.open_clusters.items() if v} != dict(clusters)):
            raise StateError("saved exposure does not match the paper book")

    def _snapshot(self, operation: str = "") -> dict:
        engine = self.engine
        # Lock order is OMS -> broker -> ledger -> risk everywhere here.
        with engine.oms._lock, engine.broker._lock, engine.oms.ledger._lock, engine.risk._lock:
            paper = engine.broker.export_state() if isinstance(engine.broker, PaperBroker) else None
            wanted = set()
            if paper is not None:
                wanted = {p["fill"]["order_id"] for p in paper["positions"]}
                wanted.update(s["order_id"] for s in paper["pending"])
            data = {
                "scope": self._scope(), "pending_operation": operation,
                "risk": engine.risk.export_state(), "ledger": engine.oms.ledger.export_state(),
                "paper": paper,
                "orders": [pack_order(engine.oms.orders[oid]) for oid in sorted(wanted)],
            }
            self._validate(data)
            return data

    def restore(self, now: float = None) -> None:
        now = time.time() if now is None else now
        try:
            self.scope = self._scope()  # validate and pin identity even on first boot
            data = load_continuity(self.path)
            if not data:
                return
            if data["saved_ts"] > now + 60:
                raise StateError("continuity is from the future; check the system clock")
            book, orders = self._validate(data)
            engine = self.engine
            # All sections passed validation before any in-memory mutation.
            if book is not None:
                engine.broker.restore_state(data["paper"], now=now)
                engine.oms.ledger.restore_state(data["ledger"])
                engine.oms.orders = {o.id: o for o in orders}
                engine.oms.fills = {p.fill.id: p.fill for p in book["positions"]}
            else:
                # Never credit local cached cash into a venue account.
                account = engine.broker.account()
                engine.oms.ledger.starting_balance = data["ledger"]["starting_balance"]
                engine.oms.ledger.balance = account.balance
                engine.oms.ledger.peak = max(data["ledger"]["peak"], account.balance,
                                            account.peak_balance)
            engine.risk.restore_state(data["risk"], now=now)
            engine.risk.update_balance(engine.oms.ledger.balance)
            self.saved_ts = data["saved_ts"]
            self.restored = True
            self.in_flight = data["pending_operation"]
            if self.in_flight:
                raise StateError(f"interrupted {self.in_flight}; review the saved book before recovery")
            if book is None and (engine.risk.state.open_count or engine.broker.open_positions()):
                raise StateError("live exposure requires venue reconciliation; no order replay performed")
            log.info("continuity restored: %s", self.path)
        except (StateError, KeyError, TypeError) as exc:
            self._fail(str(exc))

    def _fail(self, reason: str) -> None:
        self.fault = reason
        engine = self.engine
        engine.risk.state.kill = True
        engine.risk.state.kill_reason = "recovery hold: " + reason
        engine.state = EngineState.KILL
        engine._running = False
        engine.health.note_message("RECOVERY HOLD: " + reason)
        log.error("RECOVERY HOLD: %s (state file preserved)", reason)

    def assert_ready(self) -> None:
        if not self.active:
            raise StateError("continuity is not open")
        if self.fault:
            raise StateError(self.fault)

    def begin(self, operation: str) -> None:
        self.assert_ready()
        self._write(operation)

    def abort(self, operation: str) -> None:
        # Preserve the BEFORE checkpoint, including its uncertainty marker.
        self._fail(f"interrupted {operation}; restart requires review")

    def commit(self) -> None:
        self.assert_ready()
        self._write("")

    def _write(self, operation: str) -> None:
        try:
            with self.engine.oms._lock:
                payload = self._snapshot(operation)
                if not save_continuity(self.path, payload):
                    raise StateError("continuity write failed; trading stopped")
                self.saved_ts = time.time()
                self.in_flight = operation
        except (KeyError, TypeError, ValueError, OSError) as exc:
            self._fail(str(exc))
            raise StateError(self.fault) from exc

    def status(self) -> Dict[str, Any]:
        broker = self.engine.broker
        held = broker.recovery_holds() if isinstance(broker, PaperBroker) else []
        return {
            "enabled": True, "active": self.active, "restored": self.restored,
            "blocked": bool(self.fault or held),
            "reason": self.fault or ("offline expiry needs paper review" if held else ""),
            "held_positions": held, "saved_ts": self.saved_ts,
            "in_flight": self.in_flight,
        }
