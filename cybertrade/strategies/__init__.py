"""Strategy registry and factory helpers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from ..exceptions import StrategyError
from .base import Strategy, StrategyContext
from .breakout import BandwidthPierce, DonchianBreak, KeltnerRide, RangeEscape, SqueezePop
from .ensemble import AllWeatherEnsemble, DefensiveVeto
from .meanrev import (
    BollingerRevert,
    CCIRevert,
    PinBarRevert,
    RSIStretch,
    StochHook,
    WilliamsFade,
)
from .momentum import (
    ACShove,
    AOFlip,
    ElderImpulse,
    MomentumBurst,
    ROCLaunch,
    RSIMomentum,
)
from .pattern import (
    DoubleWickRejection,
    EngulfingEdge,
    HammerHook,
    InsideBreakoutBar,
    SoldiersCrows,
)
from .trend import (
    ADXTrendStorm,
    EMACrossTrend,
    IchimokuKumo,
    MacdTrendRider,
    PSARFlip,
    SupertrendSurf,
)
from .volatility import (
    GarchMeanRev,
    HVContrarianFade,
    SqueezeRelease,
    VolExpansionRider,
)
from .divergence import (
    CCIDivergenceFade,
    MacdHiddenDivergence,
    MultiTimeframeConfluence,
    RSIDivergenceEdge,
)
from .orderflow import AbsorptionFade, ImbalanceMomentum, POCReversion

STRATEGY_REGISTRY: Dict[str, Type[Strategy]] = {
    # trend
    "ema_cross_trend": EMACrossTrend,
    "macd_trend_rider": MacdTrendRider,
    "supertrend_surf": SupertrendSurf,
    "ichimoku_kumo": IchimokuKumo,
    "psar_flip": PSARFlip,
    "adx_trend_storm": ADXTrendStorm,
    # mean reversion
    "bollinger_revert": BollingerRevert,
    "rsi_stretch": RSIStretch,
    "stoch_hook": StochHook,
    "cci_revert": CCIRevert,
    "williams_fade": WilliamsFade,
    "pin_bar_revert": PinBarRevert,
    # breakout
    "donchian_break": DonchianBreak,
    "keltner_ride": KeltnerRide,
    "squeeze_pop": SqueezePop,
    "bandwidth_pierce": BandwidthPierce,
    "range_escape": RangeEscape,
    # momentum
    "roc_launch": ROCLaunch,
    "momentum_burst": MomentumBurst,
    "ao_flip": AOFlip,
    "ac_shove": ACShove,
    "elder_impulse": ElderImpulse,
    "rsi_momentum": RSIMomentum,
    # volatility
    "vol_expansion_rider": VolExpansionRider,
    "squeeze_release": SqueezeRelease,
    "garch_meanrev": GarchMeanRev,
    "hv_contrarian_fade": HVContrarianFade,
    # pattern
    "engulfing_edge": EngulfingEdge,
    "hammer_hook": HammerHook,
    "soldiers_crows": SoldiersCrows,
    "inside_breakout": InsideBreakoutBar,
    "double_wick": DoubleWickRejection,
    # divergence
    "rsi_divergence": RSIDivergenceEdge,
    "macd_hidden_divergence": MacdHiddenDivergence,
    "cci_divergence_fade": CCIDivergenceFade,
    "mtf_confluence": MultiTimeframeConfluence,
    # orderflow
    "imbalance_momentum": ImbalanceMomentum,
    "absorption_fade": AbsorptionFade,
    "poc_reversion": POCReversion,
    # meta
    "defensive_veto": DefensiveVeto,
}


def list_strategies() -> List[str]:
    return sorted(STRATEGY_REGISTRY)


def create(name: str, **params) -> Strategy:
    try:
        cls = STRATEGY_REGISTRY[name]
    except KeyError as exc:
        raise StrategyError(f"unknown strategy {name!r}") from exc
    return cls(**params)


def build_universe(names: Optional[List[str]] = None) -> List[Strategy]:
    names = names or list_strategies()
    out: List[Strategy] = []
    for name in names:
        if name in STRATEGY_REGISTRY:
            out.append(create(name))
    return out


#: The sentinel in ``StrategyConfig.enabled`` that means "run them all".
#: It is deliberately not a registry name: an operator who writes a list of
#: real strategy names narrows the ensemble, and anything else -- including
#: the default -- leaves it whole.
ALL_WEATHER = "ensemble_all_weather"


def configured_members(scfg: Any) -> List[str]:
    """Resolve ``strategy.enabled`` / ``strategy.disabled`` to member names.

    Both fields existed in the config and neither reached the ensemble: the
    engine built it from every registered strategy, so an operator who
    disabled a strategy that was losing them money got no effect at all.
    ``enabled`` defaults to the ``ensemble_all_weather`` sentinel, which is
    not a registry name -- only a list of real names narrows the set.
    """
    everything = list_strategies()
    asked = [n for n in (getattr(scfg, "enabled", None) or [])
             if n in STRATEGY_REGISTRY]
    narrowed = bool(asked)
    names = asked or list(everything)
    drop = set(getattr(scfg, "disabled", None) or [])
    if drop:
        names = [n for n in names if n not in drop]
    if narrowed and not names:
        # The operator named real strategies and then disabled every one of
        # them. Falling back to "all of them" would silently re-enable the
        # very strategies they asked to turn off.
        from ..exceptions import ConfigError

        raise ConfigError(
            "strategy.enabled and strategy.disabled together leave no "
            "strategies to run; remove one of them")
    return names or everything


def build_all_weather(
    member_names: Optional[List[str]] = None,
    mode: str = "regime_weighted",
    adaptive: bool = True,
    min_confidence: float = 0.55,
    **kw,
) -> AllWeatherEnsemble:
    members = build_universe(member_names)
    return AllWeatherEnsemble(
        members=members,
        mode=mode,
        adaptive=adaptive,
        min_confidence=min_confidence,
        **kw,
    )


__all__ = [
    "Strategy",
    "StrategyContext",
    "STRATEGY_REGISTRY",
    "list_strategies",
    "create",
    "build_universe",
    "build_all_weather",
    "AllWeatherEnsemble",
    "DefensiveVeto",
]
