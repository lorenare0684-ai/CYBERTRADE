"""Ghost wire — organic traffic discipline for the unofficial Quotex link.

"Undetectable" here means one honest thing: **this client's network manners
are indistinguishable from the Chrome it was paired from** (Phase-29 login).
It does NOT mean CAPTCHA bypass, fingerprint spoofing, proxy rotation, or
ban evasion — none of which exist in this codebase, by standing rule.

What the ghost wire actually does:

1. **Pacekeeper** — every venue frame rides a token-style gate with
   class-specific minimum gaps and *jittered* spacing (uniform, never
   metronomic): orders get think-time (``order_think_ms`` scaled 0.7–1.4×),
   a hard minimum gap, and a sliding per-minute window; history/poll
   requests and generic frames get their own smaller gaps. A bot signature
   is timing: 12 orders/second, history pulls on a 100ms grid, think-time
   of exactly 0ms. Humans are sloppy — we are sloppy on purpose.

2. **Parity headers** — HTTP and WebSocket handshakes carry the header
   set a real Chrome tab sends (UA + Origin + locale + no-cache), minus
   the things we do not actually implement (``Sec-WebSocket-Extensions``
   compression is omitted rather than faked — lying about a capability is
   a fingerprint).

3. **Jittered reconnect backoff** — exponential reconnect delays get
   ±25% jitter so reconnect patterns do not land on a fixed grid.

4. **Session-fault detection** — venue errors that mean "your session is
   dead" are classified loudly (re-pair via ``cybertrade quotex login``)
   instead of retried blindly like a dumb loop.

Stdlib only. Tests inject ``clock``/``sleep``/``rnd`` for determinism.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

# Frame classes → minimum spacing (seconds) between sends of that class.
CLASS_GAPS = {
    "order": 0.350,     # trades + sell-backs: human click cadence
    "history": 0.120,   # history/load / instruments: chart-refresh cadence
    "poll": 0.120,      # balance / portfolio pulls share the history gate
    "frame": 0.040,     # everything else (subscribe, change_balance, …)
}

# Venue-error markers that mean the *session* is dead — re-pair, don't retry.
SESSION_MARKERS = (
    "invalid session",
    "session expired",
    "authorization/reject",
    "unauthorized",
    "not authorized",
    "access denied",
    "forbidden",
    "please login",
    "cloudflare",
    "cf-challenge",
    "captcha",
)


def is_session_fault(message: str) -> bool:
    """True when a venue error means 'session dead — re-pair'."""
    msg = (message or "").lower()
    return any(marker in msg for marker in SESSION_MARKERS)


def parity_headers(user_agent: str, origin: str = "",
                   referer: str = "") -> Dict[str, str]:
    """Browser-parity extras for HTTP and WS handshakes.

    Truthful headers only: locale and cache intent that match a real
    English trade tab, plus the Origin/Referer a real upgrade carries.
    Compression/extensions are omitted (we do not implement them),
    never advertised.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept-Language": "en-US,en;q=0.9",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
    }
    if origin:
        headers["Origin"] = origin
    if referer:
        headers["Referer"] = referer
    return headers


def reconnect_delay(attempt: int, rnd: Optional[random.Random] = None) -> float:
    """Exponential backoff with jitter (2s base, ×1.8, 60s cap, ±20–25%)."""
    r = rnd or random
    base = 2.0 * (1.8 ** max(0, attempt - 1))
    base = min(60.0, base)
    return base * r.uniform(0.8, 1.25)


class Pacekeeper:
    """Serializes venue frames into human-shaped traffic.

    ``wait(kind)`` sleeps only what the class rules demand, returns the
    seconds actually slept (0 when disabled), and keeps stats for the
    status HUD. Thread-safe; ``clock``/``sleep``/``rnd`` are injectable.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        order_think_ms: int = 140,
        order_min_gap_ms: int = 350,
        max_orders_per_min: int = 10,
        history_min_gap_ms: int = 120,
        frame_min_gap_ms: int = 40,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rnd: Optional[random.Random] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.order_think_ms = max(0, int(order_think_ms))
        self.order_min_gap = max(0.0, order_min_gap_ms / 1000.0)
        self.max_orders_per_min = max(1, int(max_orders_per_min))
        self.history_min_gap = max(0.0, history_min_gap_ms / 1000.0)
        self.frame_min_gap = max(0.0, frame_min_gap_ms / 1000.0)
        self._clock = clock
        self._sleep = sleep
        self._rnd = rnd or random.Random()
        self._lock = threading.RLock()
        self._last: Dict[str, float] = {}
        self._order_stamps: Deque[float] = deque()
        self.waits = 0
        self.slept_total = 0.0
        self.orders = 0
        self.max_wait = 0.0

    def _gap_for(self, kind: str) -> float:
        if kind == "order":
            return self.order_min_gap
        if kind in ("history", "poll"):
            return self.history_min_gap
        return self.frame_min_gap

    def _think(self) -> float:
        """Jittered human think-time: uniform(0.7, 1.4) × base."""
        if self.order_think_ms <= 0:
            return 0.0
        return self._rnd.uniform(0.7, 1.4) * (self.order_think_ms / 1000.0)

    def _window_wait(self, now: float) -> float:
        """Seconds until an order slot frees inside the 60s sliding window."""
        horizon = now - 60.0
        while self._order_stamps and self._order_stamps[0] <= horizon:
            self._order_stamps.popleft()
        if len(self._order_stamps) < self.max_orders_per_min:
            return 0.0
        return max(0.0, (self._order_stamps[0] + 60.0) - now)

    def wait(self, kind: str = "frame") -> float:
        """Block until this frame class may honestly fire; return slept s."""
        if not self.enabled:
            return 0.0
        with self._lock:
            now = self._clock()
            need = 0.0
            last = self._last.get(kind)
            if last is not None:
                need = max(0.0, self._gap_for(kind) - (now - last))
            if kind == "order":
                need = max(need, self._think())
                need = max(need, self._window_wait(now))
            slept = 0.0
            if need > 0.0:
                self._sleep(need)
                slept = need
                now = self._clock()
            self._last[kind] = now
            if kind == "order":
                self._order_stamps.append(now)
                self.orders += 1
            self.waits += 1
            self.slept_total += slept
            self.max_wait = max(self.max_wait, slept)
            return slept

    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "waits": self.waits,
                "orders": self.orders,
                "slept_total": round(self.slept_total, 3),
                "max_wait": round(self.max_wait, 3),
                "orders_per_min_cap": self.max_orders_per_min,
                "order_min_gap_ms": int(self.order_min_gap * 1000),
                "order_think_ms": self.order_think_ms,
            }


__all__ = [
    "Pacekeeper",
    "CLASS_GAPS",
    "SESSION_MARKERS",
    "is_session_fault",
    "parity_headers",
    "reconnect_delay",
]
