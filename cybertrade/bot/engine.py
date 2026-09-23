"""The TradingEngine — orchestrates feed → regime → strategies → risk → venue.

Thread model: a single engine loop thread owns decision-making; feeds deliver
ticks through thread-safe books; the OMS settles expirations every cycle.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from ..config import AppConfig
from ..constants import EngineState, Side, Timeframe
from ..data.feed import Feed, QuoteBook, SyntheticFeed
from ..data.models import Signal, TradeRecord
from ..events import Topic, default_bus
from ..exceptions import KillSwitchEngaged, RiskRejection
from ..execution.oms import OrderManager
from ..execution.paper import PaperBroker
from ..regime.detector import RegimeDetector, RegimeReading
from ..risk.manager import RiskManager
from ..strategies.base import StrategyContext
from ..strategies.ensemble import AllWeatherEnsemble
from ..strategies.registry import build_all_weather
from ..utils import timex
from ..utils.mathx import clamp
from .health import HealthMonitor, HealthSnapshot
from .survivor import Posture, Survivor
from .watchdog import Watchdog
from .alerts import AlertCenter
from .calendar import EconomicCalendar, calendar_for_asset
from ..risk.correlation import CorrelationMonitor
from ..strategies.plugins import PluginRegistry
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
    ) -> None:
        self.config = config or AppConfig()
        self.config.validate()

        self.risk = RiskManager(self.config.risk)
        self.broker = broker or PaperBroker(
            starting_balance=self.config.risk.starting_balance,
            default_payout=self.config.broker.payout_default,
            latency_ms=self.config.broker.latency_ms,
            slippage_bps=self.config.broker.slippage_bps,
        )
        self.oms = OrderManager(self.broker, self.risk)
        self.feed = feed or SyntheticFeed(
            assets=self.config.strategy.universe,
            timeframe_seconds=self.config.timeframe().seconds,
            tick_interval=0.5,
        )
        self.ensemble = ensemble or build_all_weather(
            mode=self.config.strategy.ensemble_mode,
            adaptive=self.config.strategy.adaptive_weights,
            min_confidence=self.config.strategy.min_confidence,
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
        default_bus.subscribe(Topic.KILL, lambda e: self._enter_kill(str(e.payload)))

    # -- lifecycle ---------------------------------------------------------
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

    def boot(self) -> None:
        """Connect everything but keep trading disarmed."""
        self.state = EngineState.BOOT
        self._restore_calibration()
        if hasattr(self.feed, "warmup"):
            self.feed.warmup()
        self.broker.connect()
        # live venue wiring: stream quotes into the same pipeline as the feed
        # and pull the instrument catalog (both no-ops for paper).
        api = getattr(self.broker, "api", None)
        if api is not None:
            if hasattr(api, "add_tick_handler"):
                api.add_tick_handler(self._on_tick)
            if hasattr(api, "request_instruments"):
                try:
                    api.request_instruments()
                except Exception:  # noqa: BLE001
                    log.exception("instrument catalog request failed")
        self.risk.reset_day(self.config.risk.starting_balance)
        for asset in self.feed.assets:
            self.detectors[asset] = RegimeDetector()
        self.state = EngineState.DISARMED
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        self.alerts.attach()
        self.tape.attach()
        self.plugins.load_all()
        for strategy in self.plugins.strategies():
            self.ensemble.attach(strategy)
            self.health.note_message(f"plugin attached: {strategy.name}")
        self.health.note_message("engine booted (disarmed)")
        log.info("engine booted state=%s assets=%s", self.state.value, self.feed.assets)

    def arm(self, live: bool = False) -> None:
        """Permit trading.  ``live=True`` requires config.allow_live AND is
        gated behind an explicit second confirmation in the CLI/GUI."""
        if live and not self.config.risk.allow_live:
            raise KillSwitchEngaged(
                "live trading disabled in config (risk.allow_live=false) — "
                "paper mode protects you from yourself"
            )
        self.state = EngineState.LIVE if live else EngineState.ARMED
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        self._running = True
        self.feed.start()
        self.watchdog.start()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="engine")
        self._thread.start()
        self.health.note_message(f"engine ARMED mode={'LIVE' if live else 'PAPER'}")
        log.warning("engine ARMED mode=%s", "LIVE" if live else "PAPER")

    def disarm(self) -> None:
        self.state = EngineState.DISARMED
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        self.health.note_message("engine disarmed")

    def kill(self, reason: str) -> None:
        self._running = False
        self.state = EngineState.KILL
        self.risk.engage_kill(reason)
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        self.health.note_message(f"KILL: {reason}")
        log.critical("engine KILL: %s", reason)

    def shutdown(self) -> None:
        self._running = False
        self.feed.stop()
        self.watchdog.stop()
        if self._thread:
            self._thread.join(timeout=3.0)
        self.broker.disconnect()
        self.tape.detach()
        self._persist_calibration()
        self.state = EngineState.SHUTDOWN
        default_bus.publish(Topic.ENGINE_STATE, self.state.value, source="engine")
        log.info("engine shutdown complete")

    def clear_kill(self) -> None:
        self.risk.release_kill()
        self.survivor.clear_lockdown()
        self.state = EngineState.DISARMED
        self.health.note_message("kill switch cleared — disarmed")

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
                time.sleep(delay)
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

        self.watchdog.sweep(now)
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

        is_otc = signal.asset.endswith("_otc") or self.broker.name == "PAPER-SIM"
        liquidity = self.quotes.liquidity_score(signal.asset)
        spread_mult = 1.0
        avg = self.quotes.avg_spread(signal.asset)
        if avg > 0:
            spread_mult = self.quotes.spread(signal.asset) / avg

        family = "ensemble"
        decision = self.survivor.evaluate(
            signal,
            reading,
            liquidity=liquidity,
            spread_mult=spread_mult,
            risk_scale=self.risk.governor_scale(),
            is_otc=is_otc,
            now=now,
            strategy_family=family,
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
            regime_scale=decision.stake_scale,
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
            strategy=signal.strategy,
            tag=f"{decision.posture}:{signal.reason}",
            cluster=self.corr.cluster_of(signal.asset),
            regime=reading.regime.value,
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
            "survivor": self.survivor.describe(),
            "strategies": self.ensemble.describe(),
        }

    def inject_signal(self, signal: Signal) -> bool:
        """Manually push a signal through the full defense stack (GUI button)."""
        reading = self.regime_of.get(signal.asset, RegimeReading())
        return self._try_execute(signal, reading)


__all__ = ["TradingEngine"]
