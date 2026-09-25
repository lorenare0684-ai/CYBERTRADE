"""Trend-following indicators: ADX, SuperTrend, Ichimoku, PSAR, and friends."""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ..exceptions import IndicatorError
from ..utils.mathx import clamp, safe_div
from .core import Out, Series, _check, atr, ema, rma, sma, true_range_series


def adx(
    high: Series, low: Series, close: Series, period: int = 14
) -> Tuple[Out, Out, Out]:
    """Average directional index: (adx, plus_di, minus_di)."""
    _check(close, period, "adx")
    n = len(close)
    if n < period * 2 + 1:
        return [None] * n, [None] * n, [None] * n

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    tr = true_range_series(high, low, close)
    atr_s = rma(tr, period)
    plus_s = rma(plus_dm, period)
    minus_s = rma(minus_dm, period)

    plus_di: Out = [None] * n
    minus_di: Out = [None] * n
    dx: List[float] = [0.0] * n
    for i in range(n):
        if atr_s[i] is None:
            continue
        base = atr_s[i] or 0.0
        pdi = 100.0 * safe_div(plus_s[i] or 0.0, base, 0.0)
        mdi = 100.0 * safe_div(minus_s[i] or 0.0, base, 0.0)
        plus_di[i] = pdi
        minus_di[i] = mdi
        denom = pdi + mdi
        dx[i] = 100.0 * safe_div(abs(pdi - mdi), denom, 0.0)

    dx_series = dx[period:]
    adx_raw = rma(dx_series, period)
    adx_line: Out = [None] * n
    offset = period
    for i, v in enumerate(adx_raw):
        adx_line[i + offset] = v
    return adx_line, plus_di, minus_di


def supertrend(
    high: Series,
    low: Series,
    close: Series,
    period: int = 10,
    multiplier: float = 3.0,
) -> Tuple[Out, List[int]]:
    """SuperTrend: returns (line, direction) where direction +1/-1.

    Direction flips when close crosses the trailing band.
    """
    n = len(close)
    line: Out = [None] * n
    direction: List[int] = [0] * n
    a = atr(high, low, close, period)
    upper = [0.0] * n
    lower = [0.0] * n
    trend = 1
    for i in range(n):
        if a[i] is None:
            continue
        mid = 0.5 * (high[i] + low[i])
        up = mid + multiplier * a[i]
        dn = mid - multiplier * a[i]
        if i > 0 and a[i - 1] is not None:
            up = min(up, upper[i - 1]) if close[i - 1] <= upper[i - 1] else up
            dn = max(dn, lower[i - 1]) if close[i - 1] >= lower[i - 1] else dn
        upper[i] = up
        lower[i] = dn
        if close[i] > upper[i - 1] if (i > 0 and a[i - 1] is not None) else False:
            trend = 1
        if i > 0 and a[i - 1] is not None and close[i] < lower[i - 1]:
            trend = -1
        if i > 0 and trend == 1 and a[i - 1] is not None:
            trend = -1 if close[i] < lower[i] else 1
        elif i > 0 and trend == -1 and a[i - 1] is not None:
            trend = 1 if close[i] > upper[i] else -1
        if i == 0:
            trend = 1
        direction[i] = trend
        line[i] = lower[i] if trend == 1 else upper[i]
    return line, direction


def psar(
    high: Series,
    low: Series,
    af_start: float = 0.02,
    af_step: float = 0.02,
    af_max: float = 0.2,
) -> Out:
    """Parabolic stop and reverse."""
    n = len(high)
    out: Out = [None] * n
    if n < 2:
        return out
    bull = high[1] > high[0]
    sar = low[0] if bull else high[0]
    ep = high[0] if bull else low[0]
    af = af_start
    for i in range(1, n):
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)
        if bull:
            sar = min(sar, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < sar:
                bull = False
                sar = ep
                ep = low[i]
                af = af_start
            elif high[i] > ep:
                ep = high[i]
                af = min(af_max, af + af_step)
        else:
            sar = max(sar, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > sar:
                bull = True
                sar = ep
                ep = high[i]
                af = af_start
            elif low[i] < ep:
                ep = low[i]
                af = min(af_max, af + af_step)
        out[i] = sar
    return out


def ichimoku(
    high: Series,
    low: Series,
    close: Series,
    tenkan: int = 9,
    kijun: int = 26,
    senkou: int = 52,
) -> Tuple[Out, Out, Out, Out, Out]:
    """Ichimoku Kinko Hyo: (tenkan_sen, kijun_sen, span_a, span_b, chikou)."""
    n = len(close)

    def midpoint(period: int) -> Out:
        out: Out = [None] * n
        for i in range(period - 1, n):
            hi = max(high[i - period + 1 : i + 1])
            lo = min(low[i - period + 1 : i + 1])
            out[i] = 0.5 * (hi + lo)
        return out

    t = midpoint(tenkan)
    k = midpoint(kijun)
    span_a: Out = [None] * n
    span_b: Out = [None] * n
    for i in range(n):
        if t[i] is not None and k[i] is not None:
            span_a[i] = 0.5 * (t[i] + k[i])
    mid_b = midpoint(senkou)
    for i in range(n):
        if mid_b[i] is not None:
            span_b[i] = mid_b[i]
    chikou: Out = [None] * n
    for i in range(n):
        j = i - kijun
        if j >= 0:
            chikou[j] = close[i]
    return t, k, span_a, span_b, chikou


def linear_regression_channel(
    close: Series, period: int = 20, dev_mult: float = 2.0
) -> Tuple[Out, Out, Out]:
    """Least-squares regression channel: (mid, upper, lower)."""
    _check(close, period, "linear_regression_channel")
    n = len(close)
    mid: Out = [None] * n
    upper: Out = [None] * n
    lower: Out = [None] * n
    for i in range(period - 1, n):
        window = close[i - period + 1 : i + 1]
        m, b = _linreg(window)
        end_val = m * (period - 1) + b
        mid[i] = end_val
        resid = [window[j] - (m * j + b) for j in range(period)]
        sd = (sum(r * r for r in resid) / period) ** 0.5
        upper[i] = end_val + dev_mult * sd
        lower[i] = end_val - dev_mult * sd
    return mid, upper, lower


def linear_regression_slope(close: Series, period: int = 20) -> Out:
    """Slope of the least-squares fit — normalized per bar."""
    n = len(close)
    out: Out = [None] * n
    for i in range(period - 1, n):
        m, _ = _linreg(close[i - period + 1 : i + 1])
        out[i] = m
    return out


def _linreg(window: Sequence[float]) -> Tuple[float, float]:
    n = len(window)
    if n == 0:
        return 0.0, 0.0
    sx = (n - 1) * n / 2.0
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sy = sum(window)
    sxy = sum(j * window[j] for j in range(n))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return 0.0, sy / n
    m = (n * sxy - sx * sy) / denom
    b = (sy - m * sx) / n
    return m, b


def aroon(high: Series, low: Series, period: int = 25) -> Tuple[Out, Out]:
    """Aroon up/down (0..100)."""
    n = len(high)
    up: Out = [None] * n
    down: Out = [None] * n
    for i in range(period, n):
        window_h = high[i - period : i + 1]
        window_l = low[i - period : i + 1]
        hi_idx = max(range(len(window_h)), key=lambda j: window_h[j])
        lo_idx = max(range(len(window_l)), key=lambda j: -window_l[j])
        up[i] = 100.0 * (period - (period - hi_idx)) / period
        down[i] = 100.0 * (period - (period - lo_idx)) / period
    return up, down


def vortex(
    high: Series, low: Series, close: Series, period: int = 14
) -> Tuple[Out, Out]:
    """Vortex indicator: (vi_plus, vi_minus)."""
    n = len(close)
    if n < period + 1:
        return [None] * n, [None] * n
    tr = true_range_series(high, low, close)
    vp = [0.0] * n
    vm = [0.0] * n
    for i in range(1, n):
        vp[i] = abs(high[i] - low[i - 1])
        vm[i] = abs(low[i] - high[i - 1])
    plus: Out = [None] * n
    minus: Out = [None] * n
    for i in range(period, n):
        s_tr = sum(tr[i - period + 1 : i + 1])
        plus[i] = safe_div(sum(vp[i - period + 1 : i + 1]), s_tr, 0.0)
        minus[i] = safe_div(sum(vm[i - period + 1 : i + 1]), s_tr, 0.0)
    return plus, minus


def chande_momentum_oscillator(close: Series, period: int = 14) -> Out:
    """Chande momentum oscillator (smoothed momentum, -100..100)."""
    n = len(close)
    out: Out = [None] * n
    for i in range(period, n):
        up = 0.0
        down = 0.0
        for j in range(i - period + 1, i + 1):
            d = close[j] - close[j - 1]
            if d > 0:
                up += d
            else:
                down -= d
        out[i] = 100.0 * safe_div(up - down, up + down, 0.0)
    return out


def mass_index(high: Series, low: Series, period: int = 9, sum_period: int = 25) -> Out:
    """Mass index — reversal detector around 27."""
    n = len(high)
    rng = [high[i] - low[i] for i in range(n)]
    e1 = ema(rng, period)
    e2 = ema([v or 0.0 for v in e1], period)
    ratio = [
        (e1[i] / e2[i]) if (e1[i] is not None and e2[i]) else None for i in range(n)
    ]
    out: Out = [None] * n
    for i in range(sum_period - 1, n):
        window = [v for v in ratio[i - sum_period + 1 : i + 1] if v is not None]
        if len(window) == sum_period:
            out[i] = sum(window)
    return out


def trend_intensity_index(close: Series, period: int = 30) -> Out:
    """0..100 — how dominant the current drift is versus noise."""
    n = len(close)
    out: Out = [None] * n
    for i in range(period, n):
        window = close[i - period : i + 1]
        net = window[-1] - window[0]
        gross = sum(abs(window[j] - window[j - 1]) for j in range(1, len(window)))
        out[i] = 100.0 * safe_div(abs(net), gross, 0.0)
    return out


__all__ = [
    "adx",
    "supertrend",
    "psar",
    "ichimoku",
    "linear_regression_channel",
    "linear_regression_slope",
    "aroon",
    "vortex",
    "chande_momentum_oscillator",
    "mass_index",
    "trend_intensity_index",
]
