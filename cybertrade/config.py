"""Typed configuration for the terminal: engine, risk, strategies, GUI, web.

Configuration loads/saves as plain JSON so operators can diff and audit it.
Every dataclass validates itself; invalid config fails fast at startup.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .constants import DEFAULT_ASSETS, Timeframe
from .exceptions import ConfigError

DEFAULT_CONFIG_PATH = os.path.join("data", "cybertrade_config.json")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
@dataclass
class RiskConfig:
    """Hard limits.  The risk manager treats these as law, not advice."""

    starting_balance: float = 1000.0
    stake_fraction: float = 0.01          # fraction of balance per attempt
    min_stake: float = 1.0
    max_stake: float = 50.0
    max_concurrent: int = 3               # simultaneous open binaries
    max_trades_per_hour: int = 20
    max_trades_per_day: int = 80
    max_daily_loss_frac: float = 0.08     # stop trading for the day beyond this
    max_total_drawdown_frac: float = 0.20  # kill switch beyond this
    win_rate_floor: float = 0.40          # quarantine strategies below this
    min_payout: float = 0.85              # the hurdle is brutal below this (54%+ WR needed)
    edge_gate: str = "scale"              # off | scale | hard (calibrated edge gate)
    min_edge: float = 0.05                # calibrated P(win) edge needed for full size
    expiry_select: str = "signal"         # signal | adaptive (best_expiry chooser)
    expiry_candidates: List[int] = field(default_factory=lambda: [30, 60, 120, 300])
    regime_cal: bool = True               # regime-conditional P(win) — the WHEN matrix
    max_correlated_exposure: int = 2      # open trades sharing quote currency
    cooldown_after_losses: int = 3        # consecutive losses -> cooldown
    cooldown_seconds: float = 120.0
    martingale_enabled: bool = False      # strongly discouraged; off by default
    martingale_cap: float = 2.0           # max stake multiple if ever enabled
    vol_target_enabled: bool = True
    vol_target_annual: float = 0.15
    kelly_fraction: float = 0.25          # fractional Kelly ceiling
    crisis_stake_scale: float = 0.5       # cut size in crisis regime
    allow_live: bool = False              # must be flipped consciously

    def validate(self) -> None:
        if self.starting_balance <= 0:
            raise ConfigError("starting_balance must be > 0")
        if not 0.0 < self.stake_fraction <= 0.25:
            raise ConfigError("stake_fraction must be in (0, 0.25]")
        if self.min_stake <= 0 or self.max_stake < self.min_stake:
            raise ConfigError("invalid stake band")
        if self.max_concurrent < 1:
            raise ConfigError("max_concurrent must be >= 1")
        if not 0.0 < self.max_daily_loss_frac < self.max_total_drawdown_frac <= 1.0:
            raise ConfigError("drawdown fractions must satisfy 0 < daily < total <= 1")
        if not 0.0 <= self.win_rate_floor <= 1.0:
            raise ConfigError("win_rate_floor must be in [0, 1]")
        if not 0.0 <= self.min_payout < 1.0:
            raise ConfigError("min_payout must be in [0, 1)")
        if self.martingale_cap < 1.0:
            raise ConfigError("martingale_cap must be >= 1.0")
        if not 0.0 < self.kelly_fraction <= 1.0:
            raise ConfigError("kelly_fraction must be in (0, 1]")
        if self.edge_gate not in {"off", "scale", "hard"}:
            raise ConfigError("edge_gate must be off | scale | hard")
        if not -1.0 < self.min_edge < 1.0:
            raise ConfigError("min_edge must be in (-1, 1)")
        if self.expiry_select not in {"signal", "adaptive"}:
            raise ConfigError("expiry_select must be signal | adaptive")
        if not self.expiry_candidates or any(int(x) <= 0 for x in self.expiry_candidates):
            raise ConfigError("expiry_candidates must be positive seconds")


@dataclass
class StrategyConfig:
    """Which strategies run, on which assets, and how the ensemble blends them."""

    enabled: List[str] = field(default_factory=lambda: ["ensemble_all_weather"])
    disabled: List[str] = field(default_factory=list)
    universe: List[str] = field(default_factory=lambda: list(DEFAULT_ASSETS))
    timeframe: str = Timeframe.M1.value
    expiry_seconds: int = 60
    min_confidence: float = 0.55
    ensemble_mode: str = "regime_weighted"   # | majority | best | unanimous
    adaptive_weights: bool = True
    max_signals_per_candle: int = 2
    trade_on_weak: bool = False

    def validate(self) -> None:
        try:
            Timeframe(self.timeframe)
        except ValueError as exc:
            raise ConfigError(f"unknown timeframe {self.timeframe!r}") from exc
        if self.expiry_seconds <= 0:
            raise ConfigError("expiry_seconds must be > 0")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ConfigError("min_confidence must be in [0, 1]")
        if self.ensemble_mode not in {"regime_weighted", "majority", "best", "unanimous"}:
            raise ConfigError(f"unknown ensemble_mode {self.ensemble_mode!r}")
        if not self.universe:
            raise ConfigError("universe must not be empty")


@dataclass
class SurvivorConfig:
    """All-weather defensive playbook toggles."""

    enabled: bool = True
    trend_filter: bool = True
    news_blackout_minutes: float = 5.0
    spread_limit_mult: float = 3.0
    liquidity_floor: float = 0.25
    panic_deleverage: bool = True
    friday_cutoff_utc: int = 20              # stop opening trades after this hour
    weekend_lock: bool = True
    max_slippage_bps: float = 8.0
    regime_rotation: bool = True

    def validate(self) -> None:
        if self.spread_limit_mult < 1.0:
            raise ConfigError("spread_limit_mult must be >= 1")
        if not 0.0 <= self.liquidity_floor <= 1.0:
            raise ConfigError("liquidity_floor must be in [0, 1]")
        if not 0 <= self.friday_cutoff_utc <= 23:
            raise ConfigError("friday_cutoff_utc must be an hour 0..23")


@dataclass
class BrokerConfig:
    """Connection settings.  Paper is the default mode on purpose."""

    mode: str = "paper"                       # paper | quotex | dryrun
    ws_url: str = ""
    http_base: str = ""
    username: str = ""
    password: str = ""                        # never serialized to disk
    demo_account: bool = True                 # prefer broker demo balance
    payout_default: float = 0.85
    latency_ms: int = 180
    slippage_bps: float = 0.5
    reconnect_max: int = 12
    request_timeout: float = 15.0

    def validate(self) -> None:
        if self.mode not in {"paper", "quotex", "dryrun"}:
            raise ConfigError(f"unknown broker mode {self.mode!r}")
        if not 0.0 < self.payout_default < 1.0:
            raise ConfigError("payout_default must be in (0, 1)")
        if self.latency_ms < 0:
            raise ConfigError("latency_ms must be >= 0")

    def redacted(self) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        if data.get("password"):
            data["password"] = "***"
        return data


@dataclass
class DisplayConfig:
    """GUI + web chrome."""

    theme: str = "neon_abyss"                 # neon_abyss | magenta_hell | ghost_cyan
    scanlines: bool = True
    glow: bool = True
    animate: bool = True
    fps: int = 24
    show_grid: bool = True
    log_level: str = "INFO"
    web_port: int = 8899
    web_host: str = "0.0.0.0"

    def validate(self) -> None:
        if self.theme not in {"neon_abyss", "magenta_hell", "ghost_cyan"}:
            raise ConfigError(f"unknown theme {self.theme!r}")
        if not 1 <= self.fps <= 120:
            raise ConfigError("fps must be 1..120")
        if not 1 <= self.web_port <= 65535:
            raise ConfigError("web_port must be 1..65535")


@dataclass
class BacktestConfig:
    starting_balance: float = 1000.0
    payout: float = 0.85
    spread_bps: float = 0.5
    latency_ms: int = 150
    warmup_bars: int = 120
    scenarios: List[str] = field(
        default_factory=lambda: [
            "bull_trend",
            "bear_trend",
            "range_chop",
            "low_vol_grind",
            "high_vol_expansion",
            "flash_crash",
            "gap_open",
            "news_spike",
            "liquidity_vacuum",
            "regime_whipsaw",
        ]
    )

    def validate(self) -> None:
        if self.starting_balance <= 0:
            raise ConfigError("backtest starting_balance must be > 0")
        if not 0.0 < self.payout < 1.0:
            raise ConfigError("backtest payout must be in (0, 1)")
        if self.warmup_bars < 1:
            raise ConfigError("warmup_bars must be >= 1")


@dataclass
class AlertConfig:
    """Alert center delivery settings."""

    enable_sound: bool = True
    enable_webhook: bool = False
    webhook_url: str = ""
    trade_loss_alert: float = 50.0     # single-loss alert threshold
    equity_move_pct: float = 3.0       # equity swing alert threshold
    daily_target_alert: bool = True

    def validate(self) -> None:
        if self.enable_webhook and not self.webhook_url:
            raise ConfigError("enable_webhook set but webhook_url empty")
        if self.trade_loss_alert < 0:
            raise ConfigError("trade_loss_alert must be >= 0")
        if self.equity_move_pct <= 0:
            raise ConfigError("equity_move_pct must be > 0")


@dataclass
class AppConfig:
    """Root configuration object."""

    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    survivor: SurvivorConfig = field(default_factory=SurvivorConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    journal_path: str = os.path.join("data", "journal.db")
    plugins_dir: str = os.path.join("~", ".cybertrade", "plugins")
    calendar_path: str = os.path.join("~", ".cybertrade", "calendar.json")
    tape_dir: str = os.path.join("data", "tapes")
    tape_enabled: bool = True
    log_path: str = os.path.join("data", "cybertrade.log")
    config_path: str = DEFAULT_CONFIG_PATH

    def validate(self) -> None:
        self.risk.validate()
        self.strategy.validate()
        self.survivor.validate()
        self.broker.validate()
        self.display.validate()
        self.backtest.validate()

    # -- persistence -------------------------------------------------------
    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        def _encode(obj: Any) -> Any:
            if isinstance(obj, Timeframe):
                return obj.value
            raise TypeError(type(obj))

        data = {
            "risk": dataclasses.asdict(self.risk),
            "strategy": dataclasses.asdict(self.strategy),
            "survivor": dataclasses.asdict(self.survivor),
            "broker": dataclasses.asdict(self.broker),
            "display": dataclasses.asdict(self.display),
            "backtest": dataclasses.asdict(self.backtest),
            "journal_path": self.journal_path,
            "log_path": self.log_path,
        }
        if not include_secrets:
            data["broker"]["password"] = ""
        return json.loads(json.dumps(data, default=_encode))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        try:
            cfg = cls(
                risk=RiskConfig(**data.get("risk", {})),
                strategy=StrategyConfig(**data.get("strategy", {})),
                survivor=SurvivorConfig(**data.get("survivor", {})),
                broker=BrokerConfig(**data.get("broker", {})),
                display=DisplayConfig(**data.get("display", {})),
                backtest=BacktestConfig(**data.get("backtest", {})),
                journal_path=data.get("journal_path", cls.journal_path),
                log_path=data.get("log_path", cls.log_path),
            )
        except TypeError as exc:
            raise ConfigError(f"bad config shape: {exc}") from exc
        cfg.validate()
        return cfg

    def save(self, path: Optional[str] = None) -> str:
        target = path or self.config_path
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(include_secrets=False), fh, indent=2, sort_keys=True)
        return target

    @classmethod
    def load(cls, path: Optional[str] = None) -> "AppConfig":
        target = path or DEFAULT_CONFIG_PATH
        if not os.path.exists(target):
            cfg = cls()
            cfg.validate()
            return cfg
        try:
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot load config {target}: {exc}") from exc
        return cls.from_dict(data)

    # -- helpers -----------------------------------------------------------
    def timeframe(self) -> Timeframe:
        return Timeframe(self.strategy.timeframe)

    def copy(self) -> "AppConfig":
        return AppConfig.from_dict(self.to_dict(include_secrets=True))


__all__ = [
    "AppConfig",
    "RiskConfig",
    "StrategyConfig",
    "SurvivorConfig",
    "BrokerConfig",
    "DisplayConfig",
    "BacktestConfig",
    "DEFAULT_CONFIG_PATH",
]
