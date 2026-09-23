"""Session clock — time-of-day liquidity profiles per asset class (UTC).

Phase-22: "every market condition" includes WHEN the market is. 03:00 Asia
is not the 12:00 London/NY overlap, and a weekend on synthetic OTC is not
Tuesday. Conservative heuristic profiles (not a claim about any venue): the
session scale multiplies the survivor's stake_scale, so a thin tape takes a
smaller share of the bankroll while evidence models stay cold on conditions
they were never calibrated for.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable

from ..utils import timex

# Quotex-style symbols: EURUSD_otc, BTCUSD_otc, XAUUSD_otc, US500_otc.
_CRYPTOS = ("BTC", "ETH", "LTC", "XRP", "DOGE", "ADA", "SOL", "BNB")
_METALS = ("XAU", "XAG", "XPT")


def asset_class(asset: str) -> str:
    """Coarse asset class from a broker symbol ('EURUSD_otc' -> 'fx')."""
    a = asset.upper().replace("_OTC", "").replace("/", "")
    if a.startswith(_CRYPTOS):
        return "crypto"
    if a.startswith(_METALS):
        return "metal"
    if a.startswith("US") and len(a) > 2 and a[2].isdigit():
        return "index"  # US500 / US100 / US30
    return "fx"


@dataclass(frozen=True)
class SessionInfo:
    """One wall-clock slice: label, liquidity feel, stake multiplier."""

    name: str        # asia | london | overlap | newyork | offhours
    liquidity: str   # thick | normal | thin
    scale: float     # 0..1 stake multiplier for this tape
    weekend: bool    # classic markets closed (OTC keeps synthesizing)
    asset_class: str  # fx | crypto | metal | index


# Heuristic per-class scales keyed by FX session (timex.session_of_day).
_FX_SCALE = {"overlap": 1.0, "london": 1.0, "newyork": 1.0,
             "asia": 0.75, "offhours": 0.5}
_CRYPTO_SCALE = {"overlap": 1.0, "london": 1.0, "newyork": 1.0,
                 "asia": 0.85, "offhours": 0.7}   # 24/7 but overnight is thin
_METAL_SCALE = {"overlap": 1.0, "london": 1.0, "newyork": 1.0,
                "asia": 0.9, "offhours": 0.5}
_INDEX_SCALE = {"overlap": 1.0, "london": 0.75, "newyork": 1.0,
                "asia": 0.5, "offhours": 0.35}    # US index outside US hours

WEEKEND_SCALE = 0.25  # synthetic weekend tape — deep discount, not a ban


def session_for(asset: str, ts: float) -> SessionInfo:
    """Session profile for ``asset`` at wall-clock ``ts`` (UTC)."""
    cls = asset_class(asset)
    name = timex.session_of_day(ts)
    weekend = timex.is_weekend_lock(ts) and cls != "crypto"
    table = {"crypto": _CRYPTO_SCALE, "metal": _METAL_SCALE,
             "index": _INDEX_SCALE}.get(cls, _FX_SCALE)
    scale = table.get(name, 0.5)
    if weekend:
        scale = min(scale, WEEKEND_SCALE)
    liquidity = "thick" if scale >= 0.9 else ("normal" if scale >= 0.6 else "thin")
    return SessionInfo(name=name, liquidity=liquidity, scale=scale,
                       weekend=weekend, asset_class=cls)


def session_report(assets: Iterable[str], ts: float) -> Dict[str, Any]:
    """Per-asset snapshot for the cockpit HUD and tests."""
    infos = {a: session_for(a, ts) for a in assets}
    return {
        "name": timex.session_of_day(ts),
        "weekend": timex.is_weekend_lock(ts),
        "min_scale": min((i.scale for i in infos.values()), default=1.0),
        "assets": {
            a: {"class": i.asset_class, "liquidity": i.liquidity,
                "scale": i.scale, "weekend": i.weekend}
            for a, i in infos.items()
        },
    }


__all__ = ["asset_class", "SessionInfo", "session_for", "session_report",
           "WEEKEND_SCALE"]
