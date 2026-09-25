"""ALL-WEATHER SURVIVOR — the defensive playbook.

Marketing says "survive every market condition".  Physics says that is
impossible to guarantee; this module is the honest engineering answer: a
condition → response matrix that degrades gracefully in every regime we can
detect, plus an emergency posture for the ones we cannot.

Posture ladder (from most aggressive to most defensive):

    ATTACK → NORMAL → GUARD → DEFENSE → LOCKDOWN

Every detector output maps to a posture with concrete actions: stake scaling,
allowed strategy families, expiry restrictions, and hard trade vetoes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from ..constants import MarketRegime, Side, Timeframe
from ..data.models import Signal
from ..regime.detector import RegimeReading
from ..utils.mathx import clamp

log = logging.getLogger("cybertrade.survivor")


class Posture(str):
    ATTACK = "ATTACK"
    NORMAL = "NORMAL"
    GUARD = "GUARD"
    DEFENSE = "DEFENSE"
    LOCKDOWN = "LOCKDOWN"

    ORDER = (LOCKDOWN, DEFENSE, GUARD, NORMAL, ATTACK)

    @classmethod
    def most_defensive(cls, a: str, b: str) -> str:
        ia = cls.ORDER.index(a) if a in cls.ORDER else 0
        ib = cls.ORDER.index(b) if b in cls.ORDER else 0
        return cls.ORDER[min(ia, ib)]


@dataclass
class SurvivorDecision:
    """Verdict on one candidate trade."""

    allow: bool
    posture: str
    stake_scale: float = 1.0
    max_expiry_seconds: int = 3600
    min_confidence: float = 0.55
    reasons: List[str] = field(default_factory=list)
    forbidden_families: Set[str] = field(default_factory=set)
    slippage_bps: float = 0.0      # expected adverse entry slip (P24)

    @property
    def veto_reason(self) -> str:
        return "; ".join(self.reasons)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allow": self.allow,
            "posture": self.posture,
            "stake_scale": round(self.stake_scale, 3),
            "max_expiry": self.max_expiry_seconds,
            "min_confidence": round(self.min_confidence, 3),
            "reasons": self.reasons,
            "forbidden_families": sorted(self.forbidden_families),
            "slippage_bps": round(self.slippage_bps, 2),
        }


# Regime → base posture
_REGIME_POSTURE = {
    MarketRegime.BULL_TREND: Posture.NORMAL,
    MarketRegime.BEAR_TREND: Posture.NORMAL,
    MarketRegime.RANGE: Posture.NORMAL,
    MarketRegime.LOW_VOL: Posture.GUARD,
    MarketRegime.HIGH_VOL: Posture.GUARD,
    MarketRegime.CRISIS: Posture.DEFENSE,
    MarketRegime.GAP: Posture.DEFENSE,
    MarketRegime.UNKNOWN: Posture.GUARD,
}

# What a disabled playbook reports for the expiry cap. It has to be the
# permissive end, or the engine would keep clamping expiries to a posture the
# operator just switched off.
_PLAYBOOK_OFF_MAX_EXPIRY = 3600

# Posture → (stake scale, confidence floor, max expiry, forbidden families)
_POSTURE_TABLE: Dict[str, Dict[str, Any]] = {
    Posture.ATTACK: {
        "stake_scale": 1.15,
        "min_confidence": 0.52,
        "max_expiry": 3600,
        "forbidden": set(),
    },
    Posture.NORMAL: {
        "stake_scale": 1.0,
        "min_confidence": 0.55,
        "max_expiry": 3600,
        "forbidden": set(),
    },
    Posture.GUARD: {
        "stake_scale": 0.65,
        "min_confidence": 0.62,
        "max_expiry": 600,
        "forbidden": {"pattern"},
    },
    Posture.DEFENSE: {
        "stake_scale": 0.35,
        "min_confidence": 0.72,
        "max_expiry": 300,
        "forbidden": {"meanrev", "pattern", "volatility"},
    },
    Posture.LOCKDOWN: {
        "stake_scale": 0.0,
        "min_confidence": 2.0,
        "max_expiry": 0,
        "forbidden": {"trend", "meanrev", "breakout", "momentum", "volatility", "pattern", "ensemble"},
    },
}


class Survivor:
    """All-weather defense matrix.  Pure logic — no I/O."""

    def __init__(
        self,
        enabled: bool = True,
        weekend_lock: bool = True,
        friday_cutoff_utc: int = 20,
        news_blackout_minutes: float = 5.0,
        spread_limit_mult: float = 3.0,
        liquidity_floor: float = 0.25,
        panic_deleverage: bool = True,
        max_slippage_bps: float = 8.0,
        regime_rotation: bool = True,
        trend_filter: bool = True,
    ) -> None:
        self.enabled = enabled
        self.weekend_lock = weekend_lock
        self.friday_cutoff_utc = friday_cutoff_utc
        self.news_blackout_minutes = news_blackout_minutes
        self.spread_limit_mult = spread_limit_mult
        self.liquidity_floor = liquidity_floor
        self.panic_deleverage = panic_deleverage
        self.max_slippage_bps = max_slippage_bps
        self.regime_rotation = regime_rotation
        self.trend_filter = trend_filter
        self._news_blackout_until = 0.0
        self._manual_lockdown = False
        self.lockdown_reason: str = ""
        self._history: List[str] = []
        if not self.enabled:
            # Running a live account with the defensive playbook off is a
            # deliberate act, so say it out loud instead of letting a config
            # line quietly remove the liquidity, spread, slippage, expiry and
            # confidence vetoes. Manual lockdown and news blackout still work.
            log.critical(
                "SURVIVOR PLAYBOOK DISABLED by config — posture vetoes, "
                "liquidity/spread/slippage limits, expiry and confidence caps "
                "and the trend filter are all OFF. Manual lockdown and news "
                "blackout still apply. Set survivor.enabled=true to restore."
            )

    # -- manual overrides --------------------------------------------------
    def engage_lockdown(self, reason: str = "manual") -> None:
        self._manual_lockdown = True
        self.lockdown_reason = reason
        log.critical("SURVIVOR LOCKDOWN: %s", reason)

    def clear_lockdown(self) -> None:
        self._manual_lockdown = False
        self.lockdown_reason = ""

    def flag_news(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self._news_blackout_until = now + self.news_blackout_minutes * 60.0

    # -- main entry --------------------------------------------------------
    def evaluate(
        self,
        signal: Signal,
        regime: RegimeReading,
        *,
        liquidity: float = 1.0,
        spread_mult: float = 1.0,
        risk_scale: float = 1.0,
        is_otc: bool = True,
        now: Optional[float] = None,
        strategy_family: str = "",
        expected_slippage_bps: float = 0.0,
    ) -> SurvivorDecision:
        now = now if now is not None else time.time()
        posture = self.posture_for(regime, now=now, is_otc=is_otc)
        table = _POSTURE_TABLE[posture]
        reasons: List[str] = []
        forbidden = set(table["forbidden"])

        # --- hard vetoes ---------------------------------------------------
        # These two are not playbook tuning. A manual lockdown is an emergency
        # stop an operator reaches for in a panic, and a news blackout is an
        # explicit operator request; both survive survivor.enabled=False.
        # Turning the playbook off must never turn off the emergency stop.
        if self._manual_lockdown:
            reasons.append("manual lockdown")
        if now < self._news_blackout_until:
            reasons.append("news blackout")

        # --- the playbook itself -------------------------------------------
        # survivor.enabled gates everything below: the posture table, the
        # forbidden families, the crisis and trend filters. It was accepted
        # and stored but never read, so survivor.enabled=false silently left
        # every veto in place -- the flag lied in both directions.
        if self.enabled:
            if liquidity < self.liquidity_floor:
                reasons.append(f"liquidity {liquidity:.2f} below floor {self.liquidity_floor:.2f}")
            if spread_mult > self.spread_limit_mult:
                reasons.append(f"spread x{spread_mult:.1f} above limit x{self.spread_limit_mult:.1f}")
            if expected_slippage_bps > self.max_slippage_bps:
                reasons.append(
                    f"expected slippage {expected_slippage_bps:.1f}bps above limit "
                    f"{self.max_slippage_bps:.1f}bps"
                )
            if signal.expiry_seconds > table["max_expiry"]:
                reasons.append(
                    f"expiry {signal.expiry_seconds}s exceeds posture cap {table['max_expiry']}s"
                )
            if signal.confidence < table["min_confidence"]:
                reasons.append(
                    f"confidence {signal.confidence:.2f} below posture floor {table['min_confidence']:.2f}"
                )
            if strategy_family and strategy_family in forbidden:
                reasons.append(f"family {strategy_family} forbidden in {posture}")

            # do not fade the tail of an extreme move
            if regime.regime in (MarketRegime.CRISIS, MarketRegime.GAP):
                if strategy_family in {"meanrev", "pattern"}:
                    reasons.append("no knife-catching in crisis")

            # survivor.trend_filter: in a clear trend, do not trade against it.
            # This is the playbook's own "bear_trend -- ride puts, forbid
            # knife-catch longs", and the flag was accepted and ignored, so an
            # operator who turned it on got counter-trend entries anyway.
            if self.trend_filter and regime.is_trend:
                if regime.regime is MarketRegime.BEAR_TREND and signal.side.is_long:
                    reasons.append("long into a bear trend — trend filter")
                elif regime.regime is MarketRegime.BULL_TREND and not signal.side.is_long:
                    reasons.append("put into a bull trend — trend filter")
            if posture == Posture.LOCKDOWN and not reasons:
                reasons.append("posture is LOCKDOWN")

        if self.enabled:
            allow = posture != Posture.LOCKDOWN and not reasons
            stake_scale = table["stake_scale"] * risk_scale
        else:
            # Playbook off. Nothing forbids a family, no posture caps the
            # expiry or floors the confidence, and there is no table stake to
            # scale -- so report the permissive end rather than pretending a
            # posture still constrains the trade. panic_deleverage is its own
            # knob and still applies below.
            allow = not reasons
            stake_scale = risk_scale
            forbidden = set()
            if not reasons:
                reasons.append("survivor playbook disabled by config")
        if self.panic_deleverage and regime.stress > 0.5:
            stake_scale *= max(0.3, 1.0 - regime.stress)
        stake_scale = clamp(stake_scale, 0.0, 1.5)

        decision = SurvivorDecision(
            allow=allow,
            posture=posture,
            stake_scale=stake_scale,
            # A disabled playbook must not keep constraining the trade through
            # the back door: the engine clamps the expiry to max_expiry_seconds
            # and reads min_confidence off this decision.
            max_expiry_seconds=(table["max_expiry"] if self.enabled
                                else _PLAYBOOK_OFF_MAX_EXPIRY),
            min_confidence=(table["min_confidence"] if self.enabled else 0.0),
            reasons=reasons or [f"{posture} clearance"],
            forbidden_families=forbidden,
            slippage_bps=expected_slippage_bps,
        )
        self._log_transition(posture)
        return decision

    def posture_for(
        self,
        regime: RegimeReading,
        now: Optional[float] = None,
        is_otc: bool = True,
    ) -> str:
        now = now if now is not None else time.time()
        posture = _REGIME_POSTURE.get(regime.regime, Posture.GUARD)

        # Stress override — the market is already telling us it hurts.
        if regime.stress >= 0.85:
            posture = Posture.most_defensive(posture, Posture.LOCKDOWN)
        elif regime.stress >= 0.65:
            posture = Posture.most_defensive(posture, Posture.DEFENSE)

        # Whipsaw detection is done by callers via stability; keep hook here.
        if regime.confidence < 0.25 and regime.regime is MarketRegime.UNKNOWN:
            posture = Posture.most_defensive(posture, Posture.GUARD)

        # Session protection: classic (non-OTC) venues close over the weekend.
        if not is_otc and self.weekend_lock:
            from ..utils import timex

            if timex.is_weekend_lock(now):
                posture = Posture.LOCKDOWN
        from ..utils import timex

        if timex.is_friday_cutoff(now, self.friday_cutoff_utc):
            posture = Posture.most_defensive(posture, Posture.DEFENSE)

        if self._manual_lockdown:
            posture = Posture.LOCKDOWN
        return posture

    def strategy_filter(self, posture: str) -> Dict[str, Any]:
        """What the engine ensemble should emphasize right now."""
        table = _POSTURE_TABLE.get(posture, _POSTURE_TABLE[Posture.GUARD])
        rotation = {} if not self.regime_rotation else {
            Posture.ATTACK: {"trend": 1.2, "momentum": 1.15, "breakout": 1.1},
            Posture.NORMAL: {"trend": 1.0, "momentum": 1.0, "breakout": 1.0,
                             "meanrev": 1.0, "pattern": 0.9, "volatility": 0.9},
            Posture.GUARD: {"meanrev": 1.1, "pattern": 0.5, "trend": 0.7,
                            "breakout": 0.7, "momentum": 0.8},
            Posture.DEFENSE: {"trend": 0.5, "breakout": 0.5, "momentum": 0.5},
            Posture.LOCKDOWN: {},
        }
        return {
            "weights": rotation.get(posture, {}),
            "forbidden": sorted(table["forbidden"]),
            "stake_scale": table["stake_scale"],
            "min_confidence": table["min_confidence"],
        }

    def emergency(self, reason: str) -> SurvivorDecision:
        """The response to conditions no model predicted."""
        self.engage_lockdown(reason)
        log.critical("SURVIVOR EMERGENCY POSTURE: %s", reason)
        return SurvivorDecision(
            allow=False,
            posture=Posture.LOCKDOWN,
            stake_scale=0.0,
            reasons=[f"emergency: {reason}"],
        )

    # -- observability -----------------------------------------------------
    @property
    def history(self) -> List[str]:
        return self._history[-20:]

    def _log_transition(self, posture: str) -> None:
        if not self._history or self._history[-1] != posture:
            self._history.append(posture)
            if len(self._history) > 200:
                del self._history[:-200]
            log.info("survivor posture → %s", posture)

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "posture_history": self.history,
            "manual_lockdown": self._manual_lockdown,
            "news_blackout_until": self._news_blackout_until,
            "weekend_lock": self.weekend_lock,
            "friday_cutoff_utc": self.friday_cutoff_utc,
            "posture_table": {
                k: {
                    "stake_scale": v["stake_scale"],
                    "min_confidence": v["min_confidence"],
                    "max_expiry": v["max_expiry"],
                    "forbidden": sorted(v["forbidden"]),
                }
                for k, v in _POSTURE_TABLE.items()
            },
        }


def scenario_expectations() -> Dict[str, str]:
    """Documented survivor response per market condition (used in reports)."""
    return {
        "bull_trend": "NORMAL — ride with trend family, trail confidence",
        "bear_trend": "NORMAL — ride puts, forbid knife-catch longs",
        "range_chop": "NORMAL/GUARD — mean-revert only, tight expiries",
        "low_vol_grind": "GUARD — shrink stake, demand 0.62+ confidence",
        "high_vol_expansion": "GUARD — vol strategies only, scale stake 0.65",
        "flash_crash": "DEFENSE/LOCKDOWN — stand down, no averaging in",
        "gap_open": "DEFENSE — forbid entries until two clean candles",
        "news_spike": "blackout window — manual/flag-driven freeze",
        "liquidity_vacuum": "veto on spread/liquidity metrics",
        "regime_whipsaw": "raise confidence floor via low regime stability",
    }


__all__ = ["Survivor", "SurvivorDecision", "Posture", "scenario_expectations"]
