"""RiskManager — the law of the terminal.

Every order passes :meth:`authorize` before it may reach a broker.  The
manager enforces stake bands, exposure caps, trade-rate limits, drawdown
governors, loss-streak cooldowns, correlated-cluster caps, payout floors, and
circuit breakers.  Rejections are normal operation, not errors.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import RiskConfig
from ..constants import Side
from ..data.models import AccountSnapshot, Order, TradeRecord
from ..events import Topic, default_bus
from ..exceptions import RiskRejection
from ..utils import timex
from ..utils.mathx import clamp
from .limits import ExposureCaps, LimitBook, LimitCheck
from .sizing import (
    SizingDecision,
    confidence_scaled,
    drawdown_scaled,
    fixed_fraction,
    kelly_scaled,
    stake_round,
    vol_scaled,
)

log = logging.getLogger("cybertrade.risk")


@dataclass
class RiskState:
    """Mutable runtime counters the governor reads."""

    day_start_balance: float = 0.0
    current_balance: float = 0.0
    day_start_ts: float = 0.0
    peak_balance: float = 0.0
    trades_today: int = 0
    trades_this_hour: int = 0
    hour_start_ts: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    cooldown_until: float = 0.0
    locked_until: float = 0.0
    kill: bool = False
    kill_reason: str = ""
    open_assets: Dict[str, int] = field(default_factory=dict)
    open_clusters: Dict[str, int] = field(default_factory=dict)
    open_stake_sum: float = 0.0
    open_count: int = 0
    last_results: List[bool] = field(default_factory=list)
    realized_vol: float = 0.0006

    def to_dict(self) -> Dict[str, Any]:
        return {
            "day_start_balance": round(self.day_start_balance, 2),
            "peak_balance": round(self.peak_balance, 2),
            "trades_today": self.trades_today,
            "trades_this_hour": self.trades_this_hour,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "cooldown_until": self.cooldown_until,
            "locked_until": self.locked_until,
            "kill": self.kill,
            "kill_reason": self.kill_reason,
            "open_count": self.open_count,
            "open_stake_sum": round(self.open_stake_sum, 2),
        }


class RiskManager:
    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self.config = config or RiskConfig()
        self.config.validate()
        self.state = RiskState(
            day_start_balance=self.config.starting_balance,
            current_balance=self.config.starting_balance,
        )
        self.state.peak_balance = self.config.starting_balance
        self.caps = ExposureCaps(
            max_concurrent=self.config.max_concurrent,
            max_per_asset=1,
            max_currency_cluster=self.config.max_correlated_exposure,
            max_stake_sum=self.config.max_stake * self.config.max_concurrent * 1.5,
        )
        self._lock = threading.RLock()
        self._strategy_wr: Dict[str, float] = {}
        self.rejections: List[Dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------
    def reset_day(self, balance: float, ts: Optional[float] = None) -> None:
        with self._lock:
            ts = timex.now() if ts is None else ts
            self.state.day_start_balance = balance
            self.state.current_balance = balance
            self.state.day_start_ts = ts
            self.state.trades_today = 0
            self.state.trades_this_hour = 0
            self.state.hour_start_ts = ts
            self.state.locked_until = 0.0
        log.info("risk day reset balance=%.2f", balance)

    def update_balance(self, balance: float) -> None:
        with self._lock:
            self.state.current_balance = balance
            if balance > self.state.peak_balance:
                self.state.peak_balance = balance

    def set_realized_vol(self, vol: float) -> None:
        with self._lock:
            self.state.realized_vol = max(1e-9, vol)

    def engage_kill(self, reason: str) -> None:
        with self._lock:
            self.state.kill = True
            self.state.kill_reason = reason
        default_bus.publish(Topic.KILL, {"reason": reason}, source="risk")
        log.critical("KILL SWITCH: %s", reason)

    def release_kill(self) -> None:
        with self._lock:
            self.state.kill = False
            self.state.kill_reason = ""

    # -- the gate ----------------------------------------------------------
    def authorize(
        self,
        asset: str,
        side: Side,
        stake: float,
        payout: float,
        confidence: float,
        strategy: str = "",
        cluster: str = "",
        now: Optional[float] = None,
    ) -> LimitBook:
        """Evaluate every limit for a prospective trade. Raises on failure."""
        book = self.check(
            asset=asset,
            side=side,
            stake=stake,
            payout=payout,
            confidence=confidence,
            strategy=strategy,
            cluster=cluster,
            now=now,
        )
        if not book.all_ok:
            failure = book.first_failure()
            assert failure is not None
            self._record_rejection(failure, asset, strategy)
            default_bus.publish(
                Topic.RISK_REJECT,
                {"asset": asset, "check": failure.to_dict(), "strategy": strategy},
                source="risk",
            )
            raise RiskRejection(failure.message or failure.name, code=failure.name)
        return book

    def check(
        self,
        *,
        asset: str,
        side: Side,
        stake: float,
        payout: float,
        confidence: float,
        strategy: str = "",
        cluster: str = "",
        now: Optional[float] = None,
    ) -> LimitBook:
        book = LimitBook()
        cfg = self.config
        st = self.state
        now = now if now is not None else timex.now()

        with self._lock:
            self._roll_clock(now)

            book.add(
                LimitCheck(
                    "kill_switch",
                    not st.kill,
                    float(st.kill),
                    0.0,
                    f"kill switch engaged: {st.kill_reason}" if st.kill else "",
                )
            )
            book.add(
                LimitCheck(
                    "cooldown",
                    now >= st.cooldown_until,
                    st.cooldown_until - now,
                    0.0,
                    "loss-streak cooldown active" if now < st.cooldown_until else "",
                )
            )
            book.add(
                LimitCheck(
                    "drawdown_lock",
                    now >= st.locked_until,
                    st.locked_until - now,
                    0.0,
                    "daily loss governor locked trading" if now < st.locked_until else "",
                )
            )
            balance = max(
                0.0,
                st.current_balance or st.day_start_balance or cfg.starting_balance,
            )
            daily_loss_frac = self._daily_loss_frac()
            book.add(
                LimitCheck(
                    "daily_loss",
                    daily_loss_frac < cfg.max_daily_loss_frac,
                    daily_loss_frac,
                    cfg.max_daily_loss_frac,
                    "daily loss limit breached",
                )
            )
            total_dd = self._total_drawdown()
            book.add(
                LimitCheck(
                    "total_drawdown",
                    total_dd < cfg.max_total_drawdown_frac,
                    total_dd,
                    cfg.max_total_drawdown_frac,
                    "total drawdown limit breached",
                )
            )
            book.add(
                LimitCheck(
                    "stake_band",
                    cfg.min_stake * 0.5 <= stake <= cfg.max_stake,
                    stake,
                    cfg.max_stake,
                    "stake outside permitted band",
                )
            )
            book.add(
                LimitCheck(
                    "balance_covers",
                    stake <= balance * 0.5 + 1e-9,
                    stake,
                    balance * 0.5,
                    "stake exceeds half the daily balance",
                )
            )
            book.add(
                LimitCheck(
                    "payout_floor",
                    payout >= cfg.min_payout,
                    payout,
                    cfg.min_payout,
                    "payout below floor",
                )
            )
            book.add(
                LimitCheck(
                    "confidence",
                    confidence >= 0.3,
                    confidence,
                    0.3,
                    "signal confidence too low",
                )
            )
            book.add(
                LimitCheck(
                    "open_count",
                    st.open_count < cfg.max_concurrent,
                    float(st.open_count),
                    float(cfg.max_concurrent),
                    "max concurrent positions reached",
                )
            )
            per_asset = st.open_assets.get(asset, 0)
            book.add(
                LimitCheck(
                    "per_asset",
                    per_asset < self.caps.max_per_asset,
                    float(per_asset),
                    float(self.caps.max_per_asset),
                    "asset already has an open position",
                )
            )
            if cluster:
                per_cluster = st.open_clusters.get(cluster, 0)
                book.add(
                    LimitCheck(
                        "cluster",
                        per_cluster < self.caps.max_currency_cluster,
                        float(per_cluster),
                        float(self.caps.max_currency_cluster),
                        "correlated cluster at capacity",
                    )
                )
            book.add(
                LimitCheck(
                    "stake_sum",
                    st.open_stake_sum + stake <= self.caps.max_stake_sum,
                    st.open_stake_sum + stake,
                    self.caps.max_stake_sum,
                    "aggregate stake cap reached",
                )
            )
            book.add(
                LimitCheck(
                    "hour_rate",
                    st.trades_this_hour < cfg.max_trades_per_hour,
                    float(st.trades_this_hour),
                    float(cfg.max_trades_per_hour),
                    "hourly trade rate limit",
                )
            )
            book.add(
                LimitCheck(
                    "day_rate",
                    st.trades_today < cfg.max_trades_per_day,
                    float(st.trades_today),
                    float(cfg.max_trades_per_day),
                    "daily trade rate limit",
                )
            )
            wr = self.strategy_win_rate(strategy)
            if strategy and self._strategy_wr.get(strategy, {}).get("n", 0) >= 10:
                book.add(
                    LimitCheck(
                        "strategy_floor",
                        wr >= cfg.win_rate_floor,
                        wr,
                        cfg.win_rate_floor,
                        f"strategy {strategy} below win-rate floor",
                    )
                )
        return book

    # -- sizing ------------------------------------------------------------
    def size_stake(
        self,
        balance: float,
        payout: float,
        confidence: float,
        win_rate: float,
        drawdown: float,
        regime_scale: float = 1.0,
    ) -> SizingDecision:
        cfg = self.config
        with self._lock:
            if cfg.vol_target_enabled:
                decision = vol_scaled(
                    balance,
                    cfg.stake_fraction,
                    self.state.realized_vol,
                    cfg.vol_target_annual / (252.0 ** 0.5) / 100.0 or 0.0006,
                    cfg.min_stake,
                    cfg.max_stake,
                )
            elif win_rate > 0.52:
                decision = kelly_scaled(
                    balance,
                    win_rate,
                    payout,
                    cfg.kelly_fraction,
                    cfg.min_stake,
                    cfg.max_stake,
                )
            else:
                decision = fixed_fraction(balance, cfg.stake_fraction, cfg.min_stake, cfg.max_stake)

            stake = decision.stake
            stake = confidence_scaled(stake, confidence)
            stake = drawdown_scaled(stake, drawdown, cfg.max_total_drawdown_frac)
            if regime_scale < 1.0:
                stake *= regime_scale
            if cfg.martingale_enabled and self.state.consecutive_losses > 0:
                mult = min(cfg.martingale_cap, 1.5 ** min(self.state.consecutive_losses, 4))
                stake *= mult
                decision.notes += f" martingale x{mult:.2f}"
            stake = stake_round(max(cfg.min_stake * 0.5, stake), 1.0)
            decision.stake = clamp(stake, cfg.min_stake * 0.5, cfg.max_stake)
            return decision

    # -- bookkeeping -------------------------------------------------------
    def on_open(self, order: Order) -> None:
        with self._lock:
            st = self.state
            st.open_count += 1
            st.open_stake_sum += order.amount
            st.open_assets[order.asset] = st.open_assets.get(order.asset, 0) + 1
            cluster = order.meta.get("cluster") or _cluster_of(order.asset)
            st.open_clusters[cluster] = st.open_clusters.get(cluster, 0) + 1
            st.trades_today += 1
            st.trades_this_hour += 1

    def on_open_simple(self, stake: float, asset: str = "") -> None:
        """Backtest-friendly opener without a full Order object."""
        with self._lock:
            st = self.state
            st.open_count += 1
            st.open_stake_sum += stake
            if asset:
                st.open_assets[asset] = st.open_assets.get(asset, 0) + 1
            st.trades_today += 1
            st.trades_this_hour += 1

    def on_close_simple(self, won: bool, pnl: float, balance: float,
                        asset: str = "", now: Optional[float] = None) -> None:
        """Backtest-friendly closer without a full Order object."""
        now = now if now is not None else timex.now()
        with self._lock:
            st = self.state
            st.open_count = max(0, st.open_count - 1)
            if asset:
                st.open_assets[asset] = max(0, st.open_assets.get(asset, 1) - 1)
            if won:
                st.consecutive_wins += 1
                st.consecutive_losses = 0
            else:
                st.consecutive_losses += 1
                st.consecutive_wins = 0
                if (
                    self.config.cooldown_after_losses > 0
                    and st.consecutive_losses >= self.config.cooldown_after_losses
                ):
                    st.cooldown_until = now + self.config.cooldown_seconds
                    st.consecutive_losses = 0
            st.last_results.append(bool(won))
            if len(st.last_results) > 200:
                del st.last_results[:-200]
            self.update_balance(balance)
            if self._daily_loss_frac() >= self.config.max_daily_loss_frac:
                st.locked_until = timex.next_daily_boundary(now)
            if self._total_drawdown() >= self.config.max_total_drawdown_frac:
                self.engage_kill("total drawdown limit breached")

    def on_close(self, order: Order, won: bool, pnl: float, balance: float,
                 now: Optional[float] = None) -> None:
        now = now if now is not None else timex.now()
        with self._lock:
            st = self.state
            st.open_count = max(0, st.open_count - 1)
            st.open_stake_sum = max(0.0, st.open_stake_sum - order.amount)
            st.open_assets[order.asset] = max(0, st.open_assets.get(order.asset, 1) - 1)
            cluster = order.meta.get("cluster") or _cluster_of(order.asset)
            st.open_clusters[cluster] = max(0, st.open_clusters.get(cluster, 1) - 1)
            if won:
                st.consecutive_wins += 1
                st.consecutive_losses = 0
            else:
                st.consecutive_losses += 1
                st.consecutive_wins = 0
                if (
                    self.config.cooldown_after_losses > 0
                    and st.consecutive_losses >= self.config.cooldown_after_losses
                ):
                    st.cooldown_until = now + self.config.cooldown_seconds
                    st.consecutive_losses = 0
                    log.warning("cooldown activated after loss streak")
            st.last_results.append(bool(won))
            if len(st.last_results) > 200:
                del st.last_results[:-200]
            self.update_balance(balance)

            daily_loss_frac = self._daily_loss_frac()
            if daily_loss_frac >= self.config.max_daily_loss_frac:
                st.locked_until = timex.next_daily_boundary(now)
                log.warning("DAILY LOSS GOVERNOR locked until %s", st.locked_until)
                default_bus.publish(Topic.RISK, {"event": "daily_lock"}, source="risk")
            total_dd = self._total_drawdown()
            if total_dd >= self.config.max_total_drawdown_frac:
                self.engage_kill("total drawdown limit breached")

        if order.strategy:
            self.record_strategy(order.strategy, won)

    def record_strategy(self, name: str, won: bool) -> None:
        with self._lock:
            rec = self._strategy_wr.setdefault(name, {"n": 0, "w": 0})
            rec["n"] += 1
            if won:
                rec["w"] += 1

    def strategy_win_rate(self, name: str) -> float:
        rec = self._strategy_wr.get(name)
        if not rec or rec["n"] == 0:
            return 0.5
        return rec["w"] / rec["n"]

    # -- introspection -----------------------------------------------------
    def daily_loss_frac(self) -> float:
        return self._daily_loss_frac()

    def total_drawdown(self) -> float:
        return self._total_drawdown()

    def snapshot(self, balance: float) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.state.to_dict(),
                "daily_loss_frac": round(self._daily_loss_frac(), 4),
                "total_drawdown": round(self._total_drawdown(), 4),
                "limits": {
                    "max_daily_loss_frac": self.config.max_daily_loss_frac,
                    "max_total_drawdown_frac": self.config.max_total_drawdown_frac,
                    "max_concurrent": self.config.max_concurrent,
                    "max_trades_per_day": self.config.max_trades_per_day,
                    "min_payout": self.config.min_payout,
                },
                "recent_rejections": self.rejections[-5:],
            }

    def governor_scale(self) -> float:
        """0..1 multiplier the survivor applies to stake in distress."""
        dd = self._total_drawdown()
        cap = self.config.max_total_drawdown_frac
        if cap <= 0:
            return 1.0
        return clamp(1.0 - 0.8 * (dd / cap), 0.2, 1.0)

    # -- internals ---------------------------------------------------------
    def _roll_clock(self, now: float) -> None:
        st = self.state
        # Re-anchor on a fresh window, and also when the event clock moves
        # backwards (simulated/backdated streams) so rate limits stay honest.
        if (
            st.hour_start_ts == 0
            or now - st.hour_start_ts >= 3600
            or now < st.hour_start_ts
        ):
            st.hour_start_ts = now
            st.trades_this_hour = 0
        day = timex.next_daily_boundary(now) - 86400
        if st.day_start_ts == 0:
            st.day_start_ts = day

    def _daily_loss_frac(self, current: Optional[float] = None) -> float:
        st = self.state
        if st.day_start_balance <= 0:
            return 0.0
        cur = st.current_balance if current is None else current
        loss = st.day_start_balance - cur
        return max(0.0, loss / st.day_start_balance)

    def _total_drawdown(self) -> float:
        st = self.state
        if st.peak_balance <= 0:
            return 0.0
        current = st.current_balance or st.day_start_balance
        return max(0.0, (st.peak_balance - current) / st.peak_balance)

    def _record_rejection(self, check: LimitCheck, asset: str, strategy: str) -> None:
        self.rejections.append(
            {
                "ts": timex.now(),
                "asset": asset,
                "strategy": strategy,
                "check": check.to_dict(),
            }
        )
        if len(self.rejections) > 100:
            del self.rejections[:-100]
        log.info("risk reject %s asset=%s :: %s", check.name, asset, check.message or "limit")


def _cluster_of(asset: str) -> str:
    """Quoted/quote currency cluster for correlation caps."""
    name = asset.replace("_otc", "")
    for quote in ("USD", "EUR", "GBP", "JPY"):
        if name.endswith(quote):
            return quote
    return name[:3]


__all__ = ["RiskManager", "RiskState"]
