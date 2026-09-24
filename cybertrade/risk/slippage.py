"""Condition-aware fill slippage — storms and thin tapes widen the entry.

Expected slip is computed once per candidate trade (stress × session
liquidity) and flows one way: the survivor vetoes entries whose expected
slip exceeds ``RiskConfig.max_slippage_bps`` (a limit that was previously
declared but never enforced).

Heuristic magnitudes (documented, venue-independent): calm thick tape stays
at the configured base; stress scales up to ~40bps at the crisis ceiling;
thin session adds 6bps; hard cap 75bps — worse than that is not a fill you
pretend took.
"""
from __future__ import annotations

STRESS_SLIP_CEILING_BPS = 40.0   # at stress == 1.0
STRESS_SLIP_FLOOR = 0.5          # below this, stress adds nothing
THIN_SESSION_BPS = 6.0
NORMAL_SESSION_BPS = 2.0
CAP_BPS = 75.0


def expected_slippage_bps(
    *,
    stress: float = 0.0,
    session_liquidity: str = "thick",
    base: float = 0.5,
    cap: float = CAP_BPS,
) -> float:
    """Expected adverse entry slippage in basis points for one trade."""
    bps = max(0.0, float(base))
    if stress > STRESS_SLIP_FLOOR:
        over = (min(1.0, stress) - STRESS_SLIP_FLOOR) / (1.0 - STRESS_SLIP_FLOOR)
        bps = max(bps, over * STRESS_SLIP_CEILING_BPS)
    if session_liquidity == "thin":
        bps = max(bps, THIN_SESSION_BPS)
    elif session_liquidity == "normal":
        bps = max(bps, NORMAL_SESSION_BPS)
    return min(bps, cap)


__all__ = ["expected_slippage_bps", "CAP_BPS", "STRESS_SLIP_CEILING_BPS"]
