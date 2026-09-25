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
import os
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .compat import (
    IS_WINDOWS,
    default_chrome_profile,
    ensure_console_encoding,
)
from .shutdown import SAFETY_HOLD_EXIT
from .config import AppConfig
from .exceptions import BrokerError, ConfigError, FeedError, NetworkError
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


def _require_writable_data_dir(cfg: AppConfig) -> None:
    """The runtime writes under ``data/``; a protected install must say so.

    Windows operators extract the repo into ``C:\\Program Files``, a
    OneDrive-synced folder, or a locked-down profile often enough that this
    is a real first-run failure -- and a raw ``PermissionError`` traceback
    names the directory and nothing else, while the operator has no idea
    which of the six files it was trying to write.
    """
    paths = [cfg.log_path, cfg.journal_path, cfg.qx_session_path,
             cfg.calibration_path, cfg.operator_path,
             cfg.continuity_path, cfg.heartbeat_path,
             getattr(cfg, "config_path", "")]
    checked = set()
    for path in paths:
        parent = os.path.dirname(os.path.abspath(path))
        if not parent or parent in checked:
            continue
        checked.add(parent)
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"cannot create the data directory {parent} ({exc.strerror or exc}). "
                f"Install CYBERTRADE somewhere writable -- not C:\\Program Files "
                f"or a synced folder -- or point --config at a writable location.")
        if not os.access(parent, os.W_OK):
            raise ConfigError(
                f"the data directory {parent} is not writable. Install "
                f"CYBERTRADE somewhere writable -- not C:\\Program Files or a "
                f"synced folder -- or point --config at a writable location.")


def _load_config(args: argparse.Namespace) -> AppConfig:
    cfg = AppConfig.load(getattr(args, "config", None))
    # Checked before logging so the message the operator reads is about the
    # install directory, not about a log file that could not be opened.
    _require_writable_data_dir(cfg)
    setup_logging(level=cfg.display.log_level, log_file=cfg.log_path)
    return cfg


def _qx_api(cfg: AppConfig, demo: Optional[bool] = None):
    """Configured venue facade — ghost wire timings ride the construction.

    ``demo`` defaults to the configured purse; when no purse has been chosen
    yet, session tooling (login/status/warm) falls back to PRACTICE — data
    commands must never need a money decision.
    """
    from .brokers.quotex import constants as QXC
    from .brokers.quotex.api import QuotexAPI

    if demo is None:
        demo = cfg.broker.demo_account if cfg.broker.demo_account is not None else True
    return QuotexAPI(
        http_base=cfg.broker.http_base or QXC.HTTP_BASE,
        ws_url=cfg.broker.ws_url or QXC.WS_URL,
        demo=bool(demo),
        ghost=cfg.broker.ghost_pace,
        order_think_ms=cfg.broker.order_think_ms,
        order_min_gap_ms=cfg.broker.order_min_gap_ms,
        max_orders_per_min=cfg.broker.max_orders_per_min,
        reconnect_max=cfg.broker.reconnect_max,
        timeout=cfg.broker.request_timeout,
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
    try:
        answer = input("  purse [p/r]: ").strip().lower()
    except EOFError:
        # A closed or redirected stdin is not a "no": it is an operator who
        # never got asked. Guessing PRACTICE here would trade with money they
        # did not choose to risk, so this is a hold, not a default.
        raise ConfigError(
            "no purse chosen — stdin gave no answer (pass --demo or --real, "
            "or set broker.demo_account in the config file)") from None
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
        try:
            answer = input("  type 'I UNDERSTAND' to continue: ")
        except EOFError:
            # Same reasoning as the purse prompt: nobody confirmed, so nothing
            # trades. A closed stdin must hold, never proceed by default.
            raise ConfigError(
                "live confirmation not given — stdin gave no answer "
                "(pass --yes for scripted operators who already know)"
            ) from None
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


def _missing_session(exc: Exception) -> bool:
    """True when the only thing wrong is that no venue session exists yet.

    Anything else (a dead venue, an unchosen purse, a corrupt config) still
    fails loudly — both the GUI and the web terminal only offer pairing for
    this one recoverable case.
    """
    return "no quotex session" in str(exc).lower()


def _session_trouble(exc: Exception) -> bool:
    """True when an engine build failed for a re-pairable venue reason.

    No session, a rejected session, an unreachable venue, or a silent one —
    pairing again fixes all of them, so the terminals stay up and offer the
    pairing screen instead of dying with a traceback.
    """
    if isinstance(exc, (BrokerError, NetworkError, FeedError, OSError)):
        return True
    return isinstance(exc, ConfigError) and _missing_session(exc)


def _has_live_session(hub) -> bool:
    """True when the running engine holds a venue session right now.

    ``--ssid`` and ``QX_SSID`` never touch the disk, so the saved-file status
    alone would tell an operator staring at a live terminal that they have no
    session at all.
    """
    engine = getattr(hub, "engine", None)
    if engine is None:
        return False
    api = getattr(getattr(engine, "feed", None), "api", None)
    if api is None:
        return False
    # The venue api holds the cookie at session.ssid; test stubs hang it
    # directly off the api — accept either, or the probe always lies.
    sess = getattr(api, "session", None)
    ssid = getattr(sess, "ssid", "") if sess is not None else ""
    return bool(ssid or getattr(api, "ssid", ""))


def _reseat_session(engine, ssid: str) -> None:
    """Re-seat a live engine on a fresh session, with no restart.

    The api is the only thing that holds the cookie, so a re-pair mid-run is
    ``set_ssid`` + ``connect`` — the same verification ``quotex login`` does
    before it declares success. Nothing is rebuilt and no position is touched.
    """
    from .brokers.quotex.api import QuotexAPI

    api = getattr(engine.feed, "api", None)
    if api is None:
        raise ConfigError("this engine has no venue api to re-seat")
    if isinstance(api, QuotexAPI):
        api.set_ssid(ssid)
    else:  # a stub in tests: exercise the seam without the venue protocol
        setattr(api, "ssid", ssid)
    if not api.connect():
        raise ConfigError("the paired session was rejected by the venue")
    engine.health.note_message("venue session re-paired in place")


def _run_web(cfg: AppConfig, args: argparse.Namespace,
             port: Optional[int] = None) -> int:
    """Serve the browser terminal, pairing a session first if none exists.

    Same dead end as the GUI: no session means no venue candles, which means
    no engine. Rather than refusing to start, the terminal comes up with no
    engine, serves a pairing screen, and attaches one the moment a cookie
    lands. The operator stays in the browser the whole time.
    """
    from .web.pairing import PairingController
    from .web.server import EngineHub, WebTerminal

    host = args.host or cfg.display.web_host
    if port is None:
        port = _checked_port(args.port, cfg.display.web_port)
    pending: List[Any] = []

    def _on_ready(ssid: str, purse: bool) -> None:
        """Runs on the pairing worker thread — queue, never touch the hub."""
        cfg.broker.ssid = ssid          # session-only, never on disk
        cfg.broker.demo_account = purse
        pending.append(ssid)

    hub = EngineHub(None, cfg)
    hub.pairing = PairingController(
        cfg, _on_ready,
        probe=lambda: _has_live_session(hub))
    web = WebTerminal(hub, host=host, port=port)
    try:
        web.start()
    except OSError as exc:
        # A second terminal on the same port is the common case, and it is
        # an answerable question -- not a traceback.
        web.stop()
        print(f"  ✗ cannot listen on {host}:{port} — {exc}", file=sys.stderr)
        print("  another CYBERTRADE terminal is probably already using it;",
              file=sys.stderr)
        print("  pick another with --port N, or stop the other one first.",
              file=sys.stderr)
        return 1
    # --port 0 asks the OS for a free port; the one it picked is the only
    # useful thing to print, and ":0" is not an address anyone can visit.
    bound = getattr(getattr(web, "_httpd", None), "server_address", ("", port))[1]
    print(f"  ▸ web terminal : http://{host}:{bound}")

    engine = None
    try:
        try:
            engine = _build_engine(cfg, durable=True)
        except Exception as exc:  # noqa: BLE001 — a bad session must not kill the terminal
            if not _session_trouble(exc):
                raise
            log.exception("engine failed to boot — serving the pairing screen")
            print(f"  ▸ engine failed to boot ({exc})")
            print("  ▸ pair a venue session from the web terminal\n")
        else:
            hub.engine = engine
            print(f"  ▸ mode         : LIVE VENUE CANDLES · "
                  f"purse {cfg.broker.purse_label}")
            if getattr(engine, "degraded", ""):
                print(f"  ▸ NO VENUE DATA ({engine.degraded}) — "
                      "retrying in the background")
            if args.auto:
                engine.arm()
                print("  ▸ engine ARMED (LIVE — real order flow)\n")

        while True:
            time.sleep(1.0)
            if not pending:
                continue
            # a cookie landed on a worker thread: adopt it here, serially
            ssid = pending.pop(0)
            try:
                if hub.engine is None:
                    engine = _build_engine(cfg, durable=True)
                    hub.engine = engine
                    print(f"  ▸ session paired — purse "
                          f"{cfg.broker.purse_label}\n")
                    if getattr(engine, "degraded", ""):
                        print(f"  ▸ NO VENUE DATA ({engine.degraded}) — "
                              "retrying in the background")
                    if args.auto:
                        engine.arm()
                        print("  ▸ engine ARMED (LIVE — real order flow)\n")
                else:
                    _reseat_session(engine, ssid)
                    print("  ▸ venue session re-paired in place\n")
            except Exception as exc:  # noqa: BLE001 — a bad cookie must not kill the terminal
                log.exception("session adoption failed")
                print(f"  ▸ session adoption failed ({exc}) — pair again\n")
                try:
                    hub.pairing.note_error(str(exc) or exc.__class__.__name__)
                except Exception:  # noqa: BLE001 — the veil is best-effort
                    pass
    except KeyboardInterrupt:
        print("\n  shutting down…")
    finally:
        web.stop()
        if engine is not None:
            engine.shutdown()
    return 0


def _run_gui(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Open the desktop terminal, pairing a session first if none exists.

    A first run has no session, and an engine cannot boot without venue
    candles — which need a session. Rather than dead-ending the operator in a
    shell, the GUI opens its own pre-flight pairing window (``SessionGate``),
    pairs with Chrome, then builds the engine and opens the real terminal.
    """
    from .gui import GUI_AVAILABLE, run_app

    if not GUI_AVAILABLE:
        if sys.platform == "win32":
            hint = ("your Python has no tkinter — reinstall it with "
                    "\"tcl/tk and IDLE\" checked, or run `conda install tk` "
                    "in this environment")
        else:
            hint = "install python3-tk (Debian/Ubuntu) or the matching tk package"
        print(f"  ✗ desktop GUI unavailable: {hint},", file=sys.stderr)
        print("    or use the browser terminal instead: `python -m cybertrade web`",
              file=sys.stderr)
        return 2
    from .gui.session_gate import run_gate

    notice = ""
    while True:
        # Any venue-side failure lands back in the pairing window (with the
        # reason shown) instead of killing the process before it opens.
        try:
            engine = _build_engine(cfg, durable=True)
        except Exception as exc:  # noqa: BLE001 — pairing covers venue trouble
            if not _session_trouble(exc):
                raise
            log.exception("engine failed to boot — opening the pairing window")
            notice = str(exc) or exc.__class__.__name__
            print(f"  engine failed to boot ({notice})")
        else:
            break
        print("  opening the pairing window — pair again or quit")
        ready = []

        def _on_ready(ssid: str, purse: bool) -> None:
            cfg.broker.ssid = ssid               # session-only, never on disk
            cfg.broker.demo_account = purse
            ready.append(True)

        run_gate(cfg, on_ready=_on_ready, notice=notice)
        if not ready:
            return 1
    if getattr(engine, "degraded", ""):
        print(f"  NO VENUE DATA ({engine.degraded}) — retrying in the background")
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
    try:
        port = _checked_port(args.port, cfg.display.web_port)
    except ConfigError as exc:
        # A typo is a usage error, not a safety hold: exit 2 and say so.
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2
    _resolve_purse(cfg, args)
    print(BANNER)
    if not _confirm_live(args, cfg):
        return 1
    return _run_web(cfg, args, port=port)


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
            if getattr(engine, "degraded", ""):
                # Headless has no pairing screen: a blind engine holds here
                # instead of arming on an empty book.
                print(f"  SAFETY HOLD — no venue data ({engine.degraded})",
                      file=sys.stderr)
                return SAFETY_HOLD_EXIT
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
    if args.action == "assets":
        return cmd_quotex_assets(api)
    from .brokers.quotex.sync import warm_universe

    total = warm_universe(
        api, cfg.strategy.universe,
        timeframe_seconds=cfg.timeframe().seconds,
        bars=int(getattr(args, "bars", 250)), wait=3.0,
    )
    print(f"  warmed {total} candles across {len(cfg.strategy.universe)} assets")
    return 0 if total else 1


def cmd_quotex_assets(api) -> int:
    """Print every known Quotex asset with live payout/open flags.

    Requests a fresh instrument listing first; whatever the venue confirms
    within a few seconds is marked LIVE, the rest is the static floor.
    """
    import time as _time

    try:
        api.request_instruments()
    except Exception:  # noqa: BLE001 — static floor still prints
        pass
    cat = getattr(api, "catalog", None)
    before = float(getattr(cat, "synced_at", 0.0) or 0.0)
    deadline = _time.time() + 6.0
    while _time.time() < deadline:
        now_sync = float(getattr(cat, "synced_at", 0.0) or 0.0)
        if now_sync > before:
            break
        _time.sleep(0.25)
    rows = list(cat.to_list()) if cat is not None else []
    live = before > 0.0 or (cat is not None
                            and float(getattr(cat, "synced_at", 0.0) or 0.0) > 0.0)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get("kind") or "unknown"), []).append(row)
    print(f"  assets: {len(rows)} ({'LIVE venue listing' if live else 'static floor — venue listing not received'})")
    last_price = getattr(api, "last_price", None)
    for kind in sorted(groups):
        print(f"  [{kind}] ({len(groups[kind])})")
        for row in groups[kind]:
            px = None
            if callable(last_price):
                try:
                    px = last_price(row["name"])
                except Exception:  # noqa: BLE001
                    px = None
            flag = "OPEN" if row.get("open") else "SHUT"
            ident = f" id={row['id']}" if row.get("id") not in ("", row["name"]) else ""
            price = f" @ {px}" if px else ""
            print(f"    {row['name']:<14} {float(row.get('payout') or 0.0) * 100:>5.1f}%"
                  f" {flag:<4}{ident}{price}")
    return 0


def cmd_quotex_login(args: argparse.Namespace, cfg: AppConfig) -> int:
    """Chrome + your hands beat any headless login.

    Opens a persistent Chrome profile on qxbroker.com; you sign in and solve
    the CAPTCHA yourself; we detect the session (cookie or page token) over
    localhost DevTools and persist it (0600) for the websocket wire.
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
        # connect() only proves the transport; the balance push proves the
        # venue accepted the session. Wait for it instead of reading a
        # balance that is still 0.0 because the reply is in flight.
        api.wait_for_balance(timeout=8.0)
        stale = bool(getattr(api, "_session_stale", False))
        snap = api.account_snapshot()
        stats = api.socket.stats() if api.socket is not None else {}
        api.close()
        if stale:
            print("  ⚠ captured, but the venue rejected the session "
                  "(re-pair and solve the CAPTCHA again)")
            print("    (session kept — `cybertrade quotex status` will retry)")
            return 1
        frames = int(stats.get("messages_in", 0) or 0)
        if frames <= 0 and float(snap.balance or 0.0) <= 0.0:
            print("  ⚠ captured, but the venue stayed silent — run "
                  "`cybertrade quotex status` to retry")
            print("    (session kept)")
            return 1
        print(f"  ✓ verified — balance {float(snap.balance):.2f}: live session ready")
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


def _bounded_int(raw: Any, lo: int, hi: int, what: str) -> int:
    """Clamp a count into a sane range instead of trusting it.

    ``--runs 999999999`` used to try a billion paths and simply never
    return -- a terminal that hangs looks exactly like one that works.
    """
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} must be a whole number, not {raw!r}")
    if val < lo:
        raise ConfigError(f"{what} must be at least {lo}")
    if val > hi:
        raise ConfigError(f"{what} must be at most {hi} (got {val})")
    return val


def _checked_port(raw: Any, fallback: int) -> int:
    """A TCP port, or a clean ConfigError — never a bind() traceback.

    ``web --port 99999`` used to reach ``ThreadingHTTPServer`` and raise
    ``OverflowError: bind(): port must be 0-65535``, which on Windows is a
    traceback flash behind a console that closes before it can be read.
    """
    if raw is None or raw == "":
        return int(fallback)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"--port must be a whole number, not {raw!r}")
    if not 0 <= port <= 65535:
        raise ConfigError(f"--port must be 0-65535 (got {port})")
    return port


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

        w = _bounded_int(args.wins or 0, 0, 10_000_000, "--wins")
        l = _bounded_int(args.losses or 0, 0, 10_000_000, "--losses")
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
    try:
        pnls = [float(x) for x in args.pnl.split(",") if x.strip()]
    except ValueError as exc:
        print(f"  --pnl must be comma-separated numbers, not {args.pnl!r}: {exc}",
              file=sys.stderr)
        return 2
    if not pnls:
        print("  --pnl parsed to nothing — pass at least one number.",
              file=sys.stderr)
        return 2
    report = simulate(
        pnls,
        starting_balance=float(args.starting_balance),
        runs=_bounded_int(args.runs, 1, 100_000, "--runs"),
        horizon=_bounded_int(args.horizon, 1, 10_000, "--horizon"),
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
    from .strategies import STRATEGY_REGISTRY, configured_members, list_strategies

    print(BANNER)
    cfg = _load_config(args)
    # Show which of them are actually live. `strategy.disabled` used to be
    # accepted and ignored, so an operator had no way to check from the
    # terminal whether a strategy they turned off was still running.
    active = set(configured_members(cfg.strategy))
    by_family: dict = {}
    for name in list_strategies():
        cls = STRATEGY_REGISTRY[name]
        by_family.setdefault(cls.family, []).append(name)
    for family, names in sorted(by_family.items()):
        print(f"  [{family.upper()}]")
        for name in sorted(names):
            label = getattr(STRATEGY_REGISTRY[name], "label", name)
            mark = "on " if name in active else "OFF"
            print(f"    {mark} {name:<20} {label}")
    off = sorted(set(list_strategies()) - active)
    print(f"\n  active: {len(active)}/{len(list_strategies())} strategies"
          f" + 1 ensemble ({cfg.strategy.ensemble_mode})")
    if off:
        print(f"  disabled by config: {', '.join(off)}")
    else:
        print("  disabled by config: none")
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

    def _risk_switches():
        """Read the operator's real config, not a fresh default.

        "config validate" above proves the schema is sound; it says nothing
        about whether the operator has switched a risk control off. doctor is
        the preflight run before going live with real money, so a disabled
        playbook has to fail it rather than hide behind a passing checklist.
        """
        cfg = AppConfig.load(getattr(args, "config", None))
        off = []
        if not cfg.survivor.enabled:
            off.append("survivor playbook DISABLED")
        for label, active in (
            ("weekend lock", cfg.survivor.weekend_lock),
            ("panic deleverage", cfg.survivor.panic_deleverage),
            ("trend filter", cfg.survivor.trend_filter),
            ("regime rotation", cfg.survivor.regime_rotation),
        ):
            if not active:
                off.append(f"{label} off")
        if cfg.strategy.trade_on_weak:
            off.append("trade_on_weak ON")
        if cfg.strategy.max_signals_per_candle:
            off.append(f"vote cap {cfg.strategy.max_signals_per_candle}/candle")
        if cfg.risk.edge_gate == "off":
            off.append("edge gate OFF")
        if off:
            raise RuntimeError(
                "not at protective defaults — " + "; ".join(off)
                + " (each is a deliberate config choice, not a fault)"
            )
        return "all risk switches at their protective defaults"

    check("risk switches", _risk_switches)

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

        profile = default_chrome_profile()
        argv = chrome_argv("https://qxbroker.com", profile, 9333, "chrome")
        if "--remote-debugging-port=9333" not in argv:
            raise RuntimeError("devtools argv drifted")
        if f"--user-data-dir={profile}" not in argv:
            raise RuntimeError("profile argv drifted")
        return "chrome pairing available"

    check("chrome pairing", _pairing)

    def _gui():
        from .gui import GUI_AVAILABLE

        if GUI_AVAILABLE:
            return "tkinter available"
        if IS_WINDOWS:
            # tkinter ships with the Windows installer; a missing one means the
            # optional "tcl/tk" box was unticked, not that apt is the answer.
            return ("missing — rerun the Python installer with the "
                    "'tcl/tk and IDLE' option enabled")
        return "missing (apt install python3-tk)"

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

    qx = sub.add_parser("quotex", help="venue session: login / status / warm / assets")
    qx.add_argument("action", choices=["status", "warm", "login", "assets"])
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
    # Before anything prints: the banner is box-drawing art, and a redirected
    # Windows console encodes with the locale codepage, which has no code
    # points for it. Degrade the glyph, never the process.
    ensure_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        # A typed, expected failure. Commands that catch these themselves map
        # them to SAFETY_HOLD_EXIT; this is the same rule for the ones that
        # don't, so `gui` and `run` agree and no operator sees a traceback.
        sys.stdout.flush()
        print(f"  SAFETY HOLD — {exc}", file=sys.stderr)
        return SAFETY_HOLD_EXIT
    except KeyboardInterrupt:
        sys.stdout.flush()
        print("\n  interrupted.", file=sys.stderr)
        return 130
    except EOFError:
        sys.stdout.flush()
        print("  SAFETY HOLD — no input available to confirm with",
              file=sys.stderr)
        return SAFETY_HOLD_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
