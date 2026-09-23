"""Volatility indicators and regime-sensitive dispersion measures."""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from ..utils.mathx import percentile, safe_div, stdev
from .core import Out, Series, _check, atr, ema, rma, sma, true_range_series


def bollinger_bands(
    close: Series, period: int = 20, dev_mult: float = 2.0
) -> Tuple[Out, Out, Out, Out]:
    """Bollinger bands: (upper, middle, lower, %b)."""
    n = len(close)
    mid = sma(close, period)
    upper: Out = [None] * n
    lower: Out = [None] * n
    pct_b: Out = [None] * n
    for i in range(n):
        if mid[i] is None:
            continue
        window = close[i - period + 1 : i + 1]
        sd = stdev(window, ddof=1) if period > 1 else 0.0
        upper[i] = (mid[i] or 0.0) + dev_mult * sd
        lower[i] = (mid[i] or 0.0) - dev_mult * sd
        width = upper[i] - lower[i]
        pct_b[i] = safe_div(close[i] - lower[i], width, 0.5)
    return upper, mid, lower, pct_b


def bandwidth(close: Series, period: int = 20, dev_mult: float = 2.0) -> Out:
    """Bollinger bandwidth as a percent of the middle band (squeeze detector)."""
    upper, mid, lower, _ = bollinger_bands(close, period, dev_mult)
    out: Out = [None] * len(close)
    for i in range(len(close)):
        if upper[i] is not None and mid[i]:
            out[i] = 100.0 * ((upper[i] or 0.0) - (lower[i] or 0.0)) / mid[i]
    return out


def keltner_width(
    high: Series,
    low: Series,
    close: Series,
    period: int = 20,
    atr_period: int = 10,
    mult: float = 2.0,
) -> Out:
    """Keltner channel width (absolute)."""
    a = atr(high, low, close, atr_period)
    return [None if v is None else 2.0 * mult * v for v in a]


def squeeze_momentum(
    high: Series,
    low: Series,
    close: Series,
    bb_period: int = 20,
    bb_dev: float = 2.0,
    kc_period: int = 20,
    kc_mult: float = 1.5,
) -> Tuple[Out, Out]:
    """LazyBear squeeze: (squeeze_on, momentum_hist).

    squeeze_on is 1 when BB is inside KC (compression), 0 when fired.
    """
    n = len(close)
    bb_u, bb_m, bb_l, _ = bollinger_bands(close, bb_period, bb_dev)
    kc_a = atr(high, low, close, kc_period)
    kc_m = sma(close, kc_period)
    squeeze_on: Out = [None] * n
    mom: Out = [None] * n
    for i in range(n):
        if bb_u[i] is None or kc_m[i] is None or kc_a[i] is None:
            continue
        kc_u = (kc_m[i] or 0.0) + kc_mult * (kc_a[i] or 0.0)
        kc_l = (kc_m[i] or 0.0) - kc_mult * (kc_a[i] or 0.0)
        squeeze_on[i] = 1.0 if ((bb_l[i] or 0.0) > kc_l and (bb_u[i] or 0.0) < kc_u) else 0.0
        hi = max(high[i - bb_period + 1 : i + 1]) if i >= bb_period - 1 else high[i]
        lo = min(low[i - bb_period + 1 : i + 1]) if i >= bb_period - 1 else low[i]
        mom[i] = close[i] - 0.5 * (hi + lo)
    return squeeze_on, mom


def historical_volatility(close: Series, period: int = 20, annualize: float = 252.0) -> Out:
    """Stdev of log returns, annualized (percent)."""
    n = len(close)
    out: Out = [None] * n
    rets = [0.0] + [
        math.log(close[i] / close[i - 1]) if close[i - 1] and close[i] > 0 else 0.0
        for i in range(1, n)
    ]
    for i in range(period, n):
        out[i] = stdev(rets[i - period + 1 : i + 1], ddof=1) * math.sqrt(annualize) * 100.0
    return out


def ewma_volatility(close: Series, span: int = 20, annualize: float = 252.0) -> Out:
    """Risk-metrics EWMA volatility (lambda from span)."""
    n = len(close)
    if n < 2:
        return [None] * n
    lam = (span - 1.0) / (span + 1.0)
    rets = [
        math.log(close[i] / close[i - 1]) if close[i - 1] and close[i] > 0 else 0.0
        for i in range(1, n)
    ]
    var = rets[0] ** 2
    out: Out = [None] * n
    out[1] = math.sqrt(var * annualize) * 100.0
    for i, r in enumerate(rets[1:], start=2):
        var = lam * var + (1.0 - lam) * r * r
        out[i] = math.sqrt(var * annualize) * 100.0
    return out


def garch_11(close: Series, omega: float = 0.000001, alpha: float = 0.08, beta: float = 0.90) -> Out:
    """GARCH(1,1) one-step variance forecast (in return space).

    Conservative fixed parameters; good enough for relative vol ranking.
    """
    n = len(close)
    if n < 2:
        return [None] * n
    rets = [
        math.log(close[i] / close[i - 1]) if close[i - 1] and close[i] > 0 else 0.0
        for i in range(1, n)
    ]
    var = max(omega / max(1e-12, 1.0 - alpha - beta), 1e-12)
    out: Out = [None] * n
    out[1] = math.sqrt(var)
    for i, r in enumerate(rets[1:], start=2):
        var = omega + alpha * r * r + beta * var
        out[i] = math.sqrt(max(var, 1e-18))
    return out


def chaikin_volatility(high: Series, low: Series, period: int = 10, roc_period: int = 10) -> Out:
    """Chaikin volatility: ROC of the H-L range EMA."""
    n = len(high)
    hl = [high[i] - low[i] for i in range(n)]
    e = ema(hl, period)
    out: Out = [None] * n
    for i in range(roc_period, n):
        a = e[i]
        b = e[i - roc_period]
        if a is not None and b:
            out[i] = 100.0 * (a - b) / b
    return out


def ulcer_index(close: Series, period: int = 14) -> Out:
    """Ulcer index — drawdown-severity volatility."""
    n = len(close)
    out: Out = [None] * n
    for i in range(period - 1, n):
        window = close[i - period + 1 : i + 1]
        peak = -math.inf
        dd2_sum = 0.0
        for v in window:
            peak = max(peak, v)
            dd = 100.0 * safe_div(v - peak, peak, 0.0) if peak > 0 else 0.0
            dd2_sum += dd * dd
        out[i] = math.sqrt(dd2_sum / period)
    return out


def realized_range_volatility(
    high: Series, low: Series, close: Series, period: int = 20, annualize: float = 252.0
) -> Out:
    """Parkinson range-based volatility estimator (percent, annualized)."""
    n = len(close)
    out: Out = [None] * n
    factor = 1.0 / (4.0 * math.log(2.0))
    for i in range(period, n):
        acc = 0.0
        cnt = 0
        for j in range(i - period + 1, i + 1):
            if low[j] > 0 and high[j] > 0:
                acc += factor * math.log(high[j] / low[j]) ** 2
                cnt += 1
        if cnt:
            out[i] = math.sqrt(acc / cnt * annualize) * 100.0
    return out


def vol_percentile(close: Series, period: int = 20, lookback: int = 252) -> Out:
    """Where current stdev-of-returns sits in its own year-long distribution."""
    n = len(close)
    out: Out = [None] * n
    rets = [0.0] + [
        abs(math.log(close[i] / close[i - 1])) if close[i - 1] and close[i] > 0 else 0.0
        for i in range(1, n)
    ]
    for i in range(period, n):
        current = stdev(rets[i - period + 1 : i + 1], ddof=1) if period > 1 else 0.0
        history = rets[max(0, i - lookback) : i]
        if len(history) >= period:
            out[i] = percentile(
                [stdev(history[j - period + 1 : j + 1], ddof=1) for j in range(period - 1, len(history))],
                50.0,
            )
            # rank current within rolling stdev distribution
            dist = [
                stdev(history[j - period + 1 : j + 1], ddof=1)
                for j in range(period - 1, len(history))
            ]
            below = sum(1 for v in dist if v <= current)
            out[i] = 100.0 * below / len(dist) if dist else 50.0
    return out


def intraday_intensity_index(
    high: Series, low: Series, close: Series, volume: Series, period: int = 14
) -> Out:
    """Close location vs range, volume-weighted and smoothed."""
    n = len(close)
    ii = [0.0] * n
    for i in range(n):
        rng = high[i] - low[i]
        v = volume[i] if i < len(volume) else 0.0
        if rng > 0:
            ii[i] = v * ((2.0 * close[i] - high[i] - low[i]) / rng)
    out: Out = [None] * n
    for i in range(period - 1, n):
        out[i] = sum(ii[i - period + 1 : i + 1])
    return out


__all__ = [
    "bollinger_bands",
    "bandwidth",
    "keltner_width",
    "squeeze_momentum",
    "historical_volatility",
    "ewma_volatility",
    "garch_11",
    "chaikin_volatility",
    "ulcer_index",
    "realized_range_volatility",
    "vol_percentile",
    "intraday_intensity_index",
]
