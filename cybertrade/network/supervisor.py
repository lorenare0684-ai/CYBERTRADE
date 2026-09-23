"""Bounded backoff reconnect supervision for venue links.

The socket layer never auto-reconnects by design — recovery is explicit,
bounded (``BrokerConfig.reconnect_max``), and observable
(:class:`~cybertrade.events.Topic` ``CONNECTION``).  The supervisor only
heals links that have been up: a venue that never connected is not a
dropped wire, and we do not hammer it.  After the attempt cap it gives up
loudly instead of retrying forever.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from ..utils import timex

log = logging.getLogger("cybertrade.net.supervisor")


class ReconnectSupervisor:
    """Poll-driven reconnect with exponential backoff and a hard attempt cap."""

    def __init__(
        self,
        api: Any,
        *,
        max_attempts: int = 12,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        resubscribe: Optional[Callable[[Any], None]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.api = api
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)
        self.resubscribe = resubscribe
        self.on_event = on_event
        self._seen_up = False
        self._attempts = 0
        self._next_try = 0.0
        self._given_up = False

    def _emit(self, kind: str, **payload: Any) -> None:
        log.info("supervisor %s %s", kind, payload)
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                log.exception("supervisor event handler crashed")

    def _do_resubscribe(self) -> None:
        if self.resubscribe is None:
            return
        try:
            self.resubscribe(self.api)
        except Exception:  # noqa: BLE001
            log.exception("resubscribe failed after reconnect")

    def sweep(self, now: Optional[float] = None) -> str:
        """Poll link health.

        Returns ``up`` | ``down``/``retrying`` | ``given-up`` | ``standby``
        (never seen up — a dead-at-boot link is not a dropped wire).
        """
        now = timex.now() if now is None else now
        try:
            connected = bool(self.api.connected)
        except Exception:  # noqa: BLE001
            connected = False

        if connected:
            if self._attempts or self._given_up:
                self._emit("reconnect", attempts=self._attempts)
                self._do_resubscribe()
            self._seen_up = True
            self._attempts = 0
            self._given_up = False
            self._next_try = 0.0
            return "up"

        if not self._seen_up:
            return "standby"
        if self._given_up:
            return "given-up"
        if now < self._next_try:
            return "retrying"

        try:
            self.api.connect()
        except Exception as exc:  # noqa: BLE001 — any failure is a failed retry
            self._attempts += 1
            delay = min(self.max_delay, self.base_delay * (2 ** (self._attempts - 1)))
            self._next_try = now + delay
            if self._attempts >= self.max_attempts:
                self._given_up = True
                self._emit("giveup", attempts=self._attempts, error=str(exc))
                return "given-up"
            self._emit("retry", attempt=self._attempts, delay=delay, error=str(exc))
            return "retrying"

        self._emit("reconnect", attempts=self._attempts)
        self._do_resubscribe()
        self._attempts = 0
        self._next_try = 0.0
        return "up"

    def status(self) -> Dict[str, Any]:
        return {
            "seen_up": self._seen_up,
            "attempts": self._attempts,
            "given_up": self._given_up,
            "max_attempts": self.max_attempts,
        }


__all__ = ["ReconnectSupervisor"]
