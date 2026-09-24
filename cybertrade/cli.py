"""CYBERTRADE command-line interface.

    python -m cybertrade quotex login    pair a browser session (Chrome + your CAPTCHA)
    python -m cybertrade quotex status   venue session / balance / payouts
    python -m cybertrade quotex warm     pre-pull venue candles for the universe
    python -m cybertrade run             LIVE headless trading at the venue
    python -m cybertrade web             LIVE browser terminal
    python -m cybertrade gui             LIVE desktop terminal
    python -m cybertrade journal         trade journal analytics
    python -m cybertrade strategies      list the strategy matrix
    python -m cybertrade calibrate       calibration honesty ledger
    python -m cybertrade edge            binary-options edge calculator
    python -m cybertrade montecarlo      Monte Carlo risk lab (real records only)
    python -m cybertrade calendar        news blackouts
    python -m cybertrade doctor          environment self-test

**LIVE ONLY.**  There is no paper mode, no dry-run mode and no synthetic
market in this build.  Every trading command connects to the real venue and
places real orders; the two things standing between you and that are a valid
venue session and the ``I UNDERSTAND`` confirmation.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import List, Optional

from . import __version__
from .config import AppConfig
from .exceptions import ConfigError
from .logging_setup import setup_logging
from .quant.binary import breakeven_winrate, edge_of, kelly_fraction_for, kelly_stake

log = logging.getLogger("cybertrade.cli")

BANNER = r"""
 ██████╗██╗   ██╗██████╗ ███████╗██████╗ ████████╗██████╗  █████╗ ██████╗ ███████╗
██╔════╝╚██╗ ██╔╝██╔══██╗██╔════╝██╔══██╗╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝
██║      ╚████╔╝ ██████╔╝█████╗  ██████╔╝   ██║   ██████╔╝███████║██║  ██║█████╗
██║       ╚██╔╝  ██╔══██╗██╔══╝  ██╔══██╗   ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝
╚██████╗   ██║   ██████╔╝███████╗██████╔╝   ██║   ██████╔╝███████║██║  ██║█████╔╝
 ╚═════╝   ╚═╝  ╚══════╝╚═════╝    ╚═════╝    ╚═╝   ╚═════╝ ╚══════╝ ╚═════╝
              // NEON PROTOCOL — LIVE ONLY, REAL ORDER FLOW
"""


def _load_config(args: argparse.Namespace) -> AppConfig:
    cfg = AppConfig.load(getattr(args, "config", None))
    setup_logging(level=cfg.display.log_level, log_file=cfg.log_path)
    return cfg


def _qx_api(cfg: AppConfig, demo: Optional[bool] = None):
    """Configured venue facade — ghost wire timings ride the construction.

    ``demo`` defaults to the configured purse; when no purse has been chosen
    yet, session tooling (login/status/warm) falls back to PRACTICE — data
    commands must never need a money decision.
    """
    from .brokers.quotex.api import QuotexAPI

    if demo is None:
        demo = cfg.broker.demo_account if cfg.broker.demo_account is not None else True
    return QuotexAPI(
        demo=bool(demo),
        ghost=cfg.broker.ghost_pace,
        order_think_ms=cfg.broker.order_think_ms,
        order_min_gap_ms=cfg.broker.order_min_gap_ms,
        max_orders_per_min=cfg.broker.max_orders_per_min,
    )


def _live_api(cfg: AppConfig, api_factory=None):
    """Connected QuotexAPI for the live build — raises, never degrades.

    Session sources: ``cfg.broker.ssid`` / ``--ssid`` → ``QX_SSID`` env →
    paired browser session (``quotex login``) → username/password login.
    """
    import os

    if api_factory is None:
        api_factory = lambda: _qx_api(cfg)  # noqa: E731

    api = api_factory()
    ssid = cfg.broker.ssid or os.environ.get("QX_SSID", "")
    cookies = ""
    if not ssid:
        from .brokers.quotex.pairing import load_session

        paired = load_session(getattr(cfg, "qx_session_path", "") or "")
        if paired:
            ssid = paired.get("ssid", "")
            cookies = paired.get("cookies", "")
    if ssid:
        api.set_ssid(ssid, cookies)
    elif cfg.broker.username and cfg.broker.password:
        api.login(cfg.broker.username, cfg.broker.password,
                  is_demo=bool(cfg.broker.demo_account))
    else:
        raise ConfigError(
            "no Quotex session — run `cybertrade quotex login` (Chrome opens; "
            "log in and solve the CAPTCHA once), or set QX_SSID / --ssid"
        )
    api.connect()
    return api


def _resolve_purse(cfg: AppConfig, args: argparse.Namespace) -> None:
    """Choose PRACTICE vs REAL — the one decision this build never defaults.

    ``--demo`` / ``--real`` on the command line, ``broker.demo_account`` in
    config, or an interactive prompt.  A non-interactive shell with no
    configured purse fails loudly rather than guessing with your money.
    """
    if getattr(args, "demo", False) and getattr(args, "real", False):
        raise ConfigError("pick one purse: --demo (practice) or --real (real money)")
    if getattr(args, "demo", False):
        cfg.broker.demo_account = True
    elif getattr(args, "real", False):
        cfg.broker.demo_account = False
    if cfg.broker.purse_chosen:
        return
    if not sys.stdin or not sys.stdin.isatty():
        raise ConfigError(
            "no purse chosen — pass --demo (PRACTICE balance) or --real (REAL "
            "money), or set broker.demo_account in the config file"
        )
    print("  ── WHICH PURSE? ─────────────────────────────────────────────")
    print("  [p] PRACTICE — real order flow, broker demo balance (no real money)")
    print("  [r] REAL     — real order flow, real account balance (REAL MONEY)")
    answer = input("  purse [p/r]: ").strip().lower()
    if answer in ("p", "practice", "demo"):
        cfg.broker.demo_account = True
    elif answer in ("r", "real", "money"):
        cfg.broker.demo_account = False
    else:
        raise ConfigError(f"no purse chosen (got {answer!r}) — pass --demo or --real")
    cfg.require_purse()


def _confirm_live(args: argparse.Namespace, cfg: AppConfig) -> bool:
    """The I-UNDERSTAND gate — the one human check this build keeps.

    Every trading command runs it, because every trading command is live.
    ``--yes`` skips the typing for scripted operators who already know.
    """
    print("  ⚠ LIVE TRADING — this places REAL orders at Quotex.")
    print(f"  ⚠ purse        : {cfg.broker.purse_label}")
    print("  ⚠ Automated trading may violate Quotex's Terms of Service.")
    if not getattr(args, "yes", False):
        answer = input("  type 'I UNDERSTAND' to continue: ")
        if answer.strip() != "I UNDERSTAND":
            print("  aborted.")
            return False
    cfg.risk.allow_live = True
    return True


def _build_engine(cfg: AppConfig, api_factory=None, *, durable=False):
    """Build the LIVE engine: venue session → venue candles → real orders.

    No paper fallback exists.  A missing/dead session, an empty venue
    history, or an unchosen purse all raise instead of silently degrading.
    """
    from .bot.engine import TradingEngine
    from .brokers.quotex.adapter import QuotexBroker
    from .data.livefeed import LiveQuotexFeed

    cfg.require_purse()  # the purse is never defaulted
    api = _live_api(cfg, api_factory=api_factory)
    feed = LiveQuotexFeed(
        api,
        assets=cfg.strategy.universe,
        timeframe_seconds=cfg.timeframe().seconds,
        warm_bars=400,
    )
    venue = QuotexBroker(api, allow_orders=True)
    try:
        adopted = venue.reconcile_venue()
        if adopted:
            print(f"  ⚠ reconciled {adopted} venue-open contract(s) "
                  "from a previous session.")
    except Exception:  # noqa: BLE001 — reconcile never blocks boot
        pass
    print(f"  ⚠ QUOTEX LIVE — real orders enabled · purse {cfg.broker.purse_label}")
    engine = TradingEngine(cfg, feed=feed, broker=venue, durable=durable)
    engine.boot()
    return engine


def _gui_missing_session(exc: Exception) -> bool:
    """True when the only thing wrong is that no venue session exists yet.

    Anything else (a dead venue, an unchosen purse, a corrupt config) still
    fails loudly — the gate is for the one recoverable case.
    """
    return "no quotex session" in str(exc).lower()


def _run_gui(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Open the desktop terminal, pairing a session first if none exists.

    A first run has no session, and an engine cannot boot without venue
    candles — which need a session. Rather than dead-ending the operator in a
    shell, the GUI opens its own pre-flight pairing window (``SessionGate``),
    pairs with Chrome, then builds the engine and opens the real terminal.
    """
    from .gui import GUI_AVAILABLE, run_app

    if not GUI_AVAILABLE:
        print("tkinter unavailable — install python3-tk or use `python -m cybertrade web`",
              file=sys.stderr)
        return 2
    try:
        engine = _build_engine(cfg, durable=True)
    except ConfigError as exc:
        if not _gui_missing_session(exc):
            raise
        print(f"  no venue session yet — opening the pairing window ({exc})")
        from .gui.session_gate import run_gate

        ready = []

        def _on_ready(ssid: str, purse: bool) -> None:
            cfg.broker.ssid = ssid               # session-only, never on disk
            cfg.broker.demo_account = purse
            ready.append(True)

        run_gate(cfg, on_ready=_on_ready)
        if not ready:
            return 1
        engine = _build_engine(cfg, durable=True)
    try:
        run_app(engine, cfg)
    finally:
        engine.shutdown()
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    _resolve_purse(cfg, args)
    print(BANNER)
    if not _confirm_live(args, cfg):
        return 1
    return _run_gui(cfg, args)


def cmd_web(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    _resolve_purse(cfg, args)
    print(BANNER)
    if not _confirm_live(args, cfg):
        return 1
    host = args.host or cfg.display.web_host
    port = args.port or cfg.display.web_port
    engine = _build_engine(cfg, durable=True)
    from .web.server import EngineHub, WebTerminal

    hub = EngineHub(engine, cfg)
    web = WebTerminal(hub, host=host, port=port)
    web.start()
    print(f"  ▸ web terminal : http://{host}:{port}")
    print(f"  ▸ mode         : LIVE VENUE CANDLES · purse {cfg.broker.purse_label}")
    print("  ▸ ctrl+c to stop\n")
    try:
        if args.auto:
            engine.arm()
            print("  ▸ engine ARMED (LIVE — real order flow)\n")
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n  shutting down…")
    finally:
        web.stop()
        engine.shutdown()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .constants import EngineState
    from .exceptions import KillSwitchEngaged
    from .shutdown import SAFETY_HOLD_EXIT, stop_on_sigterm
    from .statestore import StateError

    engine = None
    with stop_on_sigterm():
        try:
            cfg = _load_config(args)
            _resolve_purse(cfg, args)
            print(BANNER)
            if not _confirm_live(args, cfg):
                return 1
            engine = _build_engine(cfg, durable=True)
            engine.arm()
            print("  engine ARMED (LIVE — real order flow) — ctrl+c to stop\n")
            while True:
                time.sleep(5.0)
                if engine.state is EngineState.KILL or not engine._running:
                    print("  SAFETY HOLD — " + (engine.risk.state.kill_reason or engine.last_error))
                    return SAFETY_HOLD_EXIT
                snap = engine.snapshot()
                h, a = snap["health"], snap["account"]
                print(
                    f"  [{h['engine_state']:^8}] posture {h['posture']:<8} "
                    f"bal {a['balance']:>8.2f} wr {h['win_rate'] * 100:4.1f}% "
                    f"trades {h['trades_total']} open {a['open_positions']} "
                    f"dd {a['drawdown'] * 100:.1f}%"
                )
        except (ConfigError, KillSwitchEngaged, StateError) as exc:
            print(f"  SAFETY HOLD — {exc}", file=sys.stderr)
            return SAFETY_HOLD_EXIT
        except KeyboardInterrupt:
            print("\n  disarming…")
        finally:
            if engine is not None:
                engine.shutdown()
    return 0


def cmd_quotex(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    if getattr(args, "action", "") == "login":
        return cmd_quotex_login(args, cfg)
    if getattr(args, "ssid", ""):
        cfg.broker.ssid = args.ssid  # session-only: never written to disk
    try:
        api = _live_api(cfg)
    except Exception as exc:  # noqa: BLE001 — session trouble is a CLI error
        print(f"  venue session unavailable ({exc})")
        print("  no venue session — set QX_SSID, pass --ssid, run "
              "`cybertrade quotex login`, or configure broker.username/password")
        return 1
    from .brokers.quotex.adapter import QuotexBroker

    venue = QuotexBroker(api, allow_orders=False)
    if args.action == "status":
        try:
            api.request_instruments()
        except Exception:  # noqa: BLE001
            pass
        sess = getattr(api, "session", None)
        print(f"  connected : {getattr(api, 'connected', False)}")
        print(f"  host      : {getattr(sess, 'host', '?')}"
              f"  demo={getattr(sess, 'demo', '?')}")
        snap = api.account_snapshot() if hasattr(api, "account_snapshot") else None
        if snap is not None:
            print(f"  balance   : {getattr(snap, 'balance', 0.0):.2f}")
        print(f"  instruments: {len(getattr(api, 'assets', {}) or {})}")
        for asset in cfg.strategy.universe[:3]:
            print(f"  payout {asset}: {api.payout_for(asset, 60):.2f}")
        return 0
    from .brokers.quotex.sync import warm_universe

    total = warm_universe(
        api, cfg.strategy.universe,
        timeframe_seconds=cfg.timeframe().seconds,
        bars=int(getattr(args, "bars", 250)), wait=3.0,
    )
    print(f"  warmed {total} candles across {len(cfg.strategy.universe)} assets")
    return 0 if total else 1


def cmd_quotex_login(args: argparse.Namespace, cfg: AppConfig) -> int:
    """Chrome + your hands beat any headless login.

    Opens a persistent Chrome profile on qxbroker.com; you sign in and solve
    the CAPTCHA yourself; we detect the `sessionid` cookie over localhost
    DevTools and persist it (0600) for the websocket wire.
    """
    import os

    from .brokers.quotex.pairing import pair_session

    profile = getattr(args, "profile", "") or os.path.join("data", "chrome-profile")
    port = int(getattr(args, "cdp_port", 9333))
    timeout = float(getattr(args, "timeout", 240.0))
    chrome = getattr(args, "chrome", "")
    session_path = cfg.qx_session_path
    print(BANNER)
    print("  Chrome will open Quotex — log in and solve the CAPTCHA YOURSELF.")
    print("  This tool never bypasses the CAPTCHA; it only reads the session")
    print("  cookie Chrome grants after YOU sign in (DevTools, localhost only).")
    try:
        sess = pair_session(
            session_path=session_path,
            profile_dir=profile,
            port=port,
            chrome=chrome,
            timeout=timeout,
        )
    except (FileNotFoundError, TimeoutError, OSError) as exc:
        print(f"  ✗ {exc}")
        return 1
    print(f"  ✓ session saved → {session_path} (0600)")
    try:
        api = _qx_api(cfg)
        api.set_ssid(sess["ssid"], sess.get("cookies", ""))
        api.connect()
        snap = api.account_snapshot()
        print(f"  ✓ verified — balance {float(snap.balance):.2f}: live session ready")
        api.close()
        return 0
    except Exception as exc:  # noqa: BLE001 — report, don't discard the cookie
        print(f"  ⚠ captured, but venue verify failed: {exc}")
        print("    (session kept — `cybertrade quotex status` will retry)")
        return 1


def cmd_calibrate(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    from .quant.calibration import CalibrationTracker

    path = args.path or cfg.calibration_path
    tracker = CalibrationTracker()
    if not tracker.load(path):
        print(f"  no calibration ledger at {path!r} — run the engine "
              f"(it saves on shutdown)")
        return 1
    w, l = tracker.evidence()
    n = max(1, w + l)
    payout = float(args.payout)
    rows = tracker.honesty(payout, runs=2000)
    liars = sum(1 for r in rows if r["liar"])
    print(f"  ledger {path}")
    print(f"  evidence: {w}W/{l}L ({w / n * 100:.1f}%) | observations {tracker.observations}"
          f" | gap {tracker.calibration_gap():.4f} | payout {payout:.2f}"
          f" hurdle {1 / (1 + payout):.4f}")
    print(f"  {'strategy':<28} {'W':>4} {'L':>4} {'hit%':>6} {'P(edge<0)':>9}  flag")
    for r in rows[:40]:
        flag = "LIAR" if r["liar"] else ""
        print(f"  {r['strategy']:<28} {r['wins']:>4} {r['losses']:>4} "
              f"{r['hit_rate'] * 100:>5.1f}% {r['p_edge_negative'] * 100:>8.1f}%  "
              f"{flag}")
    print(f"  {len(rows)} strategies · {liars} flagged · liar = "
          f"P(true edge < breakeven) > 50%")
    return 0


def cmd_montecarlo(args: argparse.Namespace) -> int:
    """Bootstrap a real P&L sample forward and report survivability honestly.

    There is no synthetic sample in this build: pass your own settled P&L
    (``--pnl``) or the record's win/loss counts (``--wins`` / ``--losses``).
    """
    from .risk.montecarlo import simulate

    print(BANNER)
    payout = float(args.payout)
    if getattr(args, "wins", None) is not None or getattr(args, "losses", None) is not None:
        from .risk.montecarlo import simulate_posterior

        w = int(args.wins or 0)
        l = int(args.losses or 0)
        report = simulate_posterior(w, l, payout=payout)
        print(f"  posterior: Beta({w + 2}, {l + 2}) over P(win) | payout {payout:.2f}"
              f" | breakeven {1.0 / (1.0 + payout):.4f}")
        print(f"  {report.summary_text()}")
        print(f"  P(true edge < 0) = {report.p_edge_negative * 100:.1f}%"
              f"  <- if this is large, the record is a liar")
        for n in report.notes:
            print(f"  note: {n}")
        return 0
    if not args.pnl:
        print("  no sample given — this build will not invent one.", file=sys.stderr)
        print("  pass --pnl \"+8.5,-10,+8.5,...\" (settled trades) or "
              "--wins/--losses from your journal.", file=sys.stderr)
        return 2
    pnls = [float(x) for x in args.pnl.split(",") if x.strip()]
    report = simulate(
        pnls,
        starting_balance=float(args.starting_balance),
        runs=int(args.runs),
        horizon=int(args.horizon),
    )
    if args.json:
        import json

        data = report.to_dict()
        data["verdict"] = report.verdict()
        print(json.dumps(data, indent=2))
        return 0
    print(f"  … Monte Carlo risk lab — custom sample ({len(pnls)} P&L points)\n")
    print(report.summary_text())
    print(f"\n  verdict: {report.verdict()}")
    print("  (ruin line = 50% of start — fixed-fraction sizing never hits literal zero)")
    return 0


def cmd_calendar(args: argparse.Namespace) -> None:
    from .bot.calendar import EconomicCalendar
    from .utils import timex

    print(BANNER)
    if args.year:
        cal = EconomicCalendar.estimate_year(int(args.year))
        print(f"  … estimated red releases for {args.year} (labelled 'est')\n")
    else:
        cal = EconomicCalendar.load()
        print("  … economic calendar (calendar.json or estimates)\n")
    now = timex.now()
    horizon = float(args.days) * 86400.0
    events = cal.upcoming(now, horizon)
    if args.json:
        print(json_dumps({
            "now": now,
            "blackout": cal.is_blackout(now),
            "events": [e.to_dict() for e in events],
        }))
        return 0
    if cal.is_blackout(now):
        print("  ⚠ BLACKOUT ACTIVE RIGHT NOW — entries frozen\n")
    if not events:
        print(f"  no events in the next {args.days:g} days")
        return 0
    for e in events:
        mark = "*" if e.estimated else " "
        flag = " [BLACKOUT]" if e.active_at(now) else ""
        print(
            f"  {timex.iso(e.ts)}{mark} {e.currency:<4} {e.impact:<6} "
            f"{e.title}{flag}"
        )
    print(f"\n  {len(events)} events · * = estimated time (see README honest notes)")
    return 0


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, indent=2)


def cmd_journal_real(args: argparse.Namespace) -> int:
    import json

    cfg = _load_config(args)
    from .journal import TradeJournal, journal_report

    journal = TradeJournal(cfg.journal_path)
    report = journal_report(journal)
    if report.get("empty"):
        print(f"  journal {cfg.journal_path} has no closed trades yet")
        return 0
    print(BANNER)
    print(json.dumps(report, indent=2)[:4000])
    return 0


def cmd_strategies(args: argparse.Namespace) -> int:
    from .strategies import STRATEGY_REGISTRY, list_strategies

    print(BANNER)
    by_family: dict = {}
    for name in list_strategies():
        cls = STRATEGY_REGISTRY[name]
        by_family.setdefault(cls.family, []).append(name)
    for family, names in sorted(by_family.items()):
        print(f"  [{family.upper()}]")
        for name in sorted(names):
            label = getattr(STRATEGY_REGISTRY[name], "label", name)
            print(f"    {name:<22} {label}")
    print(f"\n  total: {len(list_strategies())} strategies + 1 ensemble")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(BANNER)
    checks = []

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            checks.append((name, True, detail or "ok"))
            print(f"  ✓ {name:<28} {detail or 'ok'}")
        except Exception as exc:  # noqa: BLE001
            checks.append((name, False, str(exc)))
            print(f"  ✗ {name:<28} {exc}")

    check("python >= 3.10", lambda: sys.version.split()[0])
    check("config validate", lambda: AppConfig().validate() or "valid")

    def _live_defaults():
        cfg = AppConfig()
        if cfg.broker.mode != "quotex" or not cfg.risk.allow_live:
            raise RuntimeError("live-only defaults drifted")
        return "live-only defaults (mode=quotex, allow_live=on)"

    check("live-only config", _live_defaults)

    def _indicators():
        from .indicators import list_indicators

        return f"{len(list_indicators())} registered"

    check("indicator registry", _indicators)

    def _strategies():
        from .strategies import list_strategies

        return f"{len(list_strategies())} registered"

    check("strategy registry", _strategies)

    def _venue_wire():
        from .brokers.quotex.adapter import QuotexBroker
        from .brokers.quotex.api import QuotexAPI

        api = QuotexAPI(demo=True)
        broker = QuotexBroker(api, allow_orders=True)
        if not broker.allow_orders:
            raise RuntimeError("venue broker must accept live orders")
        return f"{broker.name} ready (allow_orders)"

    check("venue wire", _venue_wire)

    def _livefeed():
        from .data.livefeed import LiveQuotexFeed

        if LiveQuotexFeed.is_synthetic:
            raise RuntimeError("the live feed must not be synthetic")
        return "venue candles only"

    check("live feed airlock", _livefeed)

    def _ws():
        from .network.websocket import encode_frame

        encode_frame(b"x")
        return "rfc6455 codec ok"

    check("websocket codec", _ws)

    def _sio():
        from .network.socketio import encode_event

        assert encode_event("ping") == '42["ping"]'
        return "engine.io v3 codec ok"

    check("socket.io codec", _sio)

    def _pairing():
        from .brokers.quotex.pairing import chrome_argv

        argv = chrome_argv("https://qxbroker.com", "/tmp/profile", 9333,
                          "chrome")
        if "--remote-debugging-port=9333" not in argv:
            raise RuntimeError("devtools argv drifted")
        if "--user-data-dir=/tmp/profile" not in argv:
            raise RuntimeError("profile argv drifted")
        return "chrome pairing available"

    check("chrome pairing", _pairing)

    def _gui():
        from .gui import GUI_AVAILABLE

        return "tkinter available" if GUI_AVAILABLE else "missing (apt install python3-tk)"

    check("desktop gui", _gui)

    def _web():
        import os

        base = os.path.join(os.path.dirname(__file__), "web", "static")
        needed = ("index.html", "css/cyber.css", "js/app.js", "js/charts.js")
        missing = [f for f in needed if not os.path.isfile(os.path.join(base, f))]
        if missing:
            raise RuntimeError(f"missing {missing}")
        return "assets present"

    check("web terminal assets", _web)

    def _session():
        import os

        from .brokers.quotex.pairing import load_session

        paired = load_session("")
        if paired:
            return "paired browser session found"
        if os.environ.get("QX_SSID"):
            return "QX_SSID set"
        return "no session yet — run `cybertrade quotex login`"

    check("venue session", _session)

    failed = [c for c in checks if not c[1]]
    print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cybertrade",
        description="CYBERTRADE // NEON PROTOCOL — live-only all-weather trading terminal",
    )
    p.add_argument("--version", action="version", version=f"cybertrade {__version__}")
    p.add_argument("-c", "--config", help="path to config json", default=None)

    def purse_flags(sub: argparse.ArgumentParser) -> None:
        purse = sub.add_mutually_exclusive_group()
        purse.add_argument("--demo", action="store_true",
                           help="trade the PRACTICE balance (real orders, demo money)")
        purse.add_argument("--real", action="store_true",
                           help="trade the REAL balance (real orders, REAL MONEY)")

    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gui", help="desktop cyberpunk terminal (LIVE)")
    purse_flags(g)
    g.add_argument("-y", "--yes", action="store_true",
                   help="skip the I UNDERSTAND confirmation")
    g.set_defaults(func=cmd_gui)

    w = sub.add_parser("web", help="browser cyberpunk terminal (LIVE)")
    w.add_argument("--host", default=None)
    w.add_argument("--port", type=int, default=None)
    w.add_argument("--auto", action="store_true", help="auto-arm live trading")
    purse_flags(w)
    w.add_argument("-y", "--yes", action="store_true",
                   help="skip the I UNDERSTAND confirmation")
    w.set_defaults(func=cmd_web)

    r = sub.add_parser("run", help="headless LIVE trading at the venue")
    purse_flags(r)
    r.add_argument("-y", "--yes", action="store_true",
                   help="skip the I UNDERSTAND confirmation")
    r.set_defaults(func=cmd_run)

    qx = sub.add_parser("quotex", help="venue session: login / status / warm")
    qx.add_argument("action", choices=["status", "warm", "login"])
    qx.add_argument("--ssid", default="",
                    help="session cookie (session-only, never stored)")
    qx.add_argument("--bars", type=int, default=250,
                    help="candles per asset for warm")
    qx.add_argument("--profile", default="",
                    help="Chrome profile dir (default data/chrome-profile)")
    qx.add_argument("--chrome", default="",
                    help="Chrome binary (or set CYBERTRADE_CHROME)")
    qx.add_argument("--cdp-port", type=int, default=9333,
                    help="localhost DevTools port for pairing")
    qx.add_argument("--timeout", type=float, default=240.0,
                    help="seconds to wait for your manual login + CAPTCHA")
    qx.set_defaults(func=cmd_quotex)

    clb = sub.add_parser("calibrate", help="calibration honesty ledger (persisted)")
    clb.add_argument("--path", default="",
                     help="ledger path (default: config calibration_path)")
    clb.add_argument("--payout", type=float, default=0.85,
                     help="payout hurdle for liar flags")
    clb.set_defaults(func=cmd_calibrate)

    cal = sub.add_parser("calendar", help="economic calendar / news blackouts")
    cal.add_argument("--days", type=float, default=7.0, help="horizon in days")
    cal.add_argument("--year", type=int, default=0, help="estimate releases for YEAR")
    cal.add_argument("--json", action="store_true")
    cal.set_defaults(func=cmd_calendar)

    j = sub.add_parser("journal", help="journal analytics")
    j.set_defaults(func=cmd_journal_real)

    s = sub.add_parser("strategies", help="list strategies")
    s.set_defaults(func=cmd_strategies)

    eg = sub.add_parser("edge", help="binary-options edge calculator (payout math)")
    eg.add_argument("--payout", type=float, default=0.85, help="payout per win (0.85 = +85%%)")
    eg.add_argument("--winrate", type=float, default=None,
                    help="true/claimed P(win) to evaluate against the hurdle")
    eg.add_argument("--confidence", type=float, default=None,
                    help="a strategy's claimed confidence (for contrast only)")
    eg.add_argument("--bankroll", type=float, default=1000.0)
    eg.set_defaults(func=cmd_edge)

    mc = sub.add_parser("montecarlo", help="Monte Carlo risk lab (real records only)")
    mc.add_argument("--runs", type=int, default=2000, help="simulation paths")
    mc.add_argument("--horizon", type=int, default=200, help="trades per path")
    mc.add_argument("--starting-balance", type=float, default=1000.0)
    mc.add_argument("--pnl", default="", help="comma-separated settled P&L sample")
    mc.add_argument("--payout", type=float, default=0.85)
    mc.add_argument("--json", action="store_true")
    mc.add_argument("--wins", type=int, default=None,
                    help="posterior mode: settled wins in the record")
    mc.add_argument("--losses", type=int, default=None,
                    help="posterior mode: settled losses in the record")
    mc.set_defaults(func=cmd_montecarlo)

    d = sub.add_parser("doctor", help="environment self-test")
    d.set_defaults(func=cmd_doctor)
    return p


def cmd_edge(args: argparse.Namespace) -> int:
    """Binary-options edge calculator: the payout hurdle is brutal; see it."""
    b = args.payout
    be = breakeven_winrate(b)
    print(f"payout        : +{b * 100:.0f}%  (win pays {1 + b:.2f}x stake, loss costs 1.0x)")
    print(f"breakeven P   : {be:.4f}  <- must win MORE than this to profit")
    if args.winrate is not None:
        p = args.winrate
        e = edge_of(p, b)
        print(f"true P(win)   : {p:.4f}")
        print(f"edge per 1.0  : {e:+.4f}   ({'PROFITABLE' if e > 0 else 'LOSING'} long-run)")
        print(f"kelly stake   : {kelly_stake(p, b, args.bankroll):.2f} of {args.bankroll:.2f}"
              f"  (fraction {kelly_fraction_for(p, b):.4f})")
    if args.confidence is not None:
        print(f"confidence    : {args.confidence:.2f} <- a CLAIM, not P(win);"
              f" the engine gates on calibrated P(win) from its own ledger.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
