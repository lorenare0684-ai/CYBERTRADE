"""Named stress scenarios and combined 'gauntlet' presets for the survivor."""

from __future__ import annotations

from typing import Dict, List

from ..data.synthetic import SCENARIO_NAMES, MarketParams, generate_candles, scenario_catalog

GAUNTLET: Dict[str, Dict[str, object]] = {
    "bull_trend": {
        "description": "Persistent grind up with noise — trend followers feast.",
        "params": MarketParams(drift=0.0008, base_vol=0.0005),
        "expect": "positive trend-family PnL, few revenge shorts",
    },
    "bear_trend": {
        "description": "Relentless decline — knife-catchers die here.",
        "params": MarketParams(drift=-0.0008, base_vol=0.0005),
        "expect": "put bias, no averaging into longs",
    },
    "range_chop": {
        "description": "OU mean reversion around an anchor — whipsaw hell.",
        "params": MarketParams(base_vol=0.0006, mean_reversion=0.12),
        "expect": "mean-revert families only; trend family quarantined",
    },
    "low_vol_grind": {
        "description": "Compressed vol drip — death by a thousand paper cuts.",
        "params": MarketParams(base_vol=0.00025),
        "expect": "GUARD posture, stake scaled down, high confidence floor",
    },
    "high_vol_expansion": {
        "description": "Violent swings, wide bars — size blowup central.",
        "params": MarketParams(base_vol=0.0018, vol_of_vol=0.05),
        "expect": "GUARD, volatility-family emphasis, reduced size",
    },
    "flash_crash": {
        "description": "Instant collapse and partial recovery — the ruin event.",
        "params": MarketParams(base_vol=0.0007, jump_prob=0.01, jump_scale=0.02),
        "expect": "DEFENSE/LOCKDOWN posture, capital preserved",
    },
    "gap_open": {
        "description": "Discontinuous opens that jump strikes — slippage torture.",
        "params": MarketParams(base_vol=0.0006, jump_prob=0.02, jump_scale=0.008),
        "expect": "DEFENSE posture, entries blocked near gaps",
    },
    "news_spike": {
        "description": "Impulse wicks and snap-backs — top-tick entries punished.",
        "params": MarketParams(base_vol=0.0008, jump_prob=0.015, jump_scale=0.012),
        "expect": "news blackout behaviour, spike fades only",
    },
    "liquidity_vacuum": {
        "description": "Teleporting prints and spread blowouts — friction death.",
        "params": MarketParams(base_vol=0.001, jump_prob=0.03, jump_scale=0.005, spread_bps=3.0),
        "expect": "spread vetoes trigger, minimal trading",
    },
    "regime_whipsaw": {
        "description": "Rapid regime switching — overfit systems bleed out.",
        "params": MarketParams(base_vol=0.0006, mean_reversion=0.08, drift=0.0004),
        "expect": "ensemble indecision vetoes protect the bankroll",
    },
}


def scenario_names() -> List[str]:
    return list(GAUNTLET)


def generate_gauntlet(
    bars: int = 600,
    seed: int = 1337,
    timeframe_seconds: int = 60,
):
    """Produce candle sets for every gauntlet scenario in one call."""
    out = {}
    for name, spec in GAUNTLET.items():
        params: MarketParams = spec["params"]  # type: ignore[assignment]
        params.timeframe_seconds = timeframe_seconds
        out[name] = generate_candles(name, bars=bars, params=params, seed=seed, asset=name)
    return out


def describe_all() -> List[dict]:
    """Combined catalog: synthetic generator names + gauntlet expectations."""
    base = {row["name"]: row for row in scenario_catalog()}
    rows = []
    for name, spec in GAUNTLET.items():
        row = dict(base.get(name, {}))
        row.update({"name": name, "description": spec["description"], "expect": spec["expect"]})
        rows.append(row)
    return rows


__all__ = ["GAUNTLET", "scenario_names", "generate_gauntlet", "describe_all"]
