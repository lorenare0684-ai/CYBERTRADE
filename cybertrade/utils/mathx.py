"""Numeric helpers: safe math, rolling stats, and robust estimators.

All functions accept plain sequences of floats; warmup positions use ``None``
rather than NaN so downstream checks are honest (``None is not a number``).
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence

Number = Optional[float]


def clamp(value: float, low: float, high: float) -> float:
    """Clamp *value* into ``[low, high]`` (asserts a sane band)."""
    if low > high:
        low, high = high, low
    return max(low, min(high, value))


def nz(value: Number, default: float = 0.0) -> float:
    """Replace ``None``/NaN with *default*."""
    if value is None:
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    return float(value)


def is_num(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return not (isinstance(value, float) and math.isnan(value))
    return False


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    if b == 0.0 or math.isnan(b) or math.isnan(a):
        return default
    return a / b


def mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def variance(values: Sequence[float], ddof: int = 0) -> float:
    n = len(values)
    if n - ddof <= 0:
        return 0.0
    mu = mean(values)
    acc = 0.0
    for v in values:
        d = v - mu
        acc += d * d
    return acc / (n - ddof)


def stdev(values: Sequence[float], ddof: int = 0) -> float:
    return math.sqrt(variance(values, ddof=ddof))


def covariance(a: Sequence[float], b: Sequence[float], ddof: int = 0) -> float:
    n = min(len(a), len(b))
    if n - ddof <= 0:
        return 0.0
    aa = a[:n]
    bb = b[:n]
    ma = mean(aa)
    mb = mean(bb)
    acc = 0.0
    for x, y in zip(aa, bb):
        acc += (x - ma) * (y - mb)
    return acc / (n - ddof)


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    sa = stdev(a)
    sb = stdev(b)
    if sa == 0.0 or sb == 0.0:
        return 0.0
    return clamp(covariance(a, b) / (sa * sb), -1.0, 1.0)


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    n = len(data)
    mid = n // 2
    if n % 2:
        return data[mid]
    return 0.5 * (data[mid - 1] + data[mid])


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (pct in [0, 100])."""
    if not values:
        return 0.0
    data = sorted(values)
    if pct <= 0:
        return data[0]
    if pct >= 100:
        return data[-1]
    k = (len(data) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    return data[f] * (c - k) + data[c] * (k - f)


def ewma(values: Sequence[float], span: float) -> List[float]:
    """Exponentially weighted moving average over the full series."""
    if not values:
        return []
    if span <= 1:
        return [float(v) for v in values]
    alpha = 2.0 / (span + 1.0)
    out: List[float] = []
    prev = float(values[0])
    for v in values:
        prev = alpha * float(v) + (1.0 - alpha) * prev
        out.append(prev)
    return out


def rolling_apply(values: Sequence[float], window: int, fn) -> List[Number]:
    """Apply *fn* over a rolling window; ``None`` during warmup."""
    if window <= 0:
        raise ValueError("window must be > 0")
    out: List[Number] = [None] * len(values)
    for i in range(window - 1, len(values)):
        out[i] = fn(values[i - window + 1 : i + 1])
    return out


def max_drawdown(equity: Sequence[float]) -> float:
    """Maximum peak-to-trough fractional drawdown of an equity curve."""
    peak = -math.inf
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def argmax(values: Sequence[float]) -> int:
    best = 0
    for i, v in enumerate(values):
        if v > values[best]:
            best = i
    return best


def argmin(values: Sequence[float]) -> int:
    best = 0
    for i, v in enumerate(values):
        if v < values[best]:
            best = i
    return best


def sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def round_to(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    return round(round(value / tick) * tick, 10)


def almost_equal(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def sharpe(returns: Sequence[float], periods_per_year: float = 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    sd = stdev(returns, ddof=1)
    if sd == 0.0:
        return 0.0
    return mean(returns) / sd * math.sqrt(periods_per_year)


def sortino(returns: Sequence[float], periods_per_year: float = 252.0) -> float:
    downside = [min(0.0, r) for r in returns]
    ds = stdev(downside, ddof=1)
    if ds == 0.0:
        return 0.0
    return mean(returns) / ds * math.sqrt(periods_per_year)


def profit_factor(gross_win: float, gross_loss: float) -> float:
    if gross_loss <= 0.0:
        return math.inf if gross_win > 0 else 0.0
    return gross_win / gross_loss


def expectancy(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Expected value per attempt given win rate and average win/loss magnitudes."""
    return win_rate * avg_win - (1.0 - win_rate) * avg_loss


def kelly_fraction(win_rate: float, payout: float) -> float:
    """Kelly criterion for a fixed-payout binary bet.

    f* = (p * (1 + b) - 1) / b  where b is the net fractional payout.
    Clamped to [0, 1]; returns 0 when the edge is absent.
    """
    b = max(0.0, float(payout))
    p = clamp(win_rate, 0.0, 1.0)
    if b <= 0.0:
        return 0.0
    f = (p * (1.0 + b) - 1.0) / b
    return clamp(f, 0.0, 1.0)


def ema_series(values: Iterable[float], alpha: float) -> List[float]:
    out: List[float] = []
    prev: Optional[float] = None
    for v in values:
        if prev is None:
            prev = float(v)
        else:
            prev = alpha * float(v) + (1.0 - alpha) * prev
        out.append(prev)
    return out


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> List[float]:
    """True range series (first bar = high-low)."""
    n = len(close)
    out: List[float] = []
    for i in range(n):
        if i == 0:
            out.append(float(high[i]) - float(low[i]))
            continue
        prev_close = float(close[i - 1])
        tr = max(
            float(high[i]) - float(low[i]),
            abs(float(high[i]) - prev_close),
            abs(float(low[i]) - prev_close),
        )
        out.append(tr)
    return out


__all__ = [
    "clamp",
    "nz",
    "is_num",
    "safe_div",
    "mean",
    "variance",
    "stdev",
    "covariance",
    "correlation",
    "median",
    "percentile",
    "ewma",
    "rolling_apply",
    "max_drawdown",
    "argmax",
    "argmin",
    "sign",
    "round_to",
    "almost_equal",
    "sharpe",
    "sortino",
    "profit_factor",
    "expectancy",
    "kelly_fraction",
    "ema_series",
    "true_range",
]
