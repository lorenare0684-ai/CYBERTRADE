"""CYBERTRADE command-line interface.

    python -m cybertrade gui             desktop terminal
    python -m cybertrade web             browser terminal (live preview)
    python -m cybertrade run             paper-trade headless
    python -m cybertrade backtest        run the all-weather gauntlet
    python -m cybertrade edge            binary-options edge calculator
    python -m cybertrade optimize        walk-forward parameter search
    python -m cybertrade journal         trade journal analytics
    python -m cybertrade strategies      list the strategy matrix
    python -m cybertrade scenarios       list stress scenarios
    python -m cybertrade doctor          environment self-test
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import List, Optional

from . import __version__
from .config import AppConfig
from .logging_setup import setup_logging
from .quant.binary import breakeven_winrate, edge_of, kelly_fraction_for, kelly_stake

log = logging.getLogger("cybertrade.cli")

BANNER = r"""
 ██████╗██╗   ██╗██████╗ ███████╗██████╗ ████████╗██████╗  █████╗ ██████╗ ███████╗
██╔════╝╚██╗ ██╔╝██╔══██╗██╔════╝██╔══██╗╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝
██║      ╚████╔╝ ██████╔╝█████╗  ██████╔╝   ██║   ██████╔╝███████║██║  ██║█████╗
██║       ╚██╔╝  ██╔══██╗██╔══╝  ██╔══██╗   ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝
╚██████╗   ██║   ██████╔╝███████╗██████╔╝   ██║   ██║  ██║██║  ██║██████╔╝███████╗
 ╚═════╝   ╚═╝   ╚═════╝ ╚══════╝╚═════╝    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝
                        // NEON PROTOCOL — ALL-WEATHER OPS
"""


def _load_config(args: argparse.Namespace) -> AppConfig:
    cfg = AppConfig.load(getattr(args, "config", None))
    setup_logging(level=cfg.display.log_level, log_file=cfg.log_path)
    return cfg


def _build_venue(cfg: AppConfig, api_factory=None, allow_orders: bool = False):
    """Build a QuotexBroker with a live session, or None.

    Session sources (nothing is ever written to disk): in-memory
    ``cfg.broker.ssid`` / ``--ssid``, then the ``QX_SSID`` env var; else
    ``username``+``password`` via ``login()``.  Any failure degrades to
    ``None`` — paper quotes, zero drama.  ``allow_orders`` is the dry-run
    rail: leave it False for data-only venues.
    """
    import os

    try:
        if api_factory is None:
            from .brokers.quotex.adapter import QuotexBroker
            from .brokers.quotex.api import QuotexAPI

            def api_factory() -> Any:
                return QuotexAPI(demo=cfg.broker.demo_account)

        api = api_factory()
        ssid = cfg.broker.ssid or os.environ.get("QX_SSID", "")
        if ssid:
            api.set_ssid(ssid)
        elif cfg.broker.username and cfg.broker.password:
            api.login(cfg.broker.username, cfg.broker.password,
                      is_demo=cfg.broker.demo_account)
        if hasattr(api, "connect"):
            api.connect()
        from .brokers.quotex.adapter import QuotexBroker

        return QuotexBroker(api, allow_orders=allow_orders)
    except Exception as exc:  # noqa: BLE001 — venue trouble degrades to paper
        print(f"  venue session unavailable ({exc}) — paper quotes")
        return None


def _confirm_live(args: argparse.Namespace, cfg: AppConfig) -> bool:
    """The I-UNDERSTAND gate.  Returns False to abort the run.

    Runs BEFORE the engine is built — ``allow_orders`` rides on
    ``cfg.risk.allow_live`` at construction time.
    """
    if not getattr(args, "live", False):
        return True
    print("  ⚠ LIVE TRADING REQUESTED — this uses real money if configured.")
    print("  ⚠ Automated trading may violate Quotex's Terms of Service.")
    if not getattr(args, "yes", False):
        answer = input("  type 'I UNDERSTAND' to continue: ")
        if answer.strip() != "I UNDERSTAND":
            print("  aborted.")
            return False
    cfg.risk.allow_live = True
    return True


def cmd_quotex(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    if getattr(args, "ssid", ""):
        cfg.broker.ssid = args.ssid  # session-only: never written to disk
    venue = _build_venue(cfg)
    if venue is None:
        print("  no venue session — set QX_SSID, pass --ssid, or configure "
              "broker.username/password")
        return 1
    api = venue.api
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


def _build_engine(cfg: AppConfig, scenario: str = ""):
    from .bot.engine import TradingEngine
    from .data.feed import SyntheticFeed
    from .data.synthetic import MarketParams

    scenarios = {}
    names = ("bull_trend", "range_chop", "flash_crash", "bear_trend")
    for i, asset in enumerate(cfg.strategy.universe):
        scenarios[asset] = scenario or names[i % len(names)]
    feed = SyntheticFeed(
        assets=cfg.strategy.universe,
        scenarios=scenarios,
        timeframe_seconds=cfg.timeframe().seconds,
        tick_interval=0.5,
        warmup_bars=400,
    )
    broker = None
    if cfg.broker.mode == "dryrun":
        from .execution.dryrun import DryRunBroker

        venue = None
        import os
        has_creds = bool(
            cfg.broker.ssid or cfg.broker.username or os.environ.get("QX_SSID")
        )
        if has_creds:
            venue = _build_venue(cfg)
        if venue is None:
            print("  dry-run: no venue session — catalog quotes, paper fills")
        broker = DryRunBroker(
            venue=venue,
            starting_balance=cfg.risk.starting_balance,
            default_payout=cfg.broker.payout_default,
            latency_ms=cfg.broker.latency_ms,
            slippage_bps=cfg.broker.slippage_bps,
        )
    elif cfg.broker.mode == "quotex":
        # The live wire.  Defense in depth: allow_orders rides on
        # cfg.risk.allow_live, which _confirm_live sets only after the
        # 'I UNDERSTAND' gate — without it this degrades to dry-run
        # (venue quotes, paper fills) and says so.
        live_ok = bool(cfg.risk.allow_live)
        venue = _build_venue(cfg, allow_orders=live_ok)
        if venue is None:
            print("  quotex mode: no venue session — paper broker")
        elif not live_ok:
            print("  ⚠ mode=quotex without --live — running as DRY-RUN "
                  "(venue quotes, paper fills).")
            from .execution.dryrun import DryRunBroker

            broker = DryRunBroker(
                venue=venue,
                starting_balance=cfg.risk.starting_balance,
                default_payout=cfg.broker.payout_default,
                latency_ms=cfg.broker.latency_ms,
                slippage_bps=cfg.broker.slippage_bps,
            )
        else:
            print("  ⚠ QUOTEX LIVE — real orders enabled at the venue.")
            broker = venue
    engine = TradingEngine(cfg, feed=feed, broker=broker)
    engine.boot()
    return engine


def cmd_gui(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    from .gui import GUI_AVAILABLE, run_app

    if not GUI_AVAILABLE:
        print("tkinter unavailable — install python3-tk or use `python -m cybertrade web`",
              file=sys.stderr)
        return 2
    print(BANNER)
    engine = _build_engine(cfg, getattr(args, "scenario", ""))
    try:
        run_app(engine, cfg)
    finally:
        engine.shutdown()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    print(BANNER)
    host = args.host or cfg.display.web_host
    port = args.port or cfg.display.web_port
    engine = _build_engine(cfg, getattr(args, "scenario", ""))
    from .web.server import EngineHub, WebTerminal

    hub = EngineHub(engine, cfg)
    web = WebTerminal(hub, host=host, port=port)
    web.start()
    print(f"  ▸ web terminal : http://{host}:{port}")
    print("  ▸ mode         : PAPER (simulated market)")
    print("  ▸ ctrl+c to stop\n")
    try:
        if args.auto:
            engine.arm(live=False)
            print("  ▸ engine ARMED (paper)\n")
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n  shutting down…")
    finally:
        web.stop()
        engine.shutdown()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    if getattr(args, "dry_run", False):
        cfg.broker.mode = "dryrun"
        print("  DRY RUN — venue quotes (if configured), paper fills, live orders disabled.")
    print(BANNER)
    if not _confirm_live(args, cfg):
        return 1
    engine = _build_engine(cfg, getattr(args, "scenario", ""))
    live = bool(args.live)
    engine.arm(live=live)
    print(f"  engine ARMED ({'LIVE' if live else 'PAPER'}) — ctrl+c to stop\n")
    try:
        while True:
            time.sleep(5.0)
            snap = engine.snapshot()
            h = snap["health"]
            a = snap["account"]
            print(
                f"  [{h['engine_state']:^8}] posture {h['posture']:<8} "
                f"bal {a['balance']:>8.2f} wr {h['win_rate'] * 100:4.1f}% "
                f"trades {h['trades_total']} open {a['open_positions']} "
                f"dd {a['drawdown'] * 100:.1f}%"
            )
    except KeyboardInterrupt:
        print("\n  disarming…")
    finally:
        engine.shutdown()
    return 0


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
              f"{r['hit_rate'] * 100:>5.1f}% {r['p_edge_negative'] * 100:>8.1f}%  {flag}")
    print(f"  {len(rows)} strategies · {liars} flagged · liar = "
          f"P(true edge < breakeven) > 50%")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    from .backtest import Backtester, matrix_card, matrix_table

    bt = Backtester(cfg)
    scenarios = [args.scenario] if args.scenario else None
    print(BANNER)
    print("  … running all-weather gauntlet (every market condition)\n")
    results = bt.run_matrix(scenarios=scenarios, bars=args.bars, seeds=(1, 7, 42))
    print(matrix_table(results))
    print(matrix_card(results, payout=cfg.broker.payout_default))
    alive = sum(1 for r in results if r.survived)
    print(f"\n  survival: {alive}/{len(results)} scenario-seed pairs ended alive")
    avg = sum(r.report.survival_score for r in results) / max(1, len(results))
    print(f"  mean survival score: {avg:.3f}")
    if args.out:
        import json

        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump([r.to_dict() for r in results], fh, indent=2)
        print(f"  report written to {args.out}")
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    from .backtest.optimize import WalkForwardOptimizer

    print(BANNER)
    opt = WalkForwardOptimizer(scenario=args.scenario)
    print(f"  … walk-forward search on {args.scenario}\n")
    result = opt.search(seed=args.seed)
    for trial in result.leaderboard():
        print(
            f"  test {trial.test_score:.3f} train {trial.train_score:.3f} "
            f"gap {trial.overfit_gap:+.3f}  {trial.params}"
        )
    if result.best:
        print(f"\n  best honest params: {result.best.params}")
        if result.best.overfit_gap > opt.max_overfit_gap:
            print("  ⚠ all candidates look overfit — distrust this leaderboard")
    return 0


def cmd_montecarlo(args: argparse.Namespace) -> int:
    """Bootstrap a P&L sample forward and report survivability honestly."""
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
    if args.pnl:
        pnls = [float(x) for x in args.pnl.split(",") if x.strip()]
        label = f"custom sample ({len(pnls)} P&L points)"
    else:
        # deterministic synthetic sample: wins +stake*payout, losses -stake
        stake = 10.0
        breakeven_wr = 1.0 / (1.0 + payout)
        wr = min(0.95, max(0.05, breakeven_wr + float(args.edge)))
        n = 200
        wins = int(round(n * wr))
        pnls = [stake * payout] * wins + [-stake] * (n - wins)
        # deterministic shuffle so equal seeds give equal paths
        step = 7
        order = sorted(range(n), key=lambda i: (i * step) % n)
        pnls = [pnls[i] for i in order]
        label = f"synthetic wr={wr:.1%} payout={payout:.2f} n={n}"
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
    print(f"  … Monte Carlo risk lab — {label}\n")
    print(report.summary_text())
    print(f"\n  verdict: {report.verdict()}")
    print("  (ruin line = 50% of start — fixed-fraction sizing never hits literal zero)")
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    """Show news blackouts from the calendar file or estimated releases."""
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
        import json

        print(json.dumps({
            "now": now,
            "blackout": cal.is_blackout(now),
            "events": [e.to_dict() for e in events],
        }, indent=2))
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


def cmd_journal(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    from .analytics import journal_report  # may not exist; use package path
    return 0


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


def cmd_scenarios(args: argparse.Namespace) -> int:
    from .backtest.scenarios import describe_all

    print(BANNER)
    for row in describe_all():
        print(f"  {row['name']:<20} {row.get('description', '')}")
        print(f"  {'':<20} expect: {row.get('expect', '')}")
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

    def _indicators():
        from .indicators import list_indicators

        return f"{len(list_indicators())} registered"

    check("indicator registry", _indicators)

    def _strategies():
        from .strategies import list_strategies

        return f"{len(list_strategies())} registered"

    check("strategy registry", _strategies)

    def _sim():
        from .data.synthetic import generate_candles

        return f"{len(generate_candles('flash_crash', bars=30))} candles"

    check("synthetic markets", _sim)

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

    def _paper():
        from .execution.paper import PaperBroker

        b = PaperBroker(100)
        b.connect()
        return f"paper broker {b.name}"

    check("paper venue", _paper)

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

    failed = [c for c in checks if not c[1]]
    print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cybertrade",
        description="CYBERTRADE // NEON PROTOCOL — all-weather trading terminal",
    )
    p.add_argument("--version", action="version", version=f"cybertrade {__version__}")
    p.add_argument("-c", "--config", help="path to config json", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gui", help="desktop cyberpunk terminal")
    g.add_argument("--scenario", default="", help="synthetic market scenario")
    g.set_defaults(func=cmd_gui)

    w = sub.add_parser("web", help="browser cyberpunk terminal")
    w.add_argument("--host", default=None)
    w.add_argument("--port", type=int, default=None)
    w.add_argument("--scenario", default="")
    w.add_argument("--auto", action="store_true", help="auto-arm paper trading")
    w.set_defaults(func=cmd_web)

    r = sub.add_parser("run", help="headless paper/live trading")
    r.add_argument("--scenario", default="")
    r.add_argument("--live", action="store_true", help="DANGER: real order flow")
    r.add_argument("-y", "--yes", action="store_true", help="skip live confirmation")
    r.set_defaults(func=cmd_run)
    r.add_argument("--dry-run", action="store_true",
                   help="live venue quotes + paper fills; orders never reach the venue")

    b = sub.add_parser("backtest", help="run stress gauntlet")
    b.add_argument("--scenario", default="")
    b.add_argument("--bars", type=int, default=600)
    b.add_argument("--out", default="", help="write json report")
    b.set_defaults(func=cmd_backtest)

    eg = sub.add_parser("edge", help="binary-options edge calculator (payout math)")
    eg.add_argument("--payout", type=float, default=0.85, help="payout per win (0.85 = +85%%)")
    eg.add_argument("--winrate", type=float, default=None,
                    help="true/claimed P(win) to evaluate against the hurdle")
    eg.add_argument("--confidence", type=float, default=None,
                    help="a strategy's claimed confidence (for contrast only)")
    eg.add_argument("--bankroll", type=float, default=1000.0)
    eg.set_defaults(func=cmd_edge)

    o = sub.add_parser("optimize", help="walk-forward search")
    o.add_argument("--scenario", default="regime_whipsaw")
    o.add_argument("--seed", type=int, default=99)
    o.set_defaults(func=cmd_optimize)

    mc = sub.add_parser("montecarlo", help="Monte Carlo risk lab (bootstrap P&L)")
    mc.add_argument("--runs", type=int, default=2000, help="simulation paths")
    mc.add_argument("--horizon", type=int, default=200, help="trades per path")
    mc.add_argument("--starting-balance", type=float, default=1000.0)
    mc.add_argument("--pnl", default="", help="comma-separated P&L sample (else built-in)")
    mc.add_argument("--edge", type=float, default=0.0,
                    help="win-rate edge over breakeven for a synthetic sample")
    mc.add_argument("--payout", type=float, default=0.85)
    mc.add_argument("--json", action="store_true")
    mc.set_defaults(func=cmd_montecarlo)
    mc.add_argument("--wins", type=int, default=None,
                    help="posterior mode: settled wins in the record")
    mc.add_argument("--losses", type=int, default=None,
                    help="posterior mode: settled losses in the record")
    clb = sub.add_parser("calibrate", help="calibration honesty ledger (persisted)")
    clb.add_argument("--path", default="",
                     help="ledger path (default: config calibration_path)")
    clb.add_argument("--payout", type=float, default=0.85,
                     help="payout hurdle for liar flags")
    clb.set_defaults(func=cmd_calibrate)
    qx = sub.add_parser("quotex", help="venue session: status / warm history")
    qx.add_argument("action", choices=["status", "warm"])
    qx.add_argument("--ssid", default="",
                    help="session cookie (session-only, never stored)")
    qx.add_argument("--bars", type=int, default=250,
                    help="candles per asset for warm")
    qx.set_defaults(func=cmd_quotex)

    cal = sub.add_parser("calendar", help="economic calendar / news blackouts")
    cal.add_argument("--days", type=float, default=7.0, help="horizon in days")
    cal.add_argument("--year", type=int, default=0, help="estimate releases for YEAR")
    cal.add_argument("--json", action="store_true")
    cal.set_defaults(func=cmd_calendar)

    j = sub.add_parser("journal", help="journal analytics")
    j.set_defaults(func=cmd_journal_real)


    s = sub.add_parser("strategies", help="list strategies")
    s.set_defaults(func=cmd_strategies)

    sc = sub.add_parser("scenarios", help="list stress scenarios")
    sc.set_defaults(func=cmd_scenarios)

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
        print(f"confidence    : {args.confidence:.2f}  <- a CLAIM, not P(win);"
              f" the engine gates on calibrated P(win) from its own ledger.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
