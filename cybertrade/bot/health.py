"""System health snapshot aggregation for the HUDs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..utils import timex


@dataclass
class HealthSnapshot:
    ts: float = field(default_factory=timex.now)
    uptime: float = 0.0
    engine_state: str = "boot"
    posture: str = "NORMAL"
    regime: str = "unknown"
    feed_ok: bool = False
    broker_ok: bool = False
    tick_rate: float = 0.0
    signals_total: int = 0
    trades_total: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    balance: float = 0.0
    drawdown: float = 0.0
    daily_loss_frac: float = 0.0
    open_positions: int = 0
    vetoes: int = 0
    messages: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.engine_state in ("kill", "shutdown"):
            return "CRITICAL"
        if self.drawdown > 0.15 or self.daily_loss_frac > 0.05:
            return "WARNING"
        if not self.feed_ok:
            return "WARNING"
        return "NOMINAL"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "uptime": round(self.uptime, 1),
            "engine_state": self.engine_state,
            "posture": self.posture,
            "regime": self.regime,
            "feed_ok": self.feed_ok,
            "broker_ok": self.broker_ok,
            "tick_rate": round(self.tick_rate, 2),
            "signals_total": self.signals_total,
            "trades_total": self.trades_total,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "balance": round(self.balance, 2),
            "drawdown": round(self.drawdown, 4),
            "daily_loss_frac": round(self.daily_loss_frac, 4),
            "open_positions": self.open_positions,
            "vetoes": self.vetoes,
            "status": self.status,
            "messages": self.messages[-5:],
        }


class HealthMonitor:
    """Rolls engine/risk/watchdog state into one HUD-ready snapshot."""

    def __init__(self) -> None:
        self.start_ts = time.time()
        self.messages: List[str] = []
        self._tick_window: List[float] = []

    def note_message(self, msg: str) -> None:
        self.messages.append(f"[{timex.iso()}] {msg}")
        if len(self.messages) > 50:
            del self.messages[:-50]

    def note_tick(self) -> None:
        now = time.time()
        self._tick_window.append(now)
        if len(self._tick_window) > 500:
            del self._tick_window[:-500]

    @property
    def tick_rate(self) -> float:
        if len(self._tick_window) < 2:
            return 0.0
        span = self._tick_window[-1] - self._tick_window[0]
        return (len(self._tick_window) - 1) / span if span > 0 else 0.0

    def snapshot(self, **fields: Any) -> HealthSnapshot:
        snap = HealthSnapshot(
            uptime=time.time() - self.start_ts,
            tick_rate=self.tick_rate,
            messages=list(self.messages),
            **{k: v for k, v in fields.items() if k in HealthSnapshot.__annotations__},
        )
        return snap


__all__ = ["HealthMonitor", "HealthSnapshot"]
