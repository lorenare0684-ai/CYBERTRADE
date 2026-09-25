"""The TradingEngine — orchestrates venue feed → regime → strategies → risk → venue.

Thread model: a single engine loop thread owns decision-making; the live feed
delivers ticks through thread-safe books; the OMS settles expirations every
cycle.

**Live only.**  There is no paper broker, no dry-run broker and no synthetic
feed in this build: the engine is constructed with a venue broker and a venue
feed, ``arm()`` always arms live trading (gated by the CLI's I-UNDERSTAND
confirmation and the durable risk governor), and any non-venue feed is
refused at construction time.
"""

from __future__ import annotations

import logging
import os
import uuid
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from ..config import AppConfig
from ..constants import EngineState, Side, Timeframe
from ..data.feed import Feed, QuoteBook
from ..data.models import Signal, TradeRecord
from ..events import Topic, default_bus
from ..exceptions import ConfigError, KillSwitchEngaged, RiskRejection
from ..execution.oms import OrderManager
from ..regime.detector import RegimeDetector, RegimeReading
from ..risk.manager import RiskManager
from ..risk.sessions import session_for, session_report as sessions_snapshot
from ..risk.slippage import expected_slippage_bps
from ..statestore import (StateError, load_operator_state, write_heartbeat,
                          save_operator_state as persist_operator_state)
from ..strategies.base import StrategyContext
from ..strategies.ensemble import AllWeatherEnsemble
from ..strategies.registry import build_all_weather, configured_members
from ..utils import timex
from ..utils.mathx import clamp
from .health import HealthMonitor, HealthSnapshot
from .survivor import Posture, Survivor
from .watchdog import Watchdog
from .alerts import AlertCenter
from .calendar import EconomicCalendar, calendar_for_asset
from ..risk.correlation import CorrelationMonitor
from ..strategies.plugins import PluginRegistry
from ..journal import TradeJournal
from ..network.supervisor import ReconnectSupervisor
from ..quant.calibration import CalibrationTracker
from ..quant.binary import breakeven_winrate, edge_of
from ..indicators.orderflow import TickFlow
from ..data.tape import TapeRecorder

log = logging.getLogger("cybertrade.engine")


class TradingEngine:
    """Live trading loop with full defense stack."""

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        feed: Optional[Feed] = None,
        broker=None,
        ensemble: Optional[AllWeatherEnsemble] = None,
        *,
        durable: bool = False,
    ) -> None:
        self.config = config or AppConfig()
        self.config.validate()
        if self.config.broker.mode != "quotex":
            raise ConfigError(
                f"broker.mode={self.config.broker.mode!r} is not supported — this "
                "build trades LIVE at Quotex only"
            )
        if broker is None:
            raise ConfigError(
                "a venue broker is required — this build has no paper/dry-run "
                "fallback; connect a Quotex session (`cybertrade quotex login`)"
            )
        if feed is None:
            raise ConfigError(
                "a venue feed is required — this build has no synthetic market; "
                "wire LiveQuotexFeed from a live session"
            )

        self.risk = RiskManager(self.config.risk)
        self.broker = broker
        self.oms = OrderManager(self.broker, self.risk)
        self.feed = feed
        # Live trading consumes venue candles only — a non-venue feed here is
        # a wiring bug, not a fallback. Fail before boot.
        if getattr(self.feed, "is_synthetic", False):
            raise ConfigError(
                "live trading requires live venue candles — pair a browser "
                "session (`cybertrade quotex login`) and rebuild; replay and "
                "synthetic feeds are refused"
            )
        scfg = self.config.strategy
        # trade_on_weak drops the confidence floor into the WEAK band, which
        # is the only thing the flag can honestly mean: min_confidence is the
        # floor, and WEAK sits just below it.
        floor = scfg.min_confidence
        if scfg.trade_on_weak:
            # SignalStrength.WEAK is the band (0.30, min_confidence]; the
            # floor has to reach its lower edge for those to get through.
            floor = min(floor, 0.30)
        self.ensemble = ensemble or build_all_weather(
            member_names=configured_members(self.config.strategy),
            mode=scfg.ensemble_mode,
            adaptive=scfg.adaptive_weights,
            min_confidence=floor,
            max_votes=scfg.max_signals_per_candle,
        )
        scfg = self.config.survivor
        self.survivor = Survivor(
            enabled=scfg.enabled,
            weekend_lock=scfg.weekend_lock,
            friday_cutoff_utc=scfg.friday_cutoff_utc,
            news_blackout_minutes=scfg.news_blackout_minutes,
            spread_limit_mult=scfg.spread_limit_mult,
            liquidity_floor=scfg.liquidity_floor,
            panic_deleverage=scfg.panic_deleverage,
            max_slippage_bps=scfg.max_slippage_bps,
            regime_rotation=scfg.regime_rotation,
            trend_filter=scfg.trend_filter,
        )
        self.watchdog = Watchdog(
            max_stale_seconds=self.config.timeframe().seconds * 3 + 10,
            kill_callback=self.kill,
        )
        self.health = HealthMonitor()

        self.quotes = QuoteBook()
        self.regime_of: Dict[str, RegimeReading] = {}
        self.detectors: Dict[str, RegimeDetector] = {}
        self.signals: List[Signal] = []
        self.state = EngineState.BOOT
        self.signals_total = 0
        self.vetoes = 0
        self.last_error = ""
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._stop_event = threading.Event()
        self._cycles = 0
        self._heartbeat_path = (os.path.abspath(os.path.expanduser(self.config.heartbeat_path))
                                if durable and self.config.heartbeat_path else "")
        self._heartbeat_run_id = os.environ.get("CYBERTRADE_RUN_ID") or uuid.uuid4().hex
        self._lock = threading.RLock()
        self._ticks_this_cycle: Dict[str, int] = {a: 0 for a in self.feed.assets}
        self._last_candle_ts: Dict[str, float] = {}

        # -- Phase-2 intelligence layer -------------------------------------
        self.corr = CorrelationMonitor()
        self.alerts = AlertCenter(self.config.alerts)
        self.plugins = PluginRegistry(self.config.plugins_dir)
        # -- Phase-4 edge layer ---------------------------------------------
        self.calibrator = CalibrationTracker()
        self.flow: Dict[str, TickFlow] = {a: TickFlow() for a in self.feed.assets}
        self.tape = TapeRecorder(self.config.tape_dir, enabled=self.config.tape_enabled)
        try:
            self.journal: Optional[TradeJournal] = TradeJournal(self.config.journal_path)
        except Exception:  # noqa: BLE001 — a locked journal must not stop boot
            log.exception("journal open failed — trade history will be in-memory only")
            self.journal = None
        self._journal_session = 0
        self.supervisor: Optional[ReconnectSupervisor] = None
        self._decay_alerted: set = set()
        self.edge_rejects = 0
        try:
            self.calendar = EconomicCalendar.load(
                self.config.calendar_path,
                blackout_seconds=max(
                    60, int(self.config.survivor.news_blackout_minutes * 60)
                ),
            )
        except Exception:  # noqa: BLE001 — a bad calendar file must not stop boot
            log.exception("calendar load failed — continuing with empty calendar")
            self.calendar = EconomicCalendar()

        self.feed.add_listener(self._on_tick)
        self.oms.add_settle_listener(self._on_settle)
        self._kill_subscription = default_bus.subscribe(
            Topic.KILL, lambda e: self._enter_kill(str(e.payload)) if self.risk.state.kill else None
        )
        self.continuity = None
        if durable and self.config.continuity_path:
            from ..continuity import Continuity
            self.continuity = Continuity(self, self.config.continuity_path)

    # -- lifecycle ---------------------------------------------------------
    def _on_connection_event(self, kind: str, payload: dict) -> None:
        self.health.note_message(f"venue {kind}: {payload}")
        default_bus.publish(Topic.CONNECTION, {"event": kind, **payload},
                            source="network")

    def _check_decay(self) -> None:
        """Stale-edge detector — fire once per strategy per decay spell."""
        if self.journal is None:
            return
        try:
            from ..journal import strategy_decay

            for row in strategy_decay(self.journal):
                name = row["strategy"]
                if row["decaying"] and name not in self._decay_alerted:
                    self._decay_alerted.add(name)
                    self.health.note_message(
                        f"decay alert {name}: wr {row['prior_win_rate']:.0%} → "
                        f"{row['recent_win_rate']:.0%} ({row['delta']:+.2f})"
                    )
                    default_bus.publish(Topic.ALERT, {"kind": "decay", **row},
                                        source="decay-watch")
                    self.health.note_message(
                        f"QUARANTINED {name} — decay isolation engaged"
                    )
                    default_bus.publish(
                        Topic.ALERT,
                        {"kind": "quarantine", "strategy": name},
                        source="quarantine-ward",
                    )
                elif not row["decaying"] and name in self._decay_alerted:
                    self._decay_alerted.discard(name)  # recovered — arm again
                    default_bus.publish(
                        Topic.ALERT,
                        {"kind": "release", "strategy": name},
                        source="quarantine-ward",
                    )
                    self.health.note_message(
                        f"RELEASED {name} — decay cleared"
                    )
            # Phase-25: keep the ensemble's voter skip in lockstep with the ward.
            if hasattr(self.ensemble, "quarantined_votes"):
                self.ensemble.quarantined_votes = set(self._decay_alerted)
        except Exception:  # noqa: BLE001
            log.exception("decay check failed")

    def quarantine_report(self) -> List[str]:
        """Phase-25: strategies currently isolated for decay (sorted names)."""
        return sorted(self._decay_alerted)

    def _salvage_all(self, reason: str) -> None:
        """The lifeboat: liquidate every open contract at the salvage mark."""
        closed = 0
        for pos in list(self.broker.open_positions()):
            try:
                if self.oms.close_position(pos.id):
                    closed += 1
            except Exception:  # noqa: BLE001
                log.exception("salvage failed for %s", pos.id)
        if closed:
            self.health.note_message(
                f"LIFEBOAT: salvaged {closed} position(s) — {reason}"
            )
            default_bus.publish(
                Topic.ALERT,
                {"kind": "salvage", "count": closed, "reason": reason},
                source="lifeboat",
            )

    def session_report(self) -> Dict[str, Any]:
        """Phase-22: wall-clock session + per-asset stake scales (HUD)."""
        return sessions_snapshot(self.feed.assets, timex.now())

    def _restore_calibration(self) -> None:
        """Reload the honesty ledger so learning survives process death."""
        path = self.config.calibration_path
        if not path:
            return
        try:
            if self.calibrator.load(path):
                w, l = self.calibrator.evidence()
                self.health.note_message(f"calibrator restored: {w}W/{l}L")
                log.info("calibrator restored %dW/%dL (%d obs) from %s",
                         w, l, self.calibrator.observations, path)
        except Exception:  # noqa: BLE001 — a bad ledger must not stop boot
            log.exception("calibration ledger load failed — starting cold")

    def _persist_calibration(self) -> None:
        path = self.config.calibration_path
        if not path:
            return
        try:
            if self.calibrator.save(path):
                log.info("calibration ledger saved: %s (%d obs)",
                         path, self.calibrator.observations)
        except Exception:  # noqa: BLE001
            log.exception("calibration ledger save failed")

    def _restore_operator_state(self) -> None:
        """Phase-28: operator decisions (lockdown, deck) survive restart."""
        path = getattr(self.config, "operator_path", "") or ""
        data = load_operator_state(path)
        if not data:
            return
        if data.get("manual_lockdown"):
            reason = str(data.get("lockdown_reason") or "restored from disk")
            self.survivor.engage_lockdown(reason)
            self.health.note_message(
                f"operator state restored: LOCKDOWN ({reason})"
            )
            default_bus.publish(
                Topic.ALERT,
                {"kind": "lockdown_restore", "reason": reason},
                source="operator-state",
            )
            log.info("operator state: LOCKDOWN restored (%s)", reason)
        disabled = {str(n) for n in (data.get("disabled_strategies") or [])}
        known = 0
        for m in self.ensemble.members:
            if m.name in disabled:
                m.enabled = False
                known += 1
        if known:
            self.health.note_message(
                f"operator state restored: {known} strategy(ies) stay disabled"
            )
            log.info("operator state: %d strategy(ies) disabled", known)

    def save_operator_state(self) -> bool:
        """Phase-28: persist lockdown + deck toggles (never raises)."""
        path = getattr(self.config, "operator_path", "") or ""
        if not path:
            return False
        return persist_operator_state(path, {
            "manual_lockdown": bool(self.survivor._manual_lockdown),
            "lockdown_reason": self.survivor.lockdown_reason,
            "disabled_strategies": sorted(
                m.name for m in self.ensemble.members if not m.enabled
            ),
        })

    def boot(self) -> None:
        """Connect disarmed; runtime checkpoints never restore an ARMED flag."""
        try:
            if self.continuity is not None:
                self.continuity.acquire()  # reject a second writer before connecting
            self._boot()
        except BaseException:
            if self.continuity is not None:
                self.continuity.release()
            self._kill_subscription.unsubscribe()
            self.feed.stop()
            self.broker.disconnect()
            raise

    def _boot(self) -> None:
        self.state = EngineState.BOOT
        self._restore_calibration()
        self._restore_operator_state()
        if hasattr(self.feed, "warmup"):
            self.feed.warmup()
        self.broker.connect()
        # live venue wiring: stream quotes into the same pipeline as the feed
        # and pull the instrument catalog (no-ops for a broker with no api).
        api = getattr(self.broker, "api", None)
        # Phase-29: a live feed already forwards venue ticks through its own
        # listener — a second direct handler would double-count the stream.
        if api is not None and getattr(self.feed, "is_synthetic", True):
            if hasattr(api, "add_tick_handler"):
                api.add_tick_handler(self._on_tick)
            if hasattr(api, "request_instruments"):
                try:
                    api.request_instruments()
                except Exception:  # noqa: BLE001
                    log.exception("instrument catalog request failed")
            # Phase-14: pull real venue history into the books before trading —
            # every strategy needs warm indicators, whatever the venue.
            if hasattr(api, "get_candles") and callable(getattr(self.feed, "book", None)):
                try:
                    from ..brokers.quotex.sync import warm_book

                    total = 0
                    for asset in self.feed.assets:
                        book = self.feed.book(asset)
                        if book is not None:
                            total += warm_book(api, book, bars=120, wait=2.0)
                    if total:
                        log.info("venue warm: +%d candles into books", total)
                except Exception:  # noqa: BLE001 — a slow venue never blocks boot
                    log.exception("venue warm failed — books keep warmup candles")
        # Phase-16: bounded reconnect supervision for the venue wire — every
        # live engine heals its own broker wire, whatever feed it runs on.
        if api is not None:
            def _resubscribe(a):
                if hasattr(a, "request_instruments"):
                    a.request_instruments()

            self.supervisor = ReconnectSupervisor(
                api,
                max_attempts=self.config.broker.reconnect_max,
                resubscribe=_resubscribe,
                on_event=self._on_connection_event,
            )
        balance = self.config.risk.starting_balance
        if self.continuity is not None:
            balance = self.broker.account().balance
            self.oms.ledger.balance = self.oms.ledger.starting_balance = balance
            self.oms.ledger.peak = max(balance, self.broker.account().peak_balance)
            self.oms.ledger.equity_curve = [(timex.now(), balance)]
            self.risk.state.peak_balance = self.oms.ledger.peak
        self.risk.reset_day(balance)
        if self.continuity is not None:
            self.continuity.restore()
            self.oms._continuity = self.continuity
        if self.journal is not None:
            try:
                self._journal_session = self.journal.start_session(
                    self.config.broker.mode, self.oms.ledger.balance
                )
            except Exception:  # noqa: BLE001
                log.exception("journal session start failed")
        for asset in self.feed.assets:
            self.detectors[asset] = RegimeDetector()
        self.state = EngineState.KILL if self.risk.state.kill else EngineState.DISARMED
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        self.alerts.attach()
        self.tape.attach()
        self.plugins.load_all()
        for strategy in self.plugins.strategies():
            self.ensemble.attach(strategy)
            self.health.note_message(f"plugin attached: {strategy.name}")
        if self.continuity is not None and not self.continuity.fault:
            try:
                self.continuity.commit()
            except StateError:
                pass  # fail-closed state is visible in the HUD
        self._heartbeat()
        self.health.note_message(f"engine booted ({self.state.value})")
        log.info("engine booted state=%s assets=%s", self.state.value, self.feed.assets)

    def arm(self) -> None:
        """Arm LIVE trading — there is no paper arming in this build.

        The human gate lives one level up (the CLI's ``I UNDERSTAND``
        confirmation plus ``risk.allow_live``); everything below it is
        machinery: kill latch, recovery hold, and the durable governor.
        """
        with self._lock:  # concurrent ARM requests cannot create duplicate loops
            self._arm()

    def _arm(self) -> None:
        if self.risk.state.kill or self.state is EngineState.KILL:
            raise KillSwitchEngaged(self.risk.state.kill_reason or "kill is latched")
        if self.continuity is not None:
            self.continuity.assert_ready()
            if self.continuity.status()["blocked"]:
                raise KillSwitchEngaged(self.continuity.status()["reason"])
        if not self.config.risk.allow_live:
            raise KillSwitchEngaged(
                "live trading disabled in config (risk.allow_live=false)"
            )
        if self._running:
            self.state = EngineState.LIVE
            default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
            return  # repeat ARM does not spawn a second decision thread
        if self._thread and self._thread.is_alive():
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                raise KillSwitchEngaged("previous engine loop is still stopping")
        self.state = EngineState.LIVE
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        self._running = True
        self._stop_event.clear()
        self.feed.start()
        self.watchdog.start()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="engine")
        self._thread.start()
        self.health.note_message("engine ARMED mode=LIVE (real order flow)")
        log.warning("engine ARMED mode=LIVE — real order flow at the venue")

    def disarm(self) -> None:
        self.state = EngineState.DISARMED
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        self.health.note_message("engine disarmed")

    def kill(self, reason: str) -> None:
        self._running = False
        self._stop_event.set()
        self.state = EngineState.KILL
        if self.continuity is None or not self.continuity.fault:
            with self.oms.transaction("kill"):
                self.risk.engage_kill(reason)
        self._heartbeat()
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        self.health.note_message(f"KILL: {reason}")
        log.critical("engine KILL: %s", reason)

    def shutdown(self) -> None:
        self._running = False
        self._stop_event.set()
        self.feed.stop()
        self.watchdog.stop()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        stopped = not self._thread or not self._thread.is_alive()
        if self.continuity is not None and self.continuity.active:
            if not stopped:
                self.continuity._fail("engine thread did not stop; retaining state lease")
            elif not self.continuity.fault:
                try:
                    self.continuity.commit()
                except StateError:
                    pass
            if stopped:
                self.continuity.release()
        self._kill_subscription.unsubscribe()
        self.broker.disconnect()
        self.tape.detach()
        self.alerts.detach()
        self._persist_calibration()
        if self.journal is not None:
            try:
                self.journal.end_session(self._journal_session, self.oms.ledger.balance)
            except Exception:  # noqa: BLE001
                log.exception("journal session end failed")
        self.state = EngineState.SHUTDOWN
        self._heartbeat()
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        log.info("engine shutdown complete")

    def clear_kill(self) -> None:
        with self.oms.transaction("clear-kill"):
            self.risk.release_kill()
            self.survivor.clear_lockdown()
            self.save_operator_state()   # Phase-28: disk must match the unlock
            self.state = EngineState.DISARMED
        self.health.note_message("kill switch cleared — disarmed")

    def _heartbeat(self) -> None:
        if self._heartbeat_path:
            write_heartbeat(self._heartbeat_path, state=self.state.value,
                            cycle=self._cycles, run_id=self._heartbeat_run_id)

    # -- main loop ---------------------------------------------------------
    def _run_loop(self) -> None:
        tf_seconds = self.config.timeframe().seconds
        next_cycle = time.time()
        while self._running:
            try:
                self.cycle()
            except KillSwitchEngaged as exc:
                self.kill(str(exc))
            except Exception as exc:  # noqa: BLE001 - engine must survive bugs
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("engine cycle failed")
                self.health.note_message(f"cycle error: {self.last_error}")
            next_cycle += max(0.25, tf_seconds / 6.0)
            delay = next_cycle - time.time()
            if delay > 0:
                self._stop_event.wait(delay)  # shutdown wakes even an H1 engine
            else:
                next_cycle = time.time()

    def cycle(self, now: Optional[float] = None) -> Dict[str, Any]:
        """One decision cycle across the whole universe (also used by tests)."""
        now = now if now is not None else timex.now()
        if self.state is EngineState.KILL:
            raise KillSwitchEngaged(self.risk.state.kill_reason or "kill engaged")

        self.oms.pump(now)
        self.watchdog.beat("engine")
        summary = {"signals": 0, "orders": 0, "vetoes": 0, "regimes": {}}

        for asset in self.feed.assets:
            self.watchdog.mark_tick(asset)
            self._ticks_this_cycle[asset] = 0
            self._sync_quote(asset)
            reading = self._update_regime(asset)
            summary["regimes"][asset] = reading.regime.value

            if self.state not in (EngineState.ARMED, EngineState.LIVE):
                continue

            if not self._new_closed_candle(asset):
                continue

            signal = self._generate_signal(asset, reading)
            if signal is None:
                continue
            summary["signals"] += 1
            self.signals_total += 1
            self.signals.append(signal)
            if len(self.signals) > 500:
                del self.signals[:-500]
            default_bus.publish(Topic.SIGNAL, signal, source="engine")

            placed = self._try_execute(signal, reading)
            if placed:
                summary["orders"] += 1
            else:
                summary["vetoes"] += 1
                self.vetoes += 1

        # Phase-19: the lifeboat — salvage open risk before the storm takes it.
        try:
            reading = self.regime_of.get(self.feed.assets[0])
            if reading is None:  # first cycles — self-heal the seam
                reading = self._update_regime(self.feed.assets[0])
            posture = (self.survivor.posture_for(reading)
                       if reading is not None else Posture.NORMAL)
            if (self.config.risk.crisis_salvage and posture == "LOCKDOWN"
                    and self.broker.open_positions()):
                self._salvage_all("survivor LOCKDOWN")
        except Exception:  # noqa: BLE001
            log.exception("lifeboat check failed")
        if self.supervisor is not None:
            self.supervisor.sweep(now)
        self.watchdog.sweep(now)
        self._cycles += 1
        self._heartbeat()  # wall-clock, AFTER success, never the simulation clock
        return summary

    # -- stages ------------------------------------------------------------
    def _candles_for(self, asset: str, limit: int):
        book_factory = getattr(self.feed, "book", None)
        if callable(book_factory):
            try:
                multi = book_factory(asset)
                return multi.book(self.config.timeframe().seconds).candles(limit=limit)
            except Exception:  # noqa: BLE001 - fall back to history
                pass
        return self.feed.history(asset, limit, self.config.timeframe().seconds)

    def _sync_quote(self, asset: str) -> None:
        """Make sure the broker has a tradable quote even without live ticks."""
        price = self.feed.last_price(asset)
        if price is None:
            candles = self._candles_for(asset, 1)
            price = candles[-1].close if candles else None
        if price is None:
            return
        if self.broker.last_price(asset) is None:
            from ..data.models import Tick

            tick = Tick(asset=asset, price=price)
            self.quotes.on_tick(tick)
            if hasattr(self.broker, "on_tick"):
                self.broker.on_tick(tick)

    def _new_closed_candle(self, asset: str) -> bool:
        """Gate strategy evaluation to once per closed candle per asset."""
        candles = self._candles_for(asset, 2)
        if not candles:
            return False
        last_ts = candles[-1].open_ts
        if self._last_candle_ts.get(asset) == last_ts:
            return False
        self._last_candle_ts[asset] = last_ts
        return True

    def _update_regime(self, asset: str) -> RegimeReading:
        detector = self.detectors.setdefault(asset, RegimeDetector())
        candles = self._candles_for(asset, 150)
        reading = detector.assess(candles)
        self.regime_of[asset] = reading
        default_bus.publish(Topic.REGIME, {"asset": asset, **reading.to_dict()}, source="engine")
        return reading

    def _generate_signal(self, asset: str, reading: RegimeReading) -> Optional[Signal]:
        candles = self._candles_for(asset, self.ensemble.lookback)
        if len(candles) < self.ensemble.min_bars:
            return None
        # survivor.regime_rotation: let the posture table bias the blend toward
        # the strategy families that suit the tape. strategy_filter already
        # returns no weights when the flag is off, and a disabled playbook
        # biases nothing at all.
        setter = getattr(self.ensemble, "set_family_weights", None)
        if callable(setter):
            weights: Dict[str, Any] = {}
            if self.survivor.enabled:
                posture = self.survivor.posture_for(reading)
                weights = self.survivor.strategy_filter(posture).get("weights") or {}
            setter(weights)
        ctx = StrategyContext(
            asset=asset,
            candles=candles,
            regime=reading,
            timeframe_seconds=self.config.timeframe().seconds,
            expiry_seconds=self.config.strategy.expiry_seconds,
            payout=self.broker.payout_for(asset, self.config.strategy.expiry_seconds),
            ts=timex.now(),
            extra={"flow": self.flow.get(asset)},
        )
        return self.ensemble.generate(ctx)

    @staticmethod
    def _attributed_strategy(signal: Signal) -> str:
        """Phase-25: name the vote that actually earned the order.

        Ensemble signals arrive as 'ensemble_all_weather' — journal rows
        keyed by that blob blind the decay watch to WHICH edge is fading.
        Attribute to the strongest same-side voter; manual/no-vote signals
        keep their own name.
        """
        votes = signal.meta.get("votes") or []
        best_name, best_conf = "", -1.0
        for v in votes:
            if not isinstance(v, dict) or v.get("side") != signal.side:
                continue
            try:
                conf = float(v.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            name = str(v.get("strategy") or "")
            if name and conf > best_conf:
                best_name, best_conf = name, conf
        return best_name or signal.strategy

    def _try_execute(self, signal: Signal, reading: RegimeReading) -> bool:
        cfg = self.config
        now = signal.ts or timex.now()

        # Phase-2: economic-calendar blackout (news windows freeze entries)
        active = self.calendar.active_events(now, calendar_for_asset(signal.asset))
        if active:
            self.vetoes += 1
            event = active[0]
            self.health.note_message(
                f"calendar blackout {event.currency}: {event.title}"
            )
            default_bus.publish(
                Topic.NEWS,
                {
                    "blackout": True,
                    "title": event.title,
                    "currency": event.currency,
                    "ts": event.ts,
                    "asset": signal.asset,
                    "estimated": event.estimated,
                },
                source="calendar",
            )
            return False

        # Phase-25: the quarantine ward — decay flags now BLOCK trades.
        attributed = self._attributed_strategy(signal)
        if attributed in self._decay_alerted:
            self.vetoes += 1
            self.health.note_message(
                f"quarantine veto {attributed}: decaying edge isolated"
            )
            default_bus.publish(
                Topic.RISK_REJECT,
                {
                    "asset": signal.asset,
                    "strategy": attributed,
                    "reason": "strategy quarantined (decay)",
                },
                source="quarantine",
            )
            return False

        is_otc = signal.asset.endswith("_otc") or self.broker.name == "PAPER-SIM"
        liquidity = self.quotes.liquidity_score(signal.asset)
        spread_mult = 1.0
        avg = self.quotes.avg_spread(signal.asset)
        if avg > 0:
            spread_mult = self.quotes.spread(signal.asset) / avg

        family = "ensemble"
        # Phase-24: one slip estimate, two consumers — survivor veto + fill.
        slip_bps = expected_slippage_bps(
            stress=reading.stress,
            session_liquidity=session_for(signal.asset, signal.ts).liquidity,
            base=self.config.broker.slippage_bps,
        )
        decision = self.survivor.evaluate(
            signal,
            reading,
            liquidity=liquidity,
            spread_mult=spread_mult,
            risk_scale=self.risk.governor_scale(),
            is_otc=is_otc,
            now=now,
            strategy_family=family,
            expected_slippage_bps=slip_bps,
        )
        if not decision.allow:
            self.vetoes += 1
            self.health.note_message(
                f"survivor veto {signal.asset}: {decision.veto_reason}"
            )
            default_bus.publish(Topic.RISK, {"event": "survivor_veto",
                                             "reasons": decision.reasons}, source="survivor")
            return False

        balance = self.oms.ledger.balance
        # Phase-8: the venue quotes the hurdle — never the config default.
        payout = self.broker.payout_for(signal.asset, signal.expiry_seconds)

        # Phase-4/6: calibrated P(win) — per-voter evidence, not marketing copy.
        p_win = self.calibrator.p_win_for(
            signal.strategy, signal.confidence, signal.meta.get("votes"),
            regime=reading.regime.value if cfg.risk.regime_cal else "",
        )
        # Phase-12: the gate judges expected value (mean); the SIZE is what
        # the record deserves if it is lying (pessimistic Beta quantile).
        p_size = (
            self.calibrator.p_win_lower(
                signal.strategy, signal.confidence, signal.meta.get("votes"),
                regime=reading.regime.value if cfg.risk.regime_cal else "",
                quantile=cfg.risk.kelly_quantile,
            )
            if cfg.risk.kelly_quantile > 0 else p_win
        )
        edge = edge_of(p_win, payout)
        sizing = self.risk.size_stake(
            balance=balance,
            payout=payout,
            confidence=signal.confidence,
            win_rate=p_size,
            drawdown=self.risk.total_drawdown(),
            # Phase-22: thin tape takes a smaller share — session × survivor.
            regime_scale=(
                decision.stake_scale * session_for(signal.asset, signal.ts).scale
                # risk.crisis_stake_scale: shrink in a stressed tape as well
                # as under a survivor posture. The field existed and reached
                # nothing, so an operator who set it got full-size stakes in
                # exactly the conditions it was written for.
                * (cfg.risk.crisis_stake_scale if reading.is_defensive else 1.0)
            ),
        )
        stake = sizing.stake

        # Phase-4: calibrated edge gate — never pay a structural tax.
        gate_mode = self.config.risk.edge_gate
        if edge < 0:  # negative EV is ALWAYS wrong — even with the gate off
            self.edge_rejects += 1
            self.health.note_message(
                f"edge veto {signal.strategy}: p={p_win:.2f} edge={edge:+.3f}"
            )
            default_bus.publish(Topic.RISK_REJECT, {
                "asset": signal.asset, "strategy": signal.strategy,
                "p_win": round(p_win, 3), "edge": round(edge, 4),
                "reason": "negative EV at quoted payout",
            }, source="edge")
            return False
        if gate_mode != "off" and edge < self.config.risk.min_edge:
            if gate_mode == "hard":
                self.edge_rejects += 1
                self.health.note_message(
                    f"edge veto (hard) {signal.strategy}: edge={edge:+.3f}"
                )
                default_bus.publish(Topic.RISK_REJECT, {
                    "asset": signal.asset, "strategy": signal.strategy,
                    "p_win": round(p_win, 3), "edge": round(edge, 4),
                    "reason": "edge below min_edge",
                }, source="edge")
                return False
            scale = clamp(edge / max(self.config.risk.min_edge, 1e-6), 0.25, 1.0)
            stake = max(self.config.risk.min_stake, stake * scale)

        expiry = signal.expiry_seconds
        if cfg.risk.expiry_select == "adaptive":
            hist = self._candles_for(signal.asset, 80)
            if len(hist) >= 3:
                from ..quant.expiry import choose_expiry

                expiry = choose_expiry(
                    "call" if signal.side is Side.CALL else "put",
                    hist[-1].close,
                    payout,
                    [c.close for c in hist],
                    cfg.risk.expiry_candidates,
                    signal.confidence,
                    default=signal.expiry_seconds,
                )

        order = self.oms.submit(
            asset=signal.asset,
            side=signal.side,
            stake=stake,
            expiry_seconds=min(int(expiry), decision.max_expiry_seconds),
            payout=payout,
            confidence=signal.confidence,
            strategy=attributed,
            tag=f"{decision.posture}:{signal.reason}",
            cluster=self.corr.cluster_of(signal.asset),
            regime=reading.regime.value,
            slippage_bps=decision.slippage_bps,
            votes=tuple(signal.meta.get("votes") or ()),
        )
        return order is not None

    # -- callbacks ---------------------------------------------------------
    def _on_tick(self, tick) -> None:
        self.quotes.on_tick(tick)
        self.corr.on_price(tick.asset, tick.price)
        flow = self.flow.get(tick.asset)
        if flow is not None:
            flow.on_tick(tick.price)
        if hasattr(self.broker, "on_tick"):
            self.broker.on_tick(tick)
        self.health.note_tick()
        self._ticks_this_cycle[tick.asset] = self._ticks_this_cycle.get(tick.asset, 0) + 1

    def _on_settle(self, record: TradeRecord) -> None:
        won = record.won
        self.ensemble.record_result(won, record.pnl)
        meta_votes = []
        order = self.oms.orders.get(record.settlement.order_id)
        if order and "votes" in order.meta:
            meta_votes = order.meta["votes"]
        # Phase-4: the ledger teaches the calibrator what confidence was worth
        try:
            claimed = float((order.meta.get("confidence", 0.6)) if order else 0.6)
        except (TypeError, ValueError):
            claimed = 0.6
        self.calibrator.observe(
            record.strategy or (order.strategy if order else "manual"), claimed, won,
            regime=record.regime,
        )
        self.calibrator.observe_votes(
            order.meta.get("votes") if order else None, won, regime=record.regime
        )
        # Phase-13: the record survives — feed the durable journal.
        if self.journal is not None:
            try:
                self.journal.record_trade(record)
            except Exception:  # noqa: BLE001 — a journal hiccup must not break the trade path
                log.exception("journal write failed")
        self._check_decay()
        for vote in meta_votes:
            name = vote.get("strategy")
            if name:
                self.ensemble.reinforce_vote(name, won)
        self.health.note_message(
            f"settle {record.settlement.asset} {'WON' if won else 'LOST'} "
            f"{record.pnl:+.2f}"
        )

    def _enter_kill(self, reason: str) -> None:
        if self.state is not EngineState.KILL:
            self._stop_event.set()
            self._running = False
            self.state = EngineState.KILL

    # -- introspection -----------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        account = self.oms.account()
        posture = Posture.NORMAL
        first_asset = self.feed.assets[0] if self.feed.assets else ""
        if first_asset in self.regime_of:
            posture = self.survivor.posture_for(self.regime_of[first_asset])
        health: HealthSnapshot = self.health.snapshot(
            engine_state=self.state.value,
            posture=posture,
            regime=self.regime_of.get(first_asset, RegimeReading()).regime.value,
            feed_ok=self.feed.running,
            broker_ok=self.broker.connected,
            signals_total=self.signals_total,
            trades_total=len(self.oms.trades),
            wins=sum(1 for t in self.oms.trades if t.won),
            losses=sum(1 for t in self.oms.trades if not t.won),
            win_rate=self.oms.ledger.win_rate(),
            balance=account.balance,
            drawdown=self.risk.total_drawdown(),
            daily_loss_frac=self.risk.daily_loss_frac(),
            open_positions=account.open_positions,
            vetoes=self.vetoes,
        )
        return {
            "health": health.to_dict(),
            "account": account.to_dict(),
            "risk": self.risk.snapshot(account.balance),
            "regimes": {a: r.to_dict() for a, r in self.regime_of.items()},
            "watchdog": self.watchdog.status(),
            "continuity": (self.continuity.status() if self.continuity is not None
                           else {"enabled": False, "blocked": False}),
            "survivor": self.survivor.describe(),
            "strategies": self.ensemble.describe(),
        }

    def inject_signal(self, signal: Signal) -> bool:
        """Manually push a signal through the full defense stack (GUI button)."""
        reading = self.regime_of.get(signal.asset, RegimeReading())
        return self._try_execute(signal, reading)


__all__ = ["TradingEngine"]
