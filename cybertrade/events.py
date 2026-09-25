"""Thread-safe publish/subscribe event bus.

Every subsystem (feeds, engine, risk, GUI, web terminal) talks through the bus,
which keeps the hot path decoupled from the presentation layers and makes the
whole terminal testable without a display.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, DefaultDict, List, Optional

log = logging.getLogger("cybertrade.events")


class Topic(str, Enum):
    """Well-known event topics (wildcard ``*`` subscribes to everything)."""

    TICK = "tick"
    CANDLE = "candle"
    SIGNAL = "signal"
    ORDER_SUBMIT = "order.submit"
    ORDER_UPDATE = "order.update"
    FILL = "fill"
    SETTLE = "settle"
    BALANCE = "balance"
    RISK = "risk"
    RISK_REJECT = "risk.reject"
    REGIME = "regime"
    ENGINE_STATE = "engine.state"
    LOG = "log"
    HEARTBEAT = "heartbeat"
    CONNECTION = "connection"
    KILL = "kill"
    GUI_ACTION = "gui.action"
    WEB_ACTION = "web.action"
    HEALTH = "health"
    NEWS = "news"
    ALERT = "alert"


@dataclass(frozen=True)
class Event:
    """Immutable event envelope."""

    topic: str
    payload: Any = None
    ts: float = field(default_factory=time.time)
    source: str = ""

    def with_payload(self, payload: Any) -> "Event":
        return Event(topic=self.topic, payload=payload, ts=self.ts, source=self.source)


Handler = Callable[[Event], None]


class Subscription:
    """Handle returned by :meth:`EventBus.subscribe`; use as a context manager."""

    def __init__(self, bus: "EventBus", topic: str, handler: Handler) -> None:
        self._bus = bus
        self._topic = topic
        self._handler = handler

    def unsubscribe(self) -> None:
        self._bus.unsubscribe(self._topic, self._handler)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.unsubscribe()


class EventBus:
    """Synchronous, thread-safe pub/sub bus.

    Handlers run on the publishing thread.  GUI toolkits that require main
    thread execution should marshal via their own queue (the Tk bridge does).
    A failing handler never breaks the publisher or other subscribers.
    """

    WILDCARD = "*"

    def __init__(self, history_limit: int = 512) -> None:
        self._lock = threading.RLock()
        self._subs: DefaultDict[str, List[Handler]] = defaultdict(list)
        self._history: List[Event] = []
        self._history_limit = max(1, history_limit)
        self._dropped = 0

    # -- subscription ------------------------------------------------------
    def subscribe(self, topic: str | Topic, handler: Handler) -> Subscription:
        key = topic.value if isinstance(topic, Topic) else str(topic)
        with self._lock:
            self._subs[key].append(handler)
        return Subscription(self, key, handler)

    def unsubscribe(self, topic: str | Topic, handler: Handler) -> None:
        key = topic.value if isinstance(topic, Topic) else str(topic)
        with self._lock:
            try:
                self._subs[key].remove(handler)
            except ValueError:
                pass

    def once(self, topic: str | Topic, handler: Handler) -> Subscription:
        """Subscribe to a single occurrence of *topic*."""

        def _one_shot(event: Event) -> None:
            self.unsubscribe(topic, _one_shot)
            handler(event)

        return self.subscribe(topic, _one_shot)

    # -- publishing --------------------------------------------------------
    def publish(
        self,
        topic: str | Topic,
        payload: Any = None,
        source: str = "",
    ) -> Event:
        key = topic.value if isinstance(topic, Topic) else str(topic)
        event = Event(topic=key, payload=payload, source=source)
        with self._lock:
            handlers = list(self._subs.get(key, ()))
            handlers += list(self._subs.get(self.WILDCARD, ()))
            self._history.append(event)
            if len(self._history) > self._history_limit:
                overflow = len(self._history) - self._history_limit
                del self._history[:overflow]
                self._dropped += overflow
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - isolate subscribers
                log.exception("event handler crashed topic=%s source=%s", key, source)
        return event

    # -- introspection -----------------------------------------------------
    def history(self, topic: str | Topic | None = None, limit: int = 50) -> List[Event]:
        key = None
        if topic is not None:
            key = topic.value if isinstance(topic, Topic) else str(topic)
        with self._lock:
            items = list(self._history)
        if key is not None:
            items = [e for e in items if e.topic == key]
        return items[-limit:]

    @property
    def dropped(self) -> int:
        return self._dropped

    def subscriber_count(self, topic: str | Topic) -> int:
        key = topic.value if isinstance(topic, Topic) else str(topic)
        with self._lock:
            return len(self._subs.get(key, ()))

    def clear(self) -> None:
        with self._lock:
            self._subs.clear()
            self._history.clear()


# Process-wide default bus.
default_bus = EventBus()


def publish(topic: str | Topic, payload: Any = None, source: str = "") -> Event:
    """Convenience wrapper around the process-wide bus."""
    return default_bus.publish(topic, payload, source)


def subscribe(topic: str | Topic, handler: Handler) -> Subscription:
    """Convenience wrapper around the process-wide bus."""
    return default_bus.subscribe(topic, handler)


__all__ = [
    "Topic",
    "Event",
    "Handler",
    "Subscription",
    "EventBus",
    "default_bus",
    "publish",
    "subscribe",
]
