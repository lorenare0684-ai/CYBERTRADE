"""Crash checkpoints for live runtime engines — no paper book exists here.

The OMS serializes a durable BEFORE (in-flight intent) and AFTER (commit)
record around each mutation. An interrupted operation is held for review,
never replayed. This is not a distributed transaction with Quotex: live
balances remain venue-owned, and unresolved live exposure requires manual
reconciliation. The risk governor, ledger, and order registry are validated
as one book before any restore; no local cash is ever credited into a venue
account.
"""
from __future__ import annotations

import logging
import math
import os
import time
from collections import Counter
from typing import Any, Dict

from .constants import EngineState, OrderStatus
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
        if data["paper"] is not None:
            # A saved paper execution must never be restored into a live venue.
            raise StateError("paper executions must never be restored into a live venue")
        assets = Counter(o.asset for o in orders)
        clusters = Counter(o.meta.get("cluster") or _cluster_of(o.asset) for o in orders)
        if (risk.open_count != len(orders)
                or abs(risk.open_stake_sum - sum(o.amount for o in orders)) > 1e-7
                or {k: v for k, v in risk.open_assets.items() if v} != dict(assets)
                or {k: v for k, v in risk.open_clusters.items() if v} != dict(clusters)):
            raise StateError("saved exposure does not match the live order registry")
        return None, orders

    def _snapshot(self, operation: str = "") -> dict:
        engine = self.engine
        # Lock order is OMS -> broker -> ledger -> risk everywhere here.
        with engine.oms._lock, engine.broker._lock, engine.oms.ledger._lock, engine.risk._lock:
            # The live order registry: exactly the orders whose contract the
            # venue still holds open.  Settled ones leave the book; a venue
            # that no longer lists a contract surfaces as unreconciled
            # exposure on the next boot instead of a local replay.
            open_ids = {getattr(pos.fill, "order_id", "")
                        for pos in engine.broker.open_positions()}
            data = {
                "scope": self._scope(), "pending_operation": operation,
                "risk": engine.risk.export_state(), "ledger": engine.oms.ledger.export_state(),
                "paper": None,
                "orders": [pack_order(o) for o in engine.oms.orders.values()
                           if o.id in open_ids],
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
            if engine.risk.state.open_count or engine.broker.open_positions():
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
        return {
            "enabled": True, "active": self.active, "restored": self.restored,
            "blocked": bool(self.fault),
            "reason": self.fault,
            "held_positions": [], "saved_ts": self.saved_ts,
            "in_flight": self.in_flight,
        }
