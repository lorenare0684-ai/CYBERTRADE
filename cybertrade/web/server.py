"""Stdlib web terminal: static cyberpunk UI + JSON API + Server-Sent Events.

Zero dependencies.  A :class:`ThreadingHTTPServer` serves the dashboard and
streams live engine telemetry over SSE so the browser HUD stays hot without
websockets.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..events import Topic, default_bus
from ..quant.binary import breakeven_winrate
from ..utils import timex
from ..utils.jsonx import dumps

log = logging.getLogger("cybertrade.web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# SSE subscribers: each gets a queue of event dicts
_SUBSCRIBERS: List["queue.Queue"] = []
_SUBS_LOCK = threading.Lock()


def broadcast(kind: str, payload: Any) -> None:
    envelope = {"kind": kind, "ts": timex.now(), "data": payload}
    with _SUBS_LOCK:
        subs = list(_SUBSCRIBERS)
    for q in subs:
        try:
            q.put_nowait(envelope)
        except queue.Full:
            pass


class EngineHub:
    """Bridge between a TradingEngine and the web layer."""

    def __init__(self, engine, config=None) -> None:
        self.engine = engine
        self.config = config
        self.started = timex.now()
        self._log_lines: List[dict] = []
        default_bus.subscribe(Topic.LOG, self._on_log)
        for topic in (Topic.TICK, Topic.SIGNAL, Topic.SETTLE, Topic.REGIME,
                      Topic.ENGINE_STATE, Topic.RISK_REJECT, Topic.KILL,
                      Topic.FILL, Topic.ALERT, Topic.NEWS, Topic.BALANCE,
                      Topic.SCENARIO):
            default_bus.subscribe(topic, self._make_forwarder(topic.value))

    def _make_forwarder(self, name: str):
        def _forward(event) -> None:
            payload = event.payload
            if hasattr(payload, "to_dict"):
                payload = payload.to_dict()
            elif hasattr(payload, "__dict__") and not isinstance(payload, dict):
                try:
                    payload = {
                        k: (v.value if hasattr(v, "value") else v)
                        for k, v in vars(payload).items()
                        if not k.startswith("_")
                    }
                except Exception:  # noqa: BLE001
                    payload = {"repr": repr(payload)[:200]}
            broadcast(name, payload)
        return _forward

    def _on_log(self, event) -> None:
        rec = dict(event.payload)
        self._log_lines.append(rec)
        if len(self._log_lines) > 400:
            del self._log_lines[:-400]
        broadcast("log", rec)

    # -- API payloads ------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        snap = self.engine.snapshot()
        candles = {}
        for asset in self.engine.feed.assets[:4]:
            book = getattr(self.engine.feed, "book", None)
            if callable(book):
                try:
                    series = book(asset).book(self.engine.config.timeframe().seconds)
                    candles[asset] = [c.to_dict() for c in series.candles(limit=180)]
                except Exception:  # noqa: BLE001
                    candles[asset] = []
            else:
                candles[asset] = []
        ledger = self.engine.oms.ledger
        equity_curve = [[ts, bal] for ts, bal in list(ledger.equity_curve)[-400:]]
        feed = self.engine.feed
        scenarios = {}
        if callable(getattr(feed, "current_scenarios", None)):
            try:
                scenarios = feed.current_scenarios()
            except Exception:  # noqa: BLE001
                scenarios = {}
        return {
            "ts": timex.now(),
            "uptime": timex.now() - self.started,
            "snapshot": snap,
            "assets": self.engine.feed.assets,
            "trades": [t.to_dict() for t in self.engine.oms.recent_trades(30)],
            "signals": [s.to_dict() for s in self.engine.signals[-20:]],
            "candles": candles,
            "logs": self._log_lines[-80:],
            "posture": self.engine.survivor.describe(),
            "strategies": self.engine.ensemble.describe(),
            # -- Phase-2 HUD feeds -----------------------------------------
            "equity_curve": equity_curve,
            "alerts": self.engine.alerts.recent(10),
            "clusters": self.engine.corr.clusters(),
            "correlation": self.engine.corr.matrix(
                self.engine.feed.assets[:6]
            ) if len(self.engine.feed.assets) > 1 else {},
            "calendar": self.engine.calendar.to_list(timex.now())[:8],
            "scenarios": scenarios,
            # -- Phase-4 edge layer -----------------------------------------
            "edge": {
                "payout": self.engine.config.broker.payout_default,
                "breakeven": round(breakeven_winrate(self.engine.config.broker.payout_default), 4),
                "min_edge": self.engine.config.risk.min_edge,
                "gate": self.engine.config.risk.edge_gate,
                "rejects": self.engine.edge_rejects,
                "calibration": self.engine.calibrator.summary(),
            },
            "flow": {a: f.snapshot() for a, f in self.engine.flow.items()},
            "tape": self.engine.tape.stats(),
        }

    def montecarlo(self, runs: int = 400, horizon: int = 200) -> Dict[str, Any]:
        """Risk lab report bootstrapped from settled trades (or synthetic
        default when no history exists yet)."""
        from ..risk.montecarlo import simulate_from_records

        trades = self.engine.oms.ledger.trades
        starting = self.engine.config.risk.starting_balance
        if trades:
            report = simulate_from_records(
                trades, starting_balance=starting, runs=runs, horizon=horizon
            )
            source = "trades"
        else:
            from ..risk.montecarlo import simulate

            # conservative placeholder: small negative edge, wide variance
            report = simulate(
                [8.5, -10.0] * 40 + [8.5] * 8,
                starting_balance=starting, runs=runs, horizon=horizon,
            )
            source = "placeholder"
        data = report.to_dict()
        data["verdict"] = report.verdict()
        data["source"] = source
        data["n_trades"] = len(trades)
        data["summary"] = report.summary_text()
        return data

    def logs(self, limit: int = 200) -> List[dict]:
        return self._log_lines[-limit:]


class WebTerminal:
    """HTTP + SSE server for the dashboard."""

    def __init__(
        self,
        hub: EngineHub,
        host: str = "0.0.0.0",
        port: int = 8899,
    ) -> None:
        self.hub = hub
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        terminal = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json",
                      extra: Optional[Dict[str, str]] = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, code: int, data: Any) -> None:
                self._send(code, dumps(data).encode("utf-8"))

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/" or path == "/index.html":
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/") :])
                if path == "/api/state":
                    return self._json(200, terminal.hub.state())
                if path == "/api/logs":
                    return self._json(200, terminal.hub.logs())
                if path == "/api/events":
                    return self._sse()
                if path == "/api/scenarios":
                    from ..backtest.scenarios import describe_all

                    return self._json(200, describe_all())
                if path == "/api/strategies":
                    return self._json(200, terminal.hub.engine.ensemble.describe())
                if path == "/api/montecarlo":
                    from urllib.parse import parse_qs

                    q = parse_qs(parsed.query or "")
                    runs = int((q.get("runs") or ["400"])[0])
                    horizon = int((q.get("horizon") or ["200"])[0])
                    return self._json(200, terminal.hub.montecarlo(runs=runs, horizon=horizon))
                if path == "/api/calendar":
                    return self._json(
                        200,
                        {
                            "now": timex.now(),
                            "events": terminal.hub.engine.calendar.to_list(timex.now()),
                            "blackout": terminal.hub.engine.calendar.is_blackout(timex.now()),
                        },
                    )
                return self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                if parsed.path == "/api/command":
                    return self._json(200, terminal.command(body))
                return self._json(404, {"error": "not found"})

            # -- handlers ---------------------------------------------------
            def _static(self, rel: str) -> None:
                safe = os.path.normpath(rel).replace("\\", "/")
                if safe.startswith("..") or os.path.isabs(safe):
                    return self._json(403, {"error": "forbidden"})
                full = os.path.join(STATIC_DIR, safe)
                if not os.path.isfile(full):
                    return self._json(404, {"error": f"no static {safe}"})
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                with open(full, "rb") as fh:
                    self._send(200, fh.read(), ctype)

            def _sse(self) -> None:
                q: "queue.Queue" = queue.Queue(maxsize=500)
                with _SUBS_LOCK:
                    _SUBSCRIBERS.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            item = q.get(timeout=15.0)
                            payload = dumps(item).encode("utf-8")
                            self.wfile.write(b"event: msg\ndata: " + payload + b"\n\n")
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with _SUBS_LOCK:
                        if q in _SUBSCRIBERS:
                            _SUBSCRIBERS.remove(q)

        self._handler_cls = Handler

    # -- commands ----------------------------------------------------------
    def command(self, body: Dict[str, Any]) -> Dict[str, Any]:
        cmd = str(body.get("cmd", ""))
        engine = self.hub.engine
        try:
            if cmd == "arm":
                engine.arm(live=False)
                return {"ok": True, "state": engine.state.value}
            if cmd == "disarm":
                engine.disarm()
                return {"ok": True, "state": engine.state.value}
            if cmd == "kill":
                engine.kill("operator kill from web console")
                return {"ok": True, "state": engine.state.value}
            if cmd == "clear_kill":
                engine.clear_kill()
                return {"ok": True, "state": engine.state.value}
            if cmd == "trade":
                from ..constants import Side

                side = Side.CALL if body.get("side") == "call" else Side.PUT
                asset = str(body.get("asset") or (engine.feed.assets[0] if engine.feed.assets else ""))
                amount = float(body.get("amount") or 5)
                expiry = int(body.get("expiry") or engine.config.strategy.expiry_seconds)
                from ..data.models import Signal

                sig = Signal(
                    asset=asset,
                    side=side,
                    confidence=0.8,
                    strategy="manual",
                    reason="manual fire from web console",
                    expiry_seconds=expiry,
                    price=engine.feed.last_price(asset) or 0.0,
                )
                placed = engine.inject_signal(sig)
                return {"ok": placed, "placed": placed}
            if cmd == "news":
                engine.survivor.flag_news()
                return {"ok": True, "msg": "news blackout armed"}
            if cmd == "lockdown":
                engine.survivor.engage_lockdown("web console")
                return {"ok": True}
            if cmd == "unlock":
                engine.survivor.clear_lockdown()
                return {"ok": True}
            if cmd == "scenario":
                asset = str(body.get("asset") or "")
                scenario = str(body.get("scenario") or "")
                feed = engine.feed
                setter = getattr(feed, "set_scenario", None)
                if not callable(setter):
                    return {"ok": False, "error": "feed does not support scenario swaps"}
                setter(asset, scenario)
                from ..events import Topic as _T

                default_bus.publish(
                    _T.SCENARIO,
                    {"asset": asset, "scenario": scenario},
                    source="web",
                )
                current = feed.current_scenarios() if callable(
                    getattr(feed, "current_scenarios", None)) else {}
                return {"ok": True, "scenarios": current}
            if cmd == "alerts":
                return {"ok": True, "alerts": engine.alerts.recent(
                    int(body.get("limit") or 20))}
            return {"ok": False, "error": f"unknown cmd {cmd!r}"}
        except Exception as exc:  # noqa: BLE001
            log.exception("command failed: %s", cmd)
            return {"ok": False, "error": str(exc)}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._httpd = ThreadingHTTPServer((self.host, self.port), self._handler_cls)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="web-terminal"
        )
        self._thread.start()
        log.info("web terminal live at http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3.0)

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.stop()


__all__ = ["WebTerminal", "EngineHub", "broadcast"]
