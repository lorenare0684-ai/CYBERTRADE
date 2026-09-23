"""Alert center: rule-based notifications over the event bus + optional webhook.

Rules cover risk state changes (HALT/DRAWDDOWN_LOCK/NEWS_LOCK), big single
losses, equity swings, and daily-target hits.  Delivery is fire-and-forget to
keep the hot path safe: console/bell always, webhook best-effort via stdlib
urllib on a daemon thread.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from ..config import AlertConfig
from ..events import Event, Topic, default_bus
from ..exceptions import CybertradeError
from ..utils import timex

log = logging.getLogger("cybertrade.alerts")

DEFAULT_RULES = {
    "halt": True,
    "drawdown_lock": True,
    "news_lock": True,
    "kill_switch": True,
    "trade_loss_above": 50.0,       # alert when a single loss exceeds this
    "daily_target_hit": True,
    "equity_move_pct": 3.0,         # equity swings beyond this % ping
}


class AlertError(CybertradeError):
    pass


@dataclass
class Alert:
    kind: str
    title: str
    body: str
    severity: str = "info"            # info | warn | critical
    ts: float = field(default_factory=timex.now)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "ts": self.ts,
            "iso": timex.iso(self.ts),
            "meta": self.meta,
        }


def _payload_dict(event: Event) -> Dict[str, Any]:
    """Tolerant payload access: dataclasses, dicts, or plain values."""
    payload = event.payload
    if isinstance(payload, dict):
        return payload
    out: Dict[str, Any] = {}
    for key in ("pnl", "won", "asset", "side", "equity", "balance", "reason",
                "level", "event", "daily_target_hit", "title", "blackout", "ts"):
        if hasattr(payload, key):
            out[key] = getattr(payload, key)
    out.setdefault("raw", str(payload)[:200])
    return out


class AlertCenter:
    """Subscribes to bus topics and fires configured alerts."""

    def __init__(
        self,
        config: Optional[AlertConfig] = None,
        rules: Optional[Dict[str, Any]] = None,
        webhook: Optional[Callable[[Alert], None]] = None,
    ) -> None:
        self.config = config or AlertConfig()
        # precedence: explicit rules > AlertConfig thresholds > DEFAULT_RULES
        user_rules = rules or {}
        merged: Dict[str, Any] = {
            **DEFAULT_RULES,
            "trade_loss_above": self.config.trade_loss_alert,
            "equity_move_pct": self.config.equity_move_pct,
            "daily_target_hit": self.config.daily_target_alert,
            **user_rules,
        }
        self.rules: Dict[str, Any] = merged
        self._webhook = webhook
        self._history: List[Alert] = []
        self._subscribed = False
        self._lock = threading.RLock()
        self._last_equity: Optional[float] = None
        self._seen: Set[str] = set()          # dedupe keys
        self.max_history = 200

    # -- wiring ------------------------------------------------------------
    def attach(self, bus=default_bus) -> None:
        if self._subscribed:
            return
        bus.subscribe(Topic.RISK, self._on_risk)
        bus.subscribe(Topic.SETTLE, self._on_trade)
        bus.subscribe(Topic.BALANCE, self._on_account)
        bus.subscribe(Topic.NEWS, self._on_news)
        bus.subscribe(Topic.KILL, self._on_kill)
        self._subscribed = True

    def detach(self, bus=default_bus) -> None:
        bus.unsubscribe(Topic.RISK, self._on_risk)
        bus.unsubscribe(Topic.SETTLE, self._on_trade)
        bus.unsubscribe(Topic.BALANCE, self._on_account)
        bus.unsubscribe(Topic.NEWS, self._on_news)
        bus.unsubscribe(Topic.KILL, self._on_kill)
        self._subscribed = False

    # -- rules -------------------------------------------------------------
    def _rule(self, key: str, default=None):
        return self.rules.get(key, default)

    def _on_risk(self, event: Event) -> None:
        data = _payload_dict(event)
        kind = str(data.get("event", data.get("level", ""))).lower()
        reason = str(data.get("reason", ""))
        if kind in ("halt", "halted", "no_trade", "kill_switch") and self._rule("halt", True):
            self.fire(Alert(
                kind="halt",
                title="RISK HALT",
                body=reason or "risk state is HALT — trading suspended",
                severity="critical",
                meta=data,
            ))
        elif kind in ("drawdown_lock", "locked", "daily_lock") and self._rule("drawdown_lock", True):
            self.fire(Alert(
                kind="drawdown_lock",
                title="DRAWDOWN LOCK",
                body=reason or "drawdown limit hit — new risk banned",
                severity="critical",
                meta=data,
            ))
        elif kind in ("news_lock", "news_blackout", "survivor_veto") and self._rule("news_lock", True):
            if "news" in reason.lower() or kind in ("news_lock", "news_blackout"):
                self.fire(Alert(
                    kind="news_lock",
                    title="NEWS BLACKOUT",
                    body=reason or "high-impact news window — entries paused",
                    severity="warn",
                    meta=data,
                ))

    def _on_kill(self, event: Event) -> None:
        if not self._rule("kill_switch", True):
            return
        data = _payload_dict(event)
        self.fire(Alert(
            kind="kill_switch",
            title="KILL SWITCH",
            body=str(data.get("reason", "kill switch engaged — all risk closed")),
            severity="critical",
            meta=data,
        ))

    def _on_trade(self, event: Event) -> None:
        data = _payload_dict(event)
        try:
            pnl = float(data.get("pnl", 0.0))
        except (TypeError, ValueError):
            pnl = 0.0
        loss_threshold = float(self._rule("trade_loss_above", 50.0))
        if pnl <= -loss_threshold:
            self.fire(Alert(
                kind="big_loss",
                title="BIG LOSS",
                body=f"{data.get('asset', '?')} {data.get('side', '?')} lost {pnl:.2f}",
                severity="warn",
                meta=data,
            ))

    def _on_account(self, event: Event) -> None:
        data = _payload_dict(event)
        equity = data.get("equity", data.get("balance"))
        if equity is None:
            return
        try:
            equity = float(equity)
        except (TypeError, ValueError):
            return
        threshold = float(self._rule("equity_move_pct", 3.0))
        if self._last_equity and self._last_equity > 0:
            move = (equity - self._last_equity) / self._last_equity * 100.0
            if abs(move) >= threshold:
                self.fire(Alert(
                    kind="equity_move",
                    title="EQUITY MOVE",
                    body=f"equity {self._last_equity:.2f} -> {equity:.2f} ({move:+.2f}%)",
                    severity="warn" if move < 0 else "info",
                    meta={"move_pct": move, "equity": equity},
                ))
        self._last_equity = equity
        if data.get("daily_target_hit") and self._rule("daily_target_hit", True):
            key = f"target-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            if key not in self._seen:
                self._seen.add(key)
                self.fire(Alert(
                    kind="daily_target",
                    title="DAILY TARGET HIT",
                    body=f"equity {equity:.2f} — back off and keep the win",
                    severity="info",
                    meta=data,
                ))

    def _on_news(self, event: Event) -> None:
        data = _payload_dict(event)
        if data.get("blackout") and self._rule("news_lock", True):
            key = f"news-{data.get('title', '')}-{int(float(data.get('ts', 0) or 0))}"
            if key in self._seen:
                return
            self._seen.add(key)
            self.fire(Alert(
                kind="news_window",
                title="NEWS WINDOW",
                body=str(data.get("title", "high-impact release imminent")),
                severity="warn",
                meta=data,
            ))

    # -- delivery ----------------------------------------------------------
    def fire(self, alert: Alert) -> None:
        with self._lock:
            self._history.append(alert)
            if len(self._history) > self.max_history:
                del self._history[: -self.max_history]
        line = f"[ALERT/{alert.severity.upper()}] {alert.title} — {alert.body}"
        if alert.severity == "critical":
            log.error(line)
        elif alert.severity == "warn":
            log.warning(line)
        else:
            log.info(line)
        if self.config.enable_sound and alert.severity in ("warn", "critical"):
            self._bell()
        if (self.config.enable_webhook and self.config.webhook_url) or self._webhook is not None:
            threading.Thread(
                target=self._post_webhook, args=(alert,), daemon=True, name="alert-webhook"
            ).start()
        default_bus.publish(Topic.ALERT, alert.to_dict(), source="alerts")

    def _bell(self) -> None:
        try:
            print("\a", end="", flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _post_webhook(self, alert: Alert) -> None:
        if self._webhook is not None:
            try:
                self._webhook(alert)
            except Exception:  # noqa: BLE001
                log.exception("webhook sink failed")
            return
        url = self.config.webhook_url
        if not url:
            return
        payload = json.dumps({"source": "cybertrade", **alert.to_dict()}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "cybertrade/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:  # noqa: S310
                resp.read()
        except Exception as exc:  # noqa: BLE001 — alerts must never crash trading
            log.warning("webhook delivery failed: %s", exc)

    # -- reads -------------------------------------------------------------
    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return [a.to_dict() for a in self._history[-limit:]][::-1]

    def history(self) -> List[Alert]:
        with self._lock:
            return list(self._history)


__all__ = ["AlertCenter", "Alert", "AlertError", "DEFAULT_RULES"]
