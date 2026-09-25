"""Watchdog: liveness, anomaly detection, and the physical kill switch."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..events import Topic, default_bus
from ..utils import timex
from ..utils.mathx import clamp

log = logging.getLogger("cybertrade.watchdog")


@dataclass
class Heartbeat:
    name: str
    ts: float = 0.0
    ok: bool = True
    detail: str = ""

    @property
    def age(self) -> float:
        return max(0.0, timex.now() - self.ts) if self.ts else float("inf")


@dataclass
class Anomaly:
    kind: str
    severity: float          # 0..1
    detail: str
    ts: float = field(default_factory=timex.now)


class Watchdog:
    """Supervises subsystem heartbeats and hostile market behaviour.

    On fatal conditions it fires the kill callback (engine disarms / closes
    shop).  Non-fatal anomalies raise a defensive posture via listeners.
    """

    def __init__(
        self,
        max_stale_seconds: float = 15.0,
        max_latency_ms: float = 2500.0,
        kill_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.max_stale_seconds = max_stale_seconds
        self.max_latency_ms = max_latency_ms
        self.kill_callback = kill_callback
        self._beats: Dict[str, Heartbeat] = {}
        self._anomalies: List[Anomaly] = []
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._tick_ts: Dict[str, float] = {}
        self._latency_ms: float = 0.0

    # -- heartbeat registry ------------------------------------------------
    def beat(self, name: str, ok: bool = True, detail: str = "") -> None:
        with self._lock:
            self._beats[name] = Heartbeat(name=name, ts=timex.now(), ok=ok, detail=detail)
        default_bus.publish(Topic.HEARTBEAT, {"name": name, "ok": ok}, source="watchdog")

    def mark_tick(self, asset: str) -> None:
        with self._lock:
            self._tick_ts[asset] = timex.now()

    def set_latency(self, ms: float) -> None:
        self._latency_ms = ms

    # -- loop --------------------------------------------------------------
    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="watchdog")
        self._thread.start()
        log.info("watchdog armed")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while self._running:
            try:
                self.sweep()
            except Exception:  # noqa: BLE001
                log.exception("watchdog sweep failed")
            time.sleep(1.0)

    def sweep(self, now: Optional[float] = None) -> List[Anomaly]:
        now = now if now is not None else timex.now()
        found: List[Anomaly] = []
        with self._lock:
            for name, beat in self._beats.items():
                age = now - beat.ts if beat.ts else float("inf")
                if age > self.max_stale_seconds:
                    found.append(
                        Anomaly("stale_heartbeat", 0.8, f"{name} stale {age:.1f}s", ts=now)
                    )
                if not beat.ok:
                    found.append(Anomaly("beat_fail", 0.5, f"{name}: {beat.detail}", ts=now))
            for asset, ts in self._tick_ts.items():
                age = now - ts
                if age > self.max_stale_seconds:
                    found.append(
                        Anomaly("stale_feed", 0.7, f"{asset} feed silent {age:.1f}s", ts=now)
                    )
        if self._latency_ms > self.max_latency_ms:
            found.append(
                Anomaly(
                    "latency",
                    clamp(self._latency_ms / (self.max_latency_ms * 2), 0.1, 1.0),
                    f"venue latency {self._latency_ms:.0f}ms",
                    ts=now,
                )
            )
        for anomaly in found:
            self.note(anomaly)
        return found

    def note(self, anomaly: Anomaly) -> None:
        with self._lock:
            self._anomalies.append(anomaly)
            if len(self._anomalies) > 200:
                del self._anomalies[:-200]
        level = logging.CRITICAL if anomaly.severity >= 0.9 else logging.WARNING
        log.log(level, "anomaly [%s] %s", anomaly.kind, anomaly.detail)
        default_bus.publish(
            Topic.HEALTH,
            {"anomaly": anomaly.kind, "detail": anomaly.detail, "severity": anomaly.severity},
            source="watchdog",
        )
        if anomaly.severity >= 0.9 and self.kill_callback:
            self.kill_callback(f"{anomaly.kind}: {anomaly.detail}")

    # -- market anomaly detectors -----------------------------------------
    def check_price_jump(self, asset: str, last: float, prev: float, vol: float) -> Optional[Anomaly]:
        """Detect a move too violent for the recent volatility (flash crash)."""
        if prev <= 0 or vol <= 0:
            return None
        move = abs(last - prev) / prev
        z = move / vol
        if z >= 8.0:
            anomaly = Anomaly("price_jump", clamp(z / 12.0, 0.5, 1.0), f"{asset} move z={z:.1f}")
            self.note(anomaly)
            return anomaly
        return None

    def check_gap(self, asset: str, open_: float, prev_close: float, vol: float) -> Optional[Anomaly]:
        if prev_close <= 0 or vol <= 0:
            return None
        gap = abs(open_ - prev_close) / prev_close
        if gap > vol * 4:
            anomaly = Anomaly("gap", clamp(gap / (vol * 8), 0.4, 1.0), f"{asset} gap {gap * 100:.2f}%")
            self.note(anomaly)
            return anomaly
        return None

    # -- queries -----------------------------------------------------------
    def anomalies(self, limit: int = 20) -> List[Anomaly]:
        with self._lock:
            return self._anomalies[-limit:]

    def healthy(self) -> bool:
        with self._lock:
            if any(not b.ok for b in self._beats.values()):
                return False
            return not any(a.severity >= 0.9 for a in self._anomalies[-5:])

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "healthy": self.healthy(),
                "beats": {
                    name: {"age": round(b.age, 2), "ok": b.ok, "detail": b.detail}
                    for name, b in self._beats.items()
                },
                "latency_ms": round(self._latency_ms, 1),
                "recent_anomalies": [
                    {"kind": a.kind, "severity": round(a.severity, 2), "detail": a.detail}
                    for a in self._anomalies[-5:]
                ],
            }


__all__ = ["Watchdog", "Heartbeat", "Anomaly"]
