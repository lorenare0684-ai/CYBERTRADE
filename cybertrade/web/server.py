"""Stdlib web terminal: static cyberpunk UI + JSON API + Server-Sent Events.

Zero dependencies.  A :class:`ThreadingHTTPServer` serves the dashboard and
streams live engine telemetry over SSE so the browser HUD stays hot without
websockets.

Live only: the console arms a live engine (real order flow) and offers no
paper, dry-run, drill, backtest or scenario-swap controls — none of those
machinery exists in this build.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import sys
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


def _static_ctype(path: str) -> str:
    """Content-Type for a static asset, with the charset spelled out.

    The dashboard is UTF-8 (box art, arrows, tick glyphs), and a bare
    ``text/html`` lets a browser guess the codepage — which on Windows means
    the locale codepage, and mojibake. Every text type gets an explicit
    ``charset=utf-8`` so the HUD reads the same everywhere.
    """
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in (
            "application/javascript", "application/json"):
        return f"{ctype}; charset=utf-8"
    return ctype


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

    def __init__(self, engine=None, config=None) -> None:
        self.engine = engine
        self.config = config
        self.started = timex.now()
        self.pairing = None          # set by cmd_web when booting unpaired
        self._log_lines: List[dict] = []
        default_bus.subscribe(Topic.LOG, self._on_log)
        for topic in (Topic.TICK, Topic.SIGNAL, Topic.SETTLE, Topic.REGIME,
                      Topic.ENGINE_STATE, Topic.RISK_REJECT, Topic.KILL,
                      Topic.FILL, Topic.ALERT, Topic.NEWS, Topic.BALANCE):
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
        # the score bay's mute flag is gone with it: every log line is live
        rec = dict(event.payload)
        self._log_lines.append(rec)
        if len(self._log_lines) > 400:
            del self._log_lines[:-400]
        broadcast("log", rec)

    # -- API payloads ------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        if self.engine is None:
            # no venue session yet: the HUD shows the pairing screen, and
            # nothing here reports a balance, a candle or an open order —
            # none of them exist until a real session does.
            return {
                "ts": timex.now(),
                "uptime": timex.now() - self.started,
            "paired": False,
            "engine_state": "pairing",
            "posture": "NORMAL",
            "assets": [],
            "catalog": {"rows": [], "live": False, "synced_at": 0.0,
                        "prices": {}},
                "trades": [],
                "positions": [],
                "signals": [],
                "candles": {},
                "logs": self._log_lines[-80:],
                "alerts": [],
                "strategies": [],
                "clusters": [],
                "correlation": {},
                "calendar": [],
                "edge": {},
                "flow": {},
                "tape": {},
                "session": {},
                "snapshot": {"health": {"engine_state": "pairing",
                                        "posture": "NORMAL",
                                        "feed_ok": False,
                                        "broker_ok": False,
                                        "balance": 0.0,
                                        "open_positions": 0,
                                        "signals_total": 0,
                                        "wins": 0, "losses": 0,
                                        "win_rate": 0.0,
                                        "drawdown": 0.0,
                                        "tick_rate": 0.0,
                                        "status": "PAIRING"}},
                "pairing": self.pairing.status() if self.pairing else {},
            }
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
        return {
            "ts": timex.now(),
            "uptime": timex.now() - self.started,
            "snapshot": snap,
            # The unpaired payload sets this too; a client that reads the
            # top-level key must not see it vanish the moment a session
            # arrives, or "pairing" is the last state it ever reports.
            "engine_state": (snap.get("health") or {}).get("engine_state", "disarmed"),
            "assets": self.engine.feed.assets,
            "catalog": self.engine.catalog_block(),
            "trades": [t.to_dict() for t in self.engine.oms.recent_trades(30)],
            "positions": self._positions_block(),
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
            # -- Phase-4 edge layer -----------------------------------------
            "edge": {
                "payout": round(self.worst_payout(), 4),
                "breakeven": round(breakeven_winrate(self.worst_payout()), 4),
                "payouts": {a: round(self.engine.broker.payout_for(a, 60), 4)
                            for a in self.engine.feed.assets},
                "min_edge": self.engine.config.risk.min_edge,
                "gate": self.engine.config.risk.edge_gate,
                "rejects": self.engine.edge_rejects,
                "calibration": self.engine.calibrator.summary(self.worst_payout()),
                "journal": self._journal_block(),
            },
            "flow": {a: f.snapshot() for a, f in self.engine.flow.items()},
            "tape": self.engine.tape.stats(),
            "session": self.engine.session_report(),
            "paired": True,
            "pairing": self.pairing.status() if self.pairing else {},
        }

    def worst_payout(self) -> float:
        """The binding hurdle: the lowest venue quote across the universe."""
        qs = [self.engine.broker.payout_for(a, 60) for a in self.engine.feed.assets]
        return min(qs) if qs else self.engine.config.broker.payout_default

    def _journal_block(self) -> Dict[str, Any]:
        j = getattr(self.engine, "journal", None)
        if j is None:
            return {"available": False}
        try:
            from ..journal import strategy_decay

            rows = strategy_decay(j, window=8)
            decaying = [r for r in rows if r["decaying"]]
            return {
                "available": True,
                "trades": j.count(),
                "decaying": decaying[:5],
                "quarantined": self.engine.quarantine_report(),
                "watched": len(rows),
            }
        except Exception:  # noqa: BLE001
            return {"available": False}

    def _positions_block(self) -> List[Dict[str, Any]]:
        """Open contracts with live ITM/OTM mark + countdown (P27)."""
        out: List[Dict[str, Any]] = []
        now = timex.now()
        feed_last = self.engine.feed.last_price
        broker_last = getattr(self.engine.broker, "last_price", None)
        hold_reader = getattr(self.engine.broker, "recovery_holds", None)
        held = set(hold_reader()) if callable(hold_reader) else set()
        for pos in self.engine.broker.open_positions():
            try:
                last = feed_last(pos.asset)
            except Exception:  # noqa: BLE001
                last = None
            if last is None and callable(broker_last):
                try:
                    last = broker_last(pos.asset)
                except Exception:  # noqa: BLE001
                    last = None
            mark = float(last) if last is not None else float(pos.strike)
            if mark == pos.strike:
                state = "even"
            else:
                call = pos.side == "call"
                state = "itm" if (call and mark > pos.strike) or (
                    not call and mark < pos.strike
                ) else "otm"
            out.append({
                "id": pos.id,
                "asset": pos.asset,
                "side": pos.side.value,
                "stake": round(pos.stake, 2),
                "strike": round(float(pos.strike), 5),
                "mark": round(mark, 5),
                "payout": float(pos.fill.payout),
                "strategy": pos.strategy,
                "label": pos.label,
                "expiry_ts": pos.expiry_ts,
                "seconds_left": max(0.0, pos.expiry_ts - now),
                "state": "review" if pos.id in held else state,
                "recovery_hold": pos.id in held,
            })
        out.sort(key=lambda r: r["expiry_ts"])
        return out

    def montecarlo(self, runs: int = 400, horizon: int = 200,
                   mode: str = "bootstrap") -> Dict[str, Any]:
        """Risk lab: bootstrap settled trades, or (``mode="posterior"``)
        simulate the calibrated Beta posterior over P(win)."""
        from ..risk.montecarlo import simulate_from_records

        trades = self.engine.oms.ledger.trades
        starting = self.engine.config.risk.starting_balance
        source_hint = "trades"
        if not trades:
            # Phase-13: the in-memory list forgets on restart; the journal does not.
            j = getattr(self.engine, "journal", None)
            if j is not None:
                try:
                    rows = j.trades(limit=5000)
                except Exception:  # noqa: BLE001
                    rows = []
                if rows:
                    trades = rows
                    source_hint = "journal"
        if mode == "posterior":
            from ..risk.montecarlo import simulate_posterior

            wins, losses = self.engine.calibrator.evidence()
            report = simulate_posterior(
                wins, losses,
                payout=self.worst_payout(),
                starting_balance=starting, runs=runs, horizon=horizon,
            )
            source = f"posterior W{wins}/L{losses}"
        elif trades:
            report = simulate_from_records(
                trades, starting_balance=starting, runs=runs, horizon=horizon
            )
            source = source_hint
        else:
            # No record, no invented sample: this build reports the hole.
            return {
                "empty": True,
                "source": "none",
                "n_trades": 0,
                "verdict": "NO DATA",
                "summary": (
                    "no settled trades to resample — trade live (or read the "
                    "journal) before the risk lab has anything honest to say"
                ),
            }
        data = report.to_dict()
        data["verdict"] = report.verdict()
        data["source"] = source
        data["n_trades"] = len(trades)
        data["summary"] = report.summary_text()
        return data

    def logs(self, limit: int = 200) -> List[dict]:
        return self._log_lines[-limit:]


class _QuietHTTPServer(ThreadingHTTPServer):
    """Browsers abort idle keep-alive connections all the time.

    Stock ``socketserver`` prints a full traceback for every one (Windows
    reports it as ``ConnectionAbortedError`` 10053), which buries real
    errors.  Dropped client connections are not errors — swallow exactly
    those, and let everything else shout as before.
    """

    _QUIET = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

    def handle_error(self, request, client_address) -> None:  # noqa: N802
        _exc, value, _tb = sys.exc_info()
        if isinstance(value, self._QUIET):
            return
        super().handle_error(request, client_address)


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
                if getattr(self, "_head_only", False):
                    return
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, code: int, data: Any) -> None:
                self._send(code, dumps(data).encode("utf-8"))

            def do_HEAD(self) -> None:  # noqa: N802
                """Same routing as GET, headers only, no body.

                Without this the base handler answers 501, and plenty of
                legitimate clients lead with HEAD — health checks, uptime
                probes, some browsers prefetching. A 501 on / looks like the
                server is broken when it is only the method that is.
                """
                self._head_only = True
                try:
                    self.do_GET()
                finally:
                    self._head_only = False

            def do_GET(self) -> None:  # noqa: N802
                self._route()

            def do_POST(self) -> None:  # noqa: N802
                self._route()

            def do_PUT(self) -> None:  # noqa: N802
                self._route()

            def do_DELETE(self) -> None:  # noqa: N802
                self._route()

            def do_PATCH(self) -> None:  # noqa: N802
                self._route()

            def do_OPTIONS(self) -> None:  # noqa: N802
                self._route()

            def _route(self) -> None:
                """Every method funnels through here.

                An exception escaping a handler does not just lose the
                request — it kills the keep-alive connection with a truncated
                response, and the console shows a dead socket instead of the
                JSON error that would have told the operator what broke.
                """
                try:
                    if self.command in ("GET", "HEAD"):
                        return self._get()
                    if self.command == "POST":
                        return self._post()
                    if self.command == "OPTIONS":
                        # CORS preflight: the HUD may be served from another
                        # origin during development.
                        return self._json(204, {})
                    return self._json(405, {"error": f"{self.command} not allowed"})
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client hung up; nothing to report to
                except Exception as exc:  # noqa: BLE001
                    log.exception("%s %s failed", self.command, self.path)
                    try:
                        self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                    except Exception:  # noqa: BLE001 - already unwritable
                        pass

            def _get(self) -> None:  # noqa: N802
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
                if path == "/api/candles":
                    from urllib.parse import parse_qs

                    q = parse_qs(parsed.query or "")
                    asset = str((q.get("asset") or [""])[0])
                    try:
                        limit = max(1, min(500, int(float(
                            (q.get("limit") or [180])[0]))))
                    except (TypeError, ValueError):
                        limit = 180
                    if terminal.hub.engine is None or not asset:
                        return self._json(200, {"asset": asset, "candles": [],
                                                "empty": True})
                    return self._json(200, {
                        "asset": asset,
                        "candles": terminal.hub.engine.candles_for(asset, limit),
                    })
                if path == "/api/events":
                    return self._sse()
                if path == "/api/pair/status":
                    ctl = getattr(terminal.hub, "pairing", None)
                    if ctl is None:
                        return self._json(200, {"state": "unavailable",
                                                "busy": False,
                                                "error": "pairing is not offered here"})
                    return self._json(200, ctl.status())
                if path == "/api/strategies":
                    if terminal.hub.engine is None:
                        return self._json(200, {"empty": True,
                                                "strategies": [],
                                                "verdict": "NO SESSION",
                                                "summary": "pair a venue session to see the ensemble"})
                    return self._json(200, terminal.hub.engine.ensemble.describe())
                if path == "/api/montecarlo":
                    from urllib.parse import parse_qs

                    q = parse_qs(parsed.query or "")

                    def _num(key: str, default: int, lo: int, hi: int) -> int:
                        """A query string is hostile input: never let it raise.

                        `?runs=abc` used to kill the connection with a
                        ValueError, and the browser showed a dead socket
                        instead of a chart.
                        """
                        try:
                            val = int(float((q.get(key) or [default])[0]))
                        except (TypeError, ValueError):
                            return default
                        return max(lo, min(hi, val))

                    runs = _num("runs", 400, 1, 20000)
                    horizon = _num("horizon", 200, 1, 5000)
                    if terminal.hub.engine is None:
                        # An unpaired terminal has no ledger to bootstrap.
                        # The HUD renders this as NO DATA, which is the
                        # truth; a 500 would read as a broken server.
                        return self._json(200, {
                            "empty": True, "verdict": "NO DATA",
                            "summary": "no settled trades — pair a venue session",
                            "runs": runs, "horizon": horizon,
                            "bands": [], "n_trades": 0, "source": "none",
                        })
                    return self._json(200, terminal.hub.montecarlo(
                        runs=runs, horizon=horizon,
                        mode=(q.get("mode") or ["bootstrap"])[0],
                    ))
                if path == "/api/calendar":
                    if terminal.hub.engine is None:
                        return self._json(200, {"now": timex.now(), "events": [],
                                                "blackout": False,
                                                "paired": False})
                    return self._json(
                        200,
                        {
                            "now": timex.now(),
                            "events": terminal.hub.engine.calendar.to_list(timex.now()),
                            "blackout": terminal.hub.engine.calendar.is_blackout(timex.now()),
                        },
                    )
                return self._json(404, {"error": "not found"})

            def _post(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                try:
                    length = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    return self._json(400, {"error": "bad Content-Length"})
                if length < 0:
                    return self._json(400, {"error": "bad Content-Length"})
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._json(400, {"error": "bad json"})
                if not isinstance(body, dict):
                    # `[]` or `null` is valid JSON and still not a command
                    return self._json(400, {"error": "expected a json object"})
                if parsed.path == "/api/command":
                    return self._json(200, terminal.command(body))
                if parsed.path == "/api/pair/start":
                    ctl = getattr(terminal.hub, "pairing", None)
                    if ctl is None:
                        return self._json(409, {"ok": False,
                                                "error": "pairing is not offered here"})
                    return self._json(200, ctl.start(
                        profile=body.get("profile", ""),
                        port=body.get("port", 9333),
                        timeout=body.get("timeout", 240),
                        purse=body.get("purse"),
                    ))
                if parsed.path == "/api/pair/cancel":
                    ctl = getattr(terminal.hub, "pairing", None)
                    if ctl is None:
                        return self._json(409, {"ok": False,
                                                "error": "pairing is not offered here"})
                    return self._json(200, ctl.cancel())
                return self._json(404, {"error": "not found"})

            # -- handlers ---------------------------------------------------
            def _static(self, rel: str) -> None:
                safe = os.path.normpath(rel).replace("\\", "/")
                if safe.startswith("..") or os.path.isabs(safe):
                    return self._json(403, {"error": "forbidden"})
                full = os.path.join(STATIC_DIR, safe)
                if not os.path.isfile(full):
                    return self._json(404, {"error": f"no static {safe}"})
                with open(full, "rb") as fh:
                    self._send(200, fh.read(), _static_ctype(full))

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
        if not isinstance(body, dict):
            return {"ok": False, "error": "expected a json object"}
        engine = self.hub.engine
        if engine is None:
            # The terminal is up but unpaired: say so, rather than handing
            # back "'NoneType' object has no attribute 'arm'".
            return {"ok": False, "error": "no venue session yet — pair first",
                    "pairing": True}
        cmd = str(body.get("cmd", ""))
        try:
            if cmd == "arm":
                engine.arm()
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
            if cmd == "watch":
                # Chart any catalog asset: adopt + warm it, then serve bars.
                asset = str(body.get("asset") or "")
                if not asset:
                    return {"ok": False, "error": "asset required"}
                adopted = engine.adopt_asset(asset, warm=True)
                return {"ok": True, "adopted": adopted, "asset": asset,
                        "candles": engine.candles_for(asset)}
            if cmd == "close":
                pos_id = str(body.get("position") or "")
                if not pos_id:
                    return {"ok": False, "error": "position id required"}
                if not engine.oms.close_position(pos_id):
                    return {"ok": False,
                            "error": f"position {pos_id!r} not closable"}
                # OMS atomically brackets broker salvage + ledger delivery.
                engine.health.note_message(
                    f"manual close {pos_id} — operator cut"
                )
                default_bus.publish(
                    Topic.ALERT,
                    {"kind": "manual_close", "position": pos_id},
                    source="console",
                )
                return {"ok": True, "closed": pos_id}
            if cmd == "news":
                engine.survivor.flag_news()
                return {"ok": True, "msg": "news blackout armed"}
            if cmd == "lockdown":
                engine.survivor.engage_lockdown("web console")
                engine.save_operator_state()
                return {"ok": True}
            if cmd == "unlock":
                engine.survivor.clear_lockdown()
                engine.save_operator_state()
                return {"ok": True}
            if cmd == "strategy":
                name = str(body.get("name") or "")
                enabled = bool(body.get("enabled", True))
                members = getattr(engine.ensemble, "members", [])
                member = next((m for m in members if m.name == name), None)
                if member is None:
                    return {"ok": False, "error": f"unknown strategy {name!r}"}
                member.enabled = enabled
                engine.save_operator_state()
                engine.health.note_message(
                    f"strategy {name} {'ENABLED' if enabled else 'DISABLED'} "
                    f"by operator"
                )
                return {"ok": True, "strategy": member.describe()}
            if cmd == "alerts":
                return {"ok": True, "alerts": engine.alerts.recent(
                    int(body.get("limit") or 20))}
            return {"ok": False, "error": f"unknown cmd {cmd!r}"}
        except Exception as exc:  # noqa: BLE001
            log.exception("command failed: %s", cmd)
            return {"ok": False, "error": str(exc)}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._httpd = _QuietHTTPServer((self.host, self.port), self._handler_cls)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="web-terminal"
        )
        self._thread.start()
        # port 0 asks the OS for a free one; the port it picked is the only
        # address that actually works, and ":0" is not an address.
        bound = self._httpd.server_address[1] or self.port
        log.info("web terminal live at http://%s:%d", self.host, bound)

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
