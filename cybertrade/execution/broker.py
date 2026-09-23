"""Broker interface every venue adapter implements (paper, dry-run, Quotex)."""

from __future__ import annotations

import abc
import threading
from typing import Callable, Dict, List, Optional, Sequence

from ..data.models import AccountSnapshot, Fill, Order, Position, Settlement, Tick
from ..exceptions import ExecutionError


class Broker(abc.ABC):
    """Venue abstraction.

    Binary options semantics: submitting a binary order fills immediately at
    the current strike and creates a :class:`Position` that settles at
    ``fill.ts + order.expiry_seconds``.  The OMS polls :meth:`settle_due`.
    """

    def __init__(self) -> None:
        self._listeners: List[Callable[[str, object], None]] = []
        self._lock = threading.RLock()

    # -- events ------------------------------------------------------------
    def add_listener(self, callback: Callable[[str, object], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def _notify(self, kind: str, payload: object) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    # -- lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    # -- market ------------------------------------------------------------
    @abc.abstractmethod
    def last_price(self, asset: str) -> Optional[float]: ...

    @abc.abstractmethod
    def payout_for(self, asset: str, expiry_seconds: int) -> float: ...

    # -- trading -----------------------------------------------------------
    @abc.abstractmethod
    def submit(self, order: Order) -> Fill: ...

    @abc.abstractmethod
    def account(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    def open_positions(self) -> List[Position]: ...

    @abc.abstractmethod
    def settle_due(self, now: Optional[float] = None) -> List[Settlement]: ...

    def cancel(self, order_id: str) -> bool:  # binaries cannot be cancelled
        return False

    def close_position(self, position_id: str) -> bool:
        """Liquidate one open contract before expiry (venue sell-back /
        salvage mark).  False = unsupported or unknown position."""
        return False

    def close(self) -> None:
        self.disconnect()


__all__ = ["Broker"]
