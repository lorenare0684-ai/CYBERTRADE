"""Binary-option mathematics: hurdles, edges, Kelly, and expiry profiles.

All probabilities are plain floats in [0, 1]; payouts are fractions
(0.85 = 85% profit on a win, stake lost otherwise).  Fixed-fraction sizing
means true ruin is unreachable — the Kelly numbers here size *risk*, never
"recovery".
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from ..exceptions import CybertradeError
from ..utils.mathx import clamp


class QuantError(CybertradeError):
    pass


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def breakeven_winrate(payout: float) -> float:
    """Win rate where EV = 0: ``1 / (1 + payout)``."""
    if payout <= 0:
        raise QuantError("payout must be > 0")
    return 1.0 / (1.0 + payout)


def required_winrate(payout: float, min_edge: float) -> float:
    """Win rate needed for EV >= min_edge per unit stake."""
    if payout <= 0:
        raise QuantError("payout must be > 0")
    return (1.0 + min_edge) / (1.0 + payout)


def edge_of(p_win: float, payout: float) -> float:
    """EV per unit stake: ``p*(1+payout) - 1`` (negative = structural loss)."""
    p_win = clamp(p_win, 0.0, 1.0)
    return p_win * (1.0 + payout) - 1.0


def kelly_fraction_for(p_win: float, payout: float) -> float:
    """Kelly-optimal bankroll fraction for a binary payout.

    f* = (p*(1+b) - 1) / b   — clamped to >= 0 (never bet negative edge).
    """
    if payout <= 0:
        raise QuantError("payout must be > 0")
    p_win = clamp(p_win, 0.0, 1.0)
    return max(0.0, (p_win * (1.0 + payout) - 1.0) / payout)


def kelly_stake(
    p_win: float,
    payout: float,
    bankroll: float,
    fraction: float = 0.25,
    min_stake: float = 1.0,
    max_stake: float = 50.0,
) -> float:
    """Fractional-Kelly stake, bounded to the stake band. 0 when no edge."""
    if bankroll <= 0:
        return 0.0
    f = kelly_fraction_for(p_win, payout) * clamp(fraction, 0.0, 1.0)
    stake = f * bankroll
    if stake < min_stake and edge_of(p_win, payout) > 0:
        return float(min_stake)
    return float(clamp(stake, 0.0, max_stake))


def probability_itm(
    side: str,
    spot: float,
    strike: float,
    seconds: float,
    vol_per_bar: float,
    bars_per_second: float = 1.0 / 60.0,
    drift: float = 0.0,
) -> float:
    """Drifted-GBM probability the contract settles in the money.

    ``vol_per_bar`` is the stdev of one-bar log returns (same unit the regime
    detector reports); horizon bars = ``seconds * bars_per_second``.  This is
    the *market* probability — directional strategy conviction enters through
    ``drift`` (expected log-moves per bar) and, separately, through the
    :class:`~cybertrade.quant.calibration.CalibrationTracker`.
    """
    if side not in ("call", "put"):
        raise QuantError(f"side must be call or put, got {side!r}")
    if spot <= 0 or strike <= 0 or vol_per_bar < 0:
        raise QuantError("spot/strike must be > 0 and vol >= 0")
    bars = max(1e-9, seconds * bars_per_second)
    sigma = vol_per_bar * math.sqrt(bars)
    mu = drift * bars
    if sigma <= 0:
        win = 1.0 if ((side == "call" and spot > strike) or
                      (side == "put" and spot < strike)) else 0.0
        return win
    # P(S_T > K) for lognormal: Phi((ln(S/K) + mu) / sigma)
    z = (math.log(spot / strike) + mu) / sigma
    p_above = _norm_cdf(z)
    return clamp(p_above if side == "call" else 1.0 - p_above, 0.0, 1.0)


def expiry_edge_profile(
    side: str,
    spot: float,
    payout: float,
    expiries: Sequence[float],
    vol_per_bar: float,
    drift: float,
    bars_per_second: float = 1.0 / 60.0,
) -> List[Tuple[float, float, float]]:
    """(expiry, p_win, edge) across candidate expiries for an ATM contract.

    A directional forecast with mean-reverting half-life wants the expiry
    where the expected move is realized *before* reversion erases it — the
    drift term below encodes that conviction decay as ``drift * exp(-t/h)``.
    """
    out: List[Tuple[float, float, float]] = []
    for t in expiries:
        decay = math.exp(-max(t, 1.0) / 480.0)     # ~8-minute conviction half-life
        p = probability_itm(
            side, spot, spot, t, vol_per_bar,
            bars_per_second=bars_per_second, drift=drift * decay,
        )
        out.append((float(t), p, edge_of(p, payout)))
    return out


def best_expiry(
    side: str,
    spot: float,
    payout: float,
    expiries: Sequence[float],
    vol_per_bar: float,
    drift: float,
    default: Optional[float] = None,
) -> float:
    """Argmax edge across candidate expiries (``default`` on empty/no edge)."""
    profile = expiry_edge_profile(side, spot, payout, expiries, vol_per_bar, drift)
    if not profile:
        return default if default is not None else (expiries[0] if expiries else 60.0)
    best = max(profile, key=lambda row: row[2])
    return best[0] if best[2] > 0 else (default if default is not None else best[0])


__all__ = [
    "QuantError",
    "breakeven_winrate",
    "required_winrate",
    "edge_of",
    "kelly_fraction_for",
    "kelly_stake",
    "probability_itm",
    "expiry_edge_profile",
    "best_expiry",
]
