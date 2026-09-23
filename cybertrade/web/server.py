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
        # Phase-23: score-bay jobs (one at a time) + bus mute while a temp
        # gauntlet engine runs so its ticks never flicker the live chart.
        self._job: Dict[str, Any] = {"state": "idle"}
        self._job_lock = threading.Lock()
        self._mute = threading.Event()
        default_bus.subscribe(Topic.LOG, self._on_log)
        for topic in (Topic.TICK, Topic.SIGNAL, Topic.SETTLE, Topic.REGIME,
                      Topic.ENGINE_STATE, Topic.RISK_REJECT, Topic.KILL,
                      Topic.FILL, Topic.ALERT, Topic.NEWS, Topic.BALANCE,
                      Topic.SCENARIO):
            default_bus.subscribe(topic, self._make_forwarder(topic.value))

    def _make_forwarder(self, name: str):
        def _forward(event) -> None:
            if self._mute.is_set():  # temp gauntlet engine — do not cross-wire
                return
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
        if self._mute.is_set():
            return
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
            "scenarios": scenarios,
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
            "drill": self.engine.drill_report(),
            "session": self.engine.session_report(),
            "job": self.job_report(),
        }

    def job_report(self) -> Dict[str, Any]:
        """Phase-23: async score-bay job status (idle/running/done/error)."""
        with self._job_lock:
            return dict(self._job)

    def start_job(self, kind: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Launch a backtest/gauntlet off the request thread (one at a time)."""
        if kind not in ("backtest", "gauntlet"):
            return {"ok": False, "error": f"unknown job {kind!r}"}
        opts = dict(opts or {})
        with self._job_lock:
            if self._job.get("state") == "running":
                return {"ok": False, "error": "a job is already running",
                        "job": dict(self._job)}
            self._job = {"state": "running", "kind": kind,
                         "started": timex.now(), "opts": opts}
        threading.Thread(target=self._run_job, args=(kind, opts),
                         daemon=True, name=f"score-{kind}").start()
        return {"ok": True, "job": dict(self._job)}

    def _run_job(self, kind: str, opts: Dict[str, Any]) -> None:
        """Worker: the live engine is never touched — gauntlet gets a temp one."""
        import os
        import tempfile

        from ..config import AppConfig

        started = timex.now()
        try:
            if kind == "backtest":
                from ..backtest import Backtester, matrix_card, matrix_table

                cfg = self.engine.config or AppConfig()
                bt = Backtester(cfg)
                results = bt.run_matrix(
                    scenarios=list(opts["scenarios"]) if opts.get("scenarios") else None,
                    bars=int(opts.get("bars") or 200),
                    seeds=tuple(opts.get("seeds") or (1, 7, 42)),
                )
                payload = {
                    "table": matrix_table(results),
                    "card": matrix_card(results, payout=cfg.broker.payout_default),
                    "runs": len(results),
                    "alive": sum(1 for r in results if r.survived),
                }
            else:  # gauntlet — isolated temp engine, live stack untouched
                from ..bot.drills import CRISIS_SCENARIOS, run_gauntlet
                from ..bot.engine import TradingEngine
                from ..data.feed import SyntheticFeed
                from ..execution.paper import PaperBroker

                names = tuple(opts.get("scenarios") or CRISIS_SCENARIOS)
                cfg = AppConfig()
                self._mute.set()  # its bus traffic must not reach the HUD
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        cfg.journal_path = os.path.join(tmp, "score-journal.db")
                        cfg.calibration_path = os.path.join(tmp, "score-cal.json")
                        eng = TradingEngine(
                            cfg, feed=SyntheticFeed(),
                            broker=PaperBroker(starting_balance=1000.0),
                        )
                        eng.boot()
                        try:
                            rows = run_gauntlet(
                                eng, scenarios=names,
                                ticks=int(opts.get("ticks") or 150),
                                seed=int(opts.get("seed") or 1337),
                            )
                        finally:
                            eng.shutdown()
                finally:
                    self._mute.clear()
                payload = {"rows": rows, "runs": len(rows)}
            with self._job_lock:
                self._job = {"state": "done", "kind": kind, "started": started,
                             "finished": timex.now(), "result": payload}
        except Exception as exc:  # noqa: BLE001 — surface, never crash the hub
            self._mute.clear()
            with self._job_lock:
                self._job = {"state": "error", "kind": kind, "started": started,
                             "finished": timex.now(), "error": str(exc)}

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
                "state": state,
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
                    return self._json(200, terminal.hub.montecarlo(
                        runs=runs, horizon=horizon,
                        mode=parse_qs(urlparse(self.path).query).get("mode", ["bootstrap"])[0],
                    ))
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
            if cmd == "close":
                pos_id = str(body.get("position") or "")
                if not pos_id:
                    return {"ok": False, "error": "position id required"}
                if not engine.broker.close_position(pos_id):
                    return {"ok": False,
                            "error": f"position {pos_id!r} not closable"}
                # cash moved in the broker; flush the pending settlement so
                # ledger, journal, and HUD see the cut immediately (P19 path)
                try:
                    engine.oms.pump()
                except Exception:  # noqa: BLE001 — cycle will flush it
                    pass
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
            if cmd == "drill":
                from ..bot.drills import CRISIS_SCENARIOS

                scenario = str(body.get("scenario") or "")
                if scenario in ("", "stop"):
                    engine.drill.disarm()
                    return {"ok": True, "drill": engine.drill_report()}
                if scenario not in CRISIS_SCENARIOS:
                    return {"ok": False, "error": f"unknown drill {scenario!r}"}
                engine.drill.arm(scenario, assets=engine.feed.assets,
                                 ticks=int(body.get("ticks") or 250))
                from ..events import Topic as _T

                default_bus.publish(_T.SCENARIO, {"drill": scenario}, source="web")
                return {"ok": True, "drill": engine.drill_report()}
            if cmd == "run":
                kind = str(body.get("kind") or "")
                opts = body.get("opts") if isinstance(body.get("opts"), dict) else {}
                return self.hub.start_job(kind, opts)
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
