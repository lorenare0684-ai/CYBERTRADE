"""Event-driven backtester for binary-option strategies.

Walks candle sequences through regime detection → strategies → survivor →
risk → simulated fills → settlement at expiry.  Fully deterministic given a
scenario seed.  Designed to answer one question per run: would this system
have survived this market?
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import AppConfig
from ..constants import MarketRegime, Side
from ..data.models import Candle, Signal, TradeRecord
from ..data.synthetic import generate_candles
from ..regime.detector import RegimeDetector, RegimeReading
from ..risk.manager import RiskManager
from ..indicators.orderflow import TickFlow
from ..quant.binary import edge_of
from ..quant.calibration import CalibrationTracker
from ..strategies.base import StrategyContext
from ..strategies.ensemble import AllWeatherEnsemble
from ..strategies.registry import build_all_weather
from ..utils.mathx import clamp, max_drawdown
from .report import BacktestReport, build_report

log = logging.getLogger("cybertrade.backtest")


@dataclass
class SimTrade:
    """In-flight binary contract during simulation."""

    asset: str
    side: Side
    stake: float
    strike: float
    payout: float
    entry_idx: int
    expiry_idx: int
    strategy: str
    regime: str
    confidence: float
    votes: tuple = ()

    def settles_at(self, idx: int) -> bool:
        return idx >= self.expiry_idx


@dataclass
class BacktestResult:
    scenario: str
    seed: int
    report: BacktestReport
    trades: List[TradeRecord] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    regime_path: List[str] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    edge_rejects: int = 0
    strategy_evidence: Dict[str, Any] = field(default_factory=dict)
    config_snapshot: Dict[str, Any] = field(default_factory=dict)

    @property
    def survived(self) -> bool:
        """No ruin + drawdown under the total cap = 'survived' this regime."""
        return self.report.final_balance > 0 and self.report.max_drawdown < 0.35

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "seed": self.seed,
            "survived": self.survived,
            "edge_rejects": self.edge_rejects,
            "strategy_evidence": {k: list(v) for k, v in self.strategy_evidence.items()},
            **self.report.to_dict(),
        }


class Backtester:
    """Deterministic simulation harness.

    Unlike the live engine, the backtester controls its own clock: expiry is
    resolved by index, fills occur at the next bar open with configured
    slippage, and risk counters reset daily every ``bars_per_day`` bars.
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        ensemble: Optional[AllWeatherEnsemble] = None,
    ) -> None:
        self.config = config or AppConfig()
        self.ensemble = ensemble
        self.survivor_enabled = self.config.survivor.enabled
        self.calibrator = CalibrationTracker()

    def run_scenario(
        self,
        scenario: str,
        bars: int = 800,
        seed: int = 1337,
        asset: str = "SIM",
        warmup: Optional[int] = None,
    ) -> BacktestResult:
        candles = generate_candles(scenario, bars=bars, seed=seed, asset=asset,
                                   params=_params_for(self.config))
        return self.run_candles(scenario, candles, seed=seed, asset=asset, warmup=warmup)

    def run_candles(
        self,
        scenario: str,
        candles: Sequence[Candle],
        seed: int = 1337,
        asset: str = "SIM",
        warmup: Optional[int] = None,
    ) -> BacktestResult:
        bt = self.config.backtest
        cfg = self.config
        warmup = warmup if warmup is not None else bt.warmup_bars
        ensemble = self.ensemble or build_all_weather(
            mode=cfg.strategy.ensemble_mode,
            adaptive=cfg.strategy.adaptive_weights,
            min_confidence=cfg.strategy.min_confidence,
        )
        risk = RiskManager(cfg.risk)
        risk.reset_day(
            bt.starting_balance,
            ts=(candles[0].close_ts if candles else None),
        )
        detector = RegimeDetector()
        flow = TickFlow()
        self.calibrator = CalibrationTracker()  # fresh evidence per run — cold start is honest
        edge_rejects = 0

        balance = bt.starting_balance
        peak = balance
        equity_curve: List[float] = []
        regime_path: List[str] = []
        trades: List[TradeRecord] = []
        open_trades: List[SimTrade] = []
        events: List[str] = []
        signals_total = 0
        vetoes = 0
        bars_per_day = max(1, 86400 // max(1, candles[0].timeframe_seconds)) if candles else 240

        for i in range(len(candles)):
            flow.on_tick(candles[i].close, size=candles[i].volume or 1.0)
            window = list(candles[max(0, i - 150) : i + 1])
            if len(window) < warmup:
                equity_curve.append(balance)
                regime_path.append("warmup")
                continue

            # ---- settle expirations at THIS bar's close --------------------
            still_open: List[SimTrade] = []
            for trade in open_trades:
                if trade.settles_at(i):
                    expiry_price = candles[i].close
                    won = (
                        expiry_price > trade.strike
                        if trade.side is Side.CALL
                        else expiry_price < trade.strike
                    )
                    refunded = abs(expiry_price - trade.strike) < 1e-12
                    pnl = 0.0 if refunded else (
                        trade.stake * trade.payout if won else -trade.stake
                    )
                    balance += trade.stake + pnl
                    if balance > peak:
                        peak = balance
                    from ..data.models import Settlement

                    settlement = Settlement(
                        fill_id=f"sim-{trade.entry_idx}",
                        order_id=f"sim-{trade.entry_idx}",
                        asset=trade.asset,
                        side=trade.side,
                        strike=trade.strike,
                        expiry_price=expiry_price,
                        stake=trade.stake,
                        payout=trade.payout,
                        won=won and not refunded,
                        refunded=refunded,
                        ts=candles[i].close_ts,
                    )
                    record = TradeRecord(
                        settlement=settlement,
                        strategy=trade.strategy,
                        regime=trade.regime,
                    )
                    trades.append(record)
                    risk.on_close_simple(
                        won and not refunded, pnl, balance, asset=asset,
                        now=candles[i].close_ts,
                    )
                    ensemble.record_result(won and not refunded, pnl)
                    self.calibrator.observe(
                        trade.strategy, trade.confidence, won and not refunded,
                        regime=trade.regime,
                    )
                    self.calibrator.observe_votes(
                        trade.votes, won and not refunded, regime=trade.regime
                    )
                else:
                    still_open.append(trade)
            open_trades = still_open

            # ---- daily governor reset --------------------------------------
            if i > 0 and i % bars_per_day == 0:
                risk.reset_day(balance, ts=candles[i].close_ts)

            # ---- decision ---------------------------------------------------
            reading = detector.assess(window)
            regime_path.append(reading.regime.value)

            if risk.state.kill:
                equity_curve.append(balance)
                events.append(f"bar {i}: kill switch ({risk.state.kill_reason})")
                continue

            ctx = StrategyContext(
                asset=asset,
                candles=window,
                regime=reading,
                timeframe_seconds=window[-1].timeframe_seconds,
                expiry_seconds=cfg.strategy.expiry_seconds,
                payout=bt.payout,
                ts=window[-1].close_ts,
                extra={"flow": flow},
            )
            signal = ensemble.generate(ctx)
            if signal is None:
                equity_curve.append(balance)
                continue

            signals_total += 1

            # ---- survivor ---------------------------------------------------
            if self.survivor_enabled:
                from ..bot.survivor import Survivor

                survivor = Survivor(
                    weekend_lock=cfg.survivor.weekend_lock,
                    friday_cutoff_utc=cfg.survivor.friday_cutoff_utc,
                )
                decision = survivor.evaluate(
                    signal,
                    reading,
                    liquidity=1.0,
                    spread_mult=1.0,
                    risk_scale=risk.governor_scale(),
                    is_otc=True,
                    now=signal.ts,
                    strategy_family="ensemble",
                )
                stake_scale = decision.stake_scale
                if not decision.allow:
                    vetoes += 1
                    equity_curve.append(balance)
                    continue
            else:
                stake_scale = 1.0

            # ---- risk + sizing ----------------------------------------------
            check = risk.check(
                asset=asset,
                side=signal.side,
                stake=cfg.risk.min_stake,
                payout=bt.payout,
                confidence=signal.confidence,
                strategy=signal.strategy,
                cluster="SIM",
                now=signal.ts,
            )
            if not check.all_ok:
                vetoes += 1
                equity_curve.append(balance)
                continue

            p_win = self.calibrator.p_win_for(
                signal.strategy, signal.confidence, signal.meta.get("votes"),
                regime=reading.regime.value if cfg.risk.regime_cal else "",
            )
            # Phase-12: size against the liar (mirror the live engine).
            p_size = (
                self.calibrator.p_win_lower(
                    signal.strategy, signal.confidence, signal.meta.get("votes"),
                    regime=reading.regime.value if cfg.risk.regime_cal else "",
                    quantile=cfg.risk.kelly_quantile,
                )
                if cfg.risk.kelly_quantile > 0 else p_win
            )
            sizing = risk.size_stake(
                balance=balance,
                payout=bt.payout,
                confidence=signal.confidence,
                win_rate=p_size,
                drawdown=(peak - balance) / peak if peak > 0 else 0.0,
                regime_scale=stake_scale,
            )
            stake = clamp(sizing.stake, 0.0, balance * 0.5)

            # ---- Phase-5: calibrated edge gate (mirrors the live engine) ----
            edge = edge_of(p_win, bt.payout)
            if edge < 0:  # negative EV never passes — even with the gate off
                edge_rejects += 1
                vetoes += 1
                equity_curve.append(balance)
                continue
            if cfg.risk.edge_gate != "off" and edge < cfg.risk.min_edge:
                if cfg.risk.edge_gate == "hard":
                    edge_rejects += 1
                    vetoes += 1
                    equity_curve.append(balance)
                    continue
                scale = clamp(edge / max(cfg.risk.min_edge, 1e-6), 0.25, 1.0)
                stake = max(cfg.risk.min_stake, stake * scale)

            if stake < cfg.risk.min_stake * 0.5 or len(open_trades) >= cfg.risk.max_concurrent:
                equity_curve.append(balance)
                continue

            # ---- Phase-5: adaptive expiry ----------------------------------
            expiry_seconds = signal.expiry_seconds
            if cfg.risk.expiry_select == "adaptive":
                from ..quant.expiry import choose_expiry

                expiry_seconds = choose_expiry(
                    "call" if signal.side is Side.CALL else "put",
                    candles[i].close,
                    bt.payout,
                    [c.close for c in window],
                    cfg.risk.expiry_candidates,
                    signal.confidence,
                    default=signal.expiry_seconds,
                )

            # fill at next bar's open + slippage (binary reality: you always
            # pay something to get filled)
            entry_idx = i
            fill_price = candles[min(i + 1, len(candles) - 1)].open
            slip = fill_price * bt.spread_bps / 10_000.0
            strike = fill_price + (slip if signal.side is Side.CALL else -slip)
            expiry_bars = max(1, int(expiry_seconds) // max(1, window[-1].timeframe_seconds))
            trade = SimTrade(
                asset=asset,
                side=signal.side,
                stake=stake,
                strike=strike,
                payout=bt.payout,
                entry_idx=entry_idx,
                expiry_idx=entry_idx + expiry_bars,
                strategy=signal.strategy,
                regime=reading.regime.value,
                confidence=signal.confidence,
                votes=tuple(signal.meta.get("votes") or ()),
            )
            balance -= stake
            open_trades.append(trade)
            risk.on_open_simple(stake, asset)
            equity_curve.append(balance)

        # ---- settle anything left at the end ------------------------------
        if open_trades:
            events.append(f"warning: {len(open_trades)} trade(s) open at end — force settled")
            last = candles[-1]
            for trade in open_trades:
                won = last.close > trade.strike if trade.side is Side.CALL else last.close < trade.strike
                pnl = trade.stake * trade.payout if won else -trade.stake
                balance += trade.stake + pnl

        report = build_report(
            trades=trades,
            equity_curve=equity_curve,
            starting_balance=bt.starting_balance,
            final_balance=balance,
            signals_total=signals_total,
            vetoes=vetoes,
        )
        # Phase-17: the gauntlet asks the P10 question of its own record.
        ev_w, ev_l = self.calibrator.evidence()
        report.evidence = [ev_w, ev_l]
        if ev_w + ev_l >= 5:
            from ..risk.montecarlo import simulate_posterior

            report.p_edge_negative = simulate_posterior(
                ev_w, ev_l, payout=bt.payout, runs=200, horizon=30,
                starting_balance=bt.starting_balance,
            ).p_edge_negative
        return BacktestResult(
            scenario=scenario,
            seed=seed,
            report=report,
            trades=trades,
            equity_curve=equity_curve,
            regime_path=regime_path,
            events=events,
            edge_rejects=edge_rejects,
            strategy_evidence=self.calibrator.strategy_evidence(),
        )

    def run_matrix(
        self,
        scenarios: Optional[Sequence[str]] = None,
        bars: int = 600,
        seeds: Sequence[int] = (1, 7, 42),
    ) -> List[BacktestResult]:
        """The gauntlet: every scenario × several seeds."""
        results: List[BacktestResult] = []
        for scenario in scenarios or self.config.backtest.scenarios:
            for seed in seeds:
                try:
                    results.append(self.run_scenario(scenario, bars=bars, seed=seed))
                except Exception as exc:  # noqa: BLE001
                    log.exception("scenario %s seed %s failed", scenario, seed)
                    events = [f"crash: {type(exc).__name__}: {exc}"]
                    start = self.config.backtest.starting_balance
                    empty = build_report([], [start], start, start, 0, 0)
                    results.append(
                        BacktestResult(scenario=scenario, seed=seed, report=empty, events=events)
                    )
        return results


def _params_for(config: AppConfig):
    from ..data.synthetic import MarketParams

    return MarketParams(
        timeframe_seconds=config.timeframe().seconds,
        base_vol=0.0006,
    )


__all__ = ["Backtester", "BacktestResult", "SimTrade"]
