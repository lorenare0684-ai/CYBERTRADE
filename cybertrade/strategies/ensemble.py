"""Regime-aware ensemble meta-strategy.

Blends subordinate strategy votes with:

1. regime suitability scores (trend strategies win in trends, etc.),
2. adaptive EWMA performance weights that decay when a strategy stops
   performing (quarantine below the win-rate floor),
3. consensus rules (majority / unanimous / best / regime_weighted).

The ensemble also emits ``flat`` vetoes when evidence conflicts — surviving
every market condition means trading less, not more.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Mapping, Optional, Sequence

from ..constants import MarketRegime, Side, SignalStrength
from ..data.models import Signal
from ..utils.mathx import clamp
from .base import Strategy, StrategyContext

log = logging.getLogger("cybertrade.ensemble")


class AllWeatherEnsemble(Strategy):
    """Meta-strategy: the one the engine actually trades."""

    name = "ensemble_all_weather"
    label = "ALL-WEATHER ENSEMBLE"
    family = "ensemble"
    min_bars = 35

    def __init__(
        self,
        members: Optional[Sequence[Strategy]] = None,
        mode: str = "regime_weighted",
        adaptive: bool = True,
        min_confidence: float = 0.55,
        min_dominance: float = 0.60,
        win_rate_floor: float = 0.40,
        decay: float = 0.985,
        max_votes: int = 0,
        family_weights: Optional[Mapping[str, float]] = None,
        **kw,
    ) -> None:
        self.members: List[Strategy] = list(members or [])
        self.mode = mode
        self.adaptive = adaptive
        self.min_confidence = min_confidence
        self.min_dominance = min_dominance
        self.win_rate_floor = win_rate_floor
        self.decay = decay
        # 0 = no cap: the ensemble blends every member by design, and the
        # old default of 2 was never enforced at all.
        self.max_votes = max(0, int(max_votes))
        # Posture -> strategy-family bias, pushed in by the engine from
        # Survivor.strategy_filter(). Empty means "no bias".
        self.family_weights: Dict[str, float] = dict(family_weights or {})
        self._weights: Dict[str, float] = {m.name: 1.0 for m in self.members}
        self._scores: Dict[str, float] = {m.name: 0.5 for m in self.members}
        self.quarantined_votes = set()  # Phase-25: decay ward (engine-synced)
        self.vetoes = 0
        super().__init__(
            mode=mode,
            adaptive=adaptive,
            min_confidence=min_confidence,
            min_dominance=min_dominance,
            members=[m.name for m in self.members],
            **kw,
        )

    def attach(self, strategy: Strategy) -> None:
        self.members.append(strategy)
        self._weights[strategy.name] = 1.0
        self._scores[strategy.name] = 0.5

    # -- decision ----------------------------------------------------------
    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        votes: List[Signal] = []
        for member in self.members:
            if not member.enabled or self._quarantined(member):
                continue
            if member.name in self.quarantined_votes:
                continue  # Phase-25: decay ward — do not listen to the fading
            sig = member.generate(ctx)
            if sig is not None and sig.is_trade:
                votes.append(sig)

        if not votes:
            return None
        if self.max_votes and len(votes) > self.max_votes:
            # strategy.max_signals_per_candle: blend the strongest N votes
            # rather than all of them. Strongest first, so the cap can only
            # drop the weakest evidence, never the best. Weighted the same way
            # _blend and the "best" mode weight votes, so a vote the posture
            # table leans on is not dropped for being from a dampened family.
            votes = sorted(
                votes,
                key=lambda s: s.quality * self._effective_weight(ctx, s),
                reverse=True,
            )
            votes = votes[: self.max_votes]

        weighted = self._blend(ctx, votes)
        if weighted is None:
            self.vetoes += 1
            return None

        side, confidence, reasons = weighted
        if confidence < self.min_confidence:
            self.vetoes += 1
            return None

        # crisis regime: demand supermajority agreement
        if ctx.regime.regime in (MarketRegime.CRISIS, MarketRegime.GAP):
            agree = sum(1 for v in votes if v.side == side)
            if agree / len(votes) < 0.75:
                self.vetoes += 1
                return None
            confidence *= 0.85

        strength = SignalStrength.STRONG if confidence >= 0.72 else SignalStrength.MODERATE
        sig = self._signal(
            ctx,
            side,
            confidence,
            f"ensemble[{self.mode}]: {'; '.join(reasons[:3])}",
            votes=[v.to_dict() for v in votes],
            voters=len(votes),
            mode=self.mode,
        )
        sig.strength = strength
        sig.strategy = self.name
        return sig

    # -- blending ----------------------------------------------------------
    def _blend(
        self, ctx: StrategyContext, votes: Sequence[Signal]
    ) -> Optional[tuple]:
        calls = [v for v in votes if v.side is Side.CALL]
        puts = [v for v in votes if v.side is Side.PUT]

        if self.mode == "unanimous":
            # strict: every voter must agree (all-PUT must never fire CALL)
            if calls and not puts:
                side = Side.CALL
            elif puts and not calls:
                side = Side.PUT
            else:
                return None
            conf = sum(v.confidence for v in votes) / len(votes)
            return side, conf, ["unanimous"]

        if self.mode == "majority":
            if len(calls) == len(puts):
                return None
            side = Side.CALL if len(calls) > len(puts) else Side.PUT
            group = calls if side is Side.CALL else puts
            ratio = len(group) / max(1, len(votes))
            if ratio < 0.6:
                return None  # thin pluralities are noise
            conf = sum(v.confidence for v in group) / len(group)
            return side, clamp(conf * (0.5 + 0.5 * ratio), 0, 1), [
                f"majority {len(group)}/{len(votes)}"
            ]

        if self.mode == "best":
            top = max(votes, key=lambda v: v.quality * self._effective_weight(ctx, v))
            return top.side, top.confidence, [f"best {top.strategy}"]

        # default: regime_weighted
        call_w = sum(self._effective_weight(ctx, v) * v.confidence for v in calls)
        put_w = sum(self._effective_weight(ctx, v) * v.confidence for v in puts)
        total = call_w + put_w
        if total <= 0:
            return None
        if call_w == put_w:
            return None  # conflict — stand down
        side = Side.CALL if call_w > put_w else Side.PUT
        dominance = max(call_w, put_w) / total
        if dominance < self.min_dominance:
            return None  # contested tape — wait for clarity
        winner = calls if side is Side.CALL else puts
        # Weighted mean, not a plain one. dominance is a ratio and so is
        # invariant to scaling every vote alike, which left the posture bias
        # able to flip the side but never to move the strength: on a
        # single-sided tape it had no effect at all. A vote from a family the
        # posture dampens has to count for less than one it leans on.
        wsum = sum(self._effective_weight(ctx, v) for v in winner)
        if wsum > 0:
            avg_conf = sum(self._effective_weight(ctx, v) * v.confidence
                           for v in winner) / wsum
        else:
            avg_conf = sum(v.confidence for v in winner) / len(winner)
        # corroboration discount: one voter must not mint its own consensus
        support = 0.8 + 0.1 * min(len(winner), 2)      # 1 vote -> 0.9, 2+ -> 1.0
        conf = clamp(avg_conf * (0.55 + 0.45 * dominance) * support, 0.0, 1.0)
        reasons = [f"{v.strategy}:{v.confidence:.2f}" for v in winner]
        return side, conf, reasons

    def set_family_weights(self, weights: Optional[Mapping[str, float]]) -> None:
        """Bias the blend by strategy family.

        The engine calls this each cycle with the survivor's posture table, so
        a GUARD tape leans on mean reversion and dampens pattern and trend
        entries. An empty or None mapping removes the bias entirely.
        """
        clean: Dict[str, float] = {}
        for family, mult in (weights or {}).items():
            try:
                value = float(mult)
            except (TypeError, ValueError):
                continue
            if value > 0.0:
                clean[str(family)] = value
        self.family_weights = clean

    def _family_weight(self, vote: Signal) -> float:
        """The posture multiplier for the member that cast this vote."""
        if not self.family_weights:
            return 1.0
        member = self._member(vote.strategy)
        family = getattr(member, "family", "") if member else ""
        return self.family_weights.get(family, 1.0)

    def _effective_weight(self, ctx: StrategyContext, vote: Signal) -> float:
        base = self._weights.get(vote.strategy, 1.0) * self._family_weight(vote)
        member = self._member(vote.strategy)
        regime_fit = member.score_for_regime(ctx.regime.regime) if member else 0.5
        if self.mode == "regime_weighted":
            return base * (0.35 + 0.65 * regime_fit)
        return base

    def _member(self, name: str) -> Optional[Strategy]:
        for m in self.members:
            if m.name == name:
                return m
        return None

    def _quarantined(self, member: Strategy) -> bool:
        return self.adaptive and member.attempts >= 10 and member.win_rate < self.win_rate_floor

    # -- learning ----------------------------------------------------------
    def record_result(self, won: bool, pnl: float) -> None:
        super().record_result(won, pnl)

    def reinforce_vote(self, strategy_name: str, won: bool) -> None:
        """Called by the engine with each contributing vote's outcome.

        Weights hover around 1.0 (neutral EWMA score 0.5 maps to 1.0); wins
        push up toward 2.0, losses down toward 0.1.
        """
        if not self.adaptive:
            return
        old = self._scores.get(strategy_name, 0.5)
        target = 1.0 if won else 0.0
        self._scores[strategy_name] = old * 0.92 + target * 0.08
        self._weights[strategy_name] = clamp(
            0.5 + self._scores[strategy_name], 0.1, 2.0
        )
        member = self._member(strategy_name)
        if member:
            member.record_result(won, 0.0)

    def describe(self) -> dict:
        data = super().describe()
        data["mode"] = self.mode
        data["vetoes"] = self.vetoes
        data["members"] = [
            {
                **m.describe(),
                "winrate_quarantined": self._quarantined(m),
                "decay_quarantined": m.name in self.quarantined_votes,
            }
            for m in self.members
        ]
        data["decay_ward"] = sorted(self.quarantined_votes)
        data["weights"] = dict(self._weights)
        data["scores"] = {k: round(v, 3) for k, v in self._scores.items()}
        # What the survivor's posture is currently leaning on, and how many
        # votes per candle are blended. Both change trading behaviour, so an
        # operator reading the HUD should be able to see them rather than
        # having to infer them from the config file.
        data["family_weights"] = {k: round(v, 3)
                                  for k, v in sorted(self.family_weights.items())}
        data["max_votes"] = self.max_votes
        return data


class DefensiveVeto(Strategy):
    """Always-on 'strategy' whose job is to trade nothing — it exists so the
    ensemble registry can express explicit stand-down during crisis."""

    name = "defensive_veto"
    label = "Defensive Veto"
    family = "defense"
    min_bars = 1
    preferred_regimes = (MarketRegime.CRISIS, MarketRegime.GAP)

    def decide(self, ctx: StrategyContext) -> Optional[Signal]:
        return None


__all__ = ["AllWeatherEnsemble", "DefensiveVeto"]
