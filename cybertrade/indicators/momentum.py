"""Momentum and oscillator indicators."""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from ..utils.mathx import safe_div
from .core import Out, Series, _check, ema, rma, sma


def stochastic(
    high: Series,
    low: Series,
    close: Series,
    k_period: int = 14,
    k_smooth: int = 3,
    d_period: int = 3,
) -> Tuple[Out, Out]:
    """Stochastic oscillator: (%K smoothed, %D)."""
    n = len(close)
    fast_k: Out = [None] * n
    for i in range(k_period - 1, n):
        hh = max(high[i - k_period + 1 : i + 1])
        ll = min(low[i - k_period + 1 : i + 1])
        fast_k[i] = 100.0 * safe_div(close[i] - ll, hh - ll, 50.0)
    k_line = _roll_mean_opt(fast_k, k_smooth)
    d_line = _roll_mean_opt(k_line, d_period)
    return k_line, d_line


def cci(high: Series, low: Series, close: Series, period: int = 20) -> Out:
    """Commodity channel index."""
    n = len(close)
    out: Out = [None] * n
    tp = [(high[i] + low[i] + close[i]) / 3.0 for i in range(n)]
    ma = sma(tp, period)
    for i in range(n):
        if ma[i] is None:
            continue
        window = tp[i - period + 1 : i + 1]
        md = sum(abs(v - (ma[i] or 0.0)) for v in window) / period
        out[i] = safe_div(tp[i] - (ma[i] or 0.0), 0.015 * md, 0.0)
    return out


def mfi(
    high: Series,
    low: Series,
    close: Series,
    volume: Series,
    period: int = 14,
) -> Out:
    """Money flow index."""
    n = len(close)
    out: Out = [None] * n
    tp = [(high[i] + low[i] + close[i]) / 3.0 for i in range(n)]
    for i in range(period, n):
        pos = 0.0
        neg = 0.0
        for j in range(i - period + 1, i + 1):
            flow = tp[j] * (volume[j] if j < len(volume) else 0.0)
            if tp[j] > tp[j - 1]:
                pos += flow
            elif tp[j] < tp[j - 1]:
                neg += flow
        if neg == 0:
            out[i] = 100.0 if pos > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + pos / neg)
    return out


def williams_r(high: Series, low: Series, close: Series, period: int = 14) -> Out:
    """Williams %R (negative -100..0)."""
    n = len(close)
    out: Out = [None] * n
    for i in range(period - 1, n):
        hh = max(high[i - period + 1 : i + 1])
        ll = min(low[i - period + 1 : i + 1])
        out[i] = -100.0 * safe_div(hh - close[i], hh - ll, 0.0)
    return out


def ultimate_oscillator(
    high: Series,
    low: Series,
    close: Series,
    p1: int = 7,
    p2: int = 14,
    p3: int = 28,
) -> Out:
    """Larry Williams' ultimate oscillator (0..100)."""
    n = len(close)
    out: Out = [None] * n
    if n < 2:
        return out
    bp = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        pc = close[i - 1]
        bp[i] = close[i] - min(low[i], pc)
        tr[i] = max(high[i], pc) - min(low[i], pc)

    def _avg(period: int, end: int) -> Tuple[float, float]:
        s_bp = sum(bp[end - period + 1 : end + 1])
        s_tr = sum(tr[end - period + 1 : end + 1])
        return s_bp, s_tr

    for i in range(p3, n):
        b1, t1 = _avg(p1, i)
        b2, t2 = _avg(p2, i)
        b3, t3 = _avg(p3, i)
        a1 = safe_div(b1, t1, 0.0)
        a2 = safe_div(b2, t2, 0.0)
        a3 = safe_div(b3, t3, 0.0)
        out[i] = 100.0 * (4.0 * a1 + 2.0 * a2 + a3) / 7.0
    return out


def awesome_oscillator(
    high: Series, low: Series, fast: int = 5, slow: int = 34
) -> Out:
    """Awesome oscillator: SMA(median, fast) - SMA(median, slow)."""
    n = len(high)
    median = [(high[i] + low[i]) / 2.0 for i in range(n)]
    f = sma(median, fast)
    s = sma(median, slow)
    out: Out = [None] * n
    for i in range(n):
        if f[i] is not None and s[i] is not None:
            out[i] = f[i] - s[i]
    return out


def accel_decel_oscillator(
    high: Series, low: Series, fast: int = 5, slow: int = 34, signal: int = 5
) -> Tuple[Out, Out]:
    """Accelerator/decelerator: (ao - sma(ao, signal), ao)."""
    ao = awesome_oscillator(high, low, fast, slow)
    ao_s = _roll_mean_opt(ao, signal)
    out: Out = [None] * len(high)
    for i in range(len(high)):
        if ao[i] is not None and ao_s[i] is not None:
            out[i] = ao[i] - ao_s[i]
    return out, ao


def detrended_price_oscillator(close: Series, period: int = 20) -> Out:
    """DPO — price vs displaced SMA."""
    n = len(close)
    out: Out = [None] * n
    shift = period // 2 + 1
    ma = sma(close, period)
    for i in range(n):
        j = i - shift
        if j >= 0 and ma[j] is not None:
            out[i] = close[i] - (ma[j] or 0.0)
    return out


def relative_vigor_index(
    high: Series, low: Series, open_: Series, close: Series, period: int = 10
) -> Tuple[Out, Out]:
    """RVI and signal line."""
    n = len(close)
    if n < period + 3:
        return [None] * n, [None] * n
    num = [0.0] * n
    den = [0.0] * n
    for i in range(n):
        num[i] = (close[i] - open_[i]) + 2.0 * (close[i - 1] if i else close[i] - open_[i])
        den[i] = (high[i] - low[i]) + 2.0 * (
            (high[i - 1] - low[i - 1]) if i else (high[i] - low[i])
        )
    rvi_num = _roll_sum(num, period)
    rvi_den = _roll_sum(den, period)
    rvi: Out = [None] * n
    for i in range(n):
        if rvi_num[i] is not None and rvi_den[i]:
            rvi[i] = safe_div(rvi_num[i], rvi_den[i], 0.0)
    sig_vals = [
        (nz(rvi[i]) + 2 * nz(rvi[i - 1] if i else 0) + 2 * nz(rvi[i - 2] if i > 1 else 0) + nz(rvi[i - 3] if i > 2 else 0)) / 6.0
        if rvi[i] is not None
        else None
        for i in range(n)
    ]
    return rvi, sig_vals


def nz(v: Optional[float], default: float = 0.0) -> float:
    return default if v is None else v


def elder_ray(
    high: Series,
    low: Series,
    close: Series,
    period: int = 13,
) -> Tuple[Out, Out, Out]:
    """Elder Ray: (bull power, bear power, ema trend)."""
    e = ema(close, period)
    bull: Out = [None] * len(close)
    bear: Out = [None] * len(close)
    for i in range(len(close)):
        if e[i] is not None:
            bull[i] = high[i] - e[i]
            bear[i] = low[i] - e[i]
    return bull, bear, e


def psychological_line(close: Series, period: int = 12) -> Out:
    """Share of up bars in the window (0..1)."""
    n = len(close)
    out: Out = [None] * n
    for i in range(period, n):
        ups = sum(
            1 for j in range(i - period + 1, i + 1) if close[j] > close[j - 1]
        )
        out[i] = ups / period
    return out


def _roll_mean_opt(series: Out, period: int) -> Out:
    n = len(series)
    out: Out = [None] * n
    if period <= 1:
        return list(series)
    for i in range(n):
        window = [v for v in series[max(0, i - period + 1) : i + 1] if v is not None]
        if len(window) == period:
            out[i] = sum(window) / period
    return out


def _roll_sum(series: Sequence[float], period: int) -> Out:
    n = len(series)
    out: Out = [None] * n
    for i in range(period - 1, n):
        out[i] = sum(series[i - period + 1 : i + 1])
    return out


__all__ = [
    "stochastic",
    "cci",
    "mfi",
    "williams_r",
    "ultimate_oscillator",
    "awesome_oscillator",
    "accel_decel_oscillator",
    "detrended_price_oscillator",
    "relative_vigor_index",
    "elder_ray",
    "psychological_line",
]
