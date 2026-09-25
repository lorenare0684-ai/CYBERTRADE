"""Core indicators: moving averages, oscillators, ranges, and ATR families.

Series convention
-----------------
Every function accepts a ``Sequence[float]`` and returns
``List[Optional[float]]`` where warmup slots are ``None``.  Multi-line
indicators return tuples of lists in documented order.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from ..exceptions import IndicatorError
from ..utils.mathx import clamp, nz, rolling_apply, safe_div, true_range

Series = Sequence[float]
Out = List[Optional[float]]


def _check(series: Series, period: int, name: str) -> None:
    if period < 1:
        raise IndicatorError(f"{name}: period must be >= 1")


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def sma(series: Series, period: int) -> Out:
    """Simple moving average."""
    _check(series, period, "sma")
    n = len(series)
    out: Out = [None] * n
    if n < period:
        return out
    acc = sum(series[:period])
    out[period - 1] = acc / period
    for i in range(period, n):
        acc += series[i] - series[i - period]
        out[i] = acc / period
    return out


def wma(series: Series, period: int) -> Out:
    """Linearly weighted moving average (newest bar weighs the most)."""
    _check(series, period, "wma")
    n = len(series)
    out: Out = [None] * n
    denom = period * (period + 1) / 2.0
    for i in range(period - 1, n):
        acc = 0.0
        wsum = 0.0
        for j in range(period):
            w = j + 1
            acc += w * series[i - period + 1 + j]
            wsum += w
        out[i] = acc / (wsum or denom)
    return out


def ema(series: Series, period: int) -> Out:
    """Exponential moving average (seeded with the first SMA)."""
    _check(series, period, "ema")
    n = len(series)
    out: Out = [None] * n
    if n < period:
        return out
    seed = sum(series[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, n):
        prev = alpha * series[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def rma(series: Series, period: int) -> Out:
    """Wilder's smoothing (running moving average), used by RSI/ATR/ADX."""
    _check(series, period, "rma")
    n = len(series)
    out: Out = [None] * n
    if n < period:
        return out
    seed = sum(series[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + series[i]) / period
        out[i] = prev
    return out


def smma(series: Series, period: int) -> Out:
    """Alias of :func:`rma` (smoothed MA)."""
    return rma(series, period)


def dema(series: Series, period: int) -> Out:
    """Double exponential moving average."""
    e1 = ema(series, period)
    clean1 = [nz(v) for v in e1]
    e2 = ema(clean1, period)
    out: Out = [None] * len(series)
    for i, (a, b) in enumerate(zip(e1, e2)):
        if a is not None and b is not None:
            out[i] = 2.0 * a - b
    return out


def tema(series: Series, period: int) -> Out:
    """Triple exponential moving average."""
    e1 = ema(series, period)
    e2 = ema([nz(v) for v in e1], period)
    e3 = ema([nz(v) for v in e2], period)
    out: Out = [None] * len(series)
    for i, (a, b, c) in enumerate(zip(e1, e2, e3)):
        if a is not None and b is not None and c is not None:
            out[i] = 3.0 * a - 3.0 * b + c
    return out


def hma(series: Series, period: int) -> Out:
    """Hull moving average — low-lag, smooth."""
    if period < 2:
        raise IndicatorError("hma: period must be >= 2")
    half = max(1, period // 2)
    w1 = wma(series, half)
    w2 = wma(series, period)
    raw = [nz(a) * 2.0 - nz(b) for a, b in zip(w1, w2)]
    root = max(1, int(round(math.sqrt(period))))
    return wma(raw, root)


def vwma(close: Series, volume: Series, period: int) -> Out:
    """Volume-weighted moving average."""
    _check(close, period, "vwma")
    n = len(close)
    if len(volume) < n:
        raise IndicatorError("vwma: volume shorter than close")
    out: Out = [None] * n
    for i in range(period - 1, n):
        pv = 0.0
        vol = 0.0
        for j in range(i - period + 1, i + 1):
            pv += close[j] * volume[j]
            vol += volume[j]
        out[i] = safe_div(pv, vol, close[i])
    return out


def alma(series: Series, period: int, offset: float = 0.85, sigma: float = 6.0) -> Out:
    """Arnaud Legoux moving average."""
    _check(series, period, "alma")
    n = len(series)
    out: Out = [None] * n
    m = offset * (period - 1)
    s = period / sigma if sigma else 1.0
    weights = []
    denom = 0.0
    for j in range(period):
        w = math.exp(-((j - m) ** 2) / (2.0 * s * s))
        weights.append(w)
        denom += w
    if denom == 0:
        return sma(series, period)
    for i in range(period - 1, n):
        acc = 0.0
        for j in range(period):
            acc += weights[j] * series[i - period + 1 + j]
        out[i] = acc / denom
    return out


def kama(series: Series, period: int = 10, fast: int = 2, slow: int = 30) -> Out:
    """Kaufman adaptive moving average."""
    _check(series, period, "kama")
    n = len(series)
    out: Out = [None] * n
    if n <= period:
        return out
    fast_sc = 2.0 / (fast + 1.0)
    slow_sc = 2.0 / (slow + 1.0)
    prev = series[period - 1]
    out[period - 1] = prev
    for i in range(period, n):
        change = abs(series[i] - series[i - period])
        volatility = sum(abs(series[j] - series[j - 1]) for j in range(i - period + 1, i + 1))
        er = safe_div(change, volatility, 0.0)
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = prev + sc * (series[i] - prev)
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Momentum classics
# ---------------------------------------------------------------------------
def rsi(close: Series, period: int = 14) -> Out:
    """Relative strength index (Wilder)."""
    _check(close, period, "rsi")
    n = len(close)
    out: Out = [None] * n
    if n <= period:
        return out
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, n):
        d = close[i] - close[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, n):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_gain == 0 and avg_loss == 0:
            out[i] = 50.0
        else:
            rs = safe_div(avg_gain, avg_loss, math.inf if avg_gain else 0.0)
            if math.isinf(rs):
                out[i] = 100.0
            else:
                out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def stoch_rsi(
    close: Series, period: int = 14, k: int = 3, d: int = 3
) -> Tuple[Out, Out]:
    """Stochastic of RSI: returns (%K smoothed, %D smoothed)."""
    r = rsi(close, period)
    n = len(r)
    k_raw: Out = [None] * n
    for i in range(n):
        window = [v for v in r[max(0, i - period + 1) : i + 1] if v is not None]
        if len(window) < period and i < period * 2 - 2:
            continue
        if not window:
            continue
        lo, hi = min(window), max(window)
        if i >= period * 2 - 2:
            k_raw[i] = 100.0 * safe_div(nz(r[i]) - lo, hi - lo, 0.0)
    k_sm = _sma_ignore_none(k_raw, k)
    d_sm = _sma_ignore_none(k_sm, d)
    return k_sm, d_sm


def macd(
    close: Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[Out, Out, Out]:
    """Moving average convergence/divergence: (macd, signal, histogram)."""
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    n = len(close)
    line: Out = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            line[i] = ema_fast[i] - ema_slow[i]
    sig = _ema_ignore_none(line, signal)
    hist: Out = [None] * n
    for i in range(n):
        if line[i] is not None and sig[i] is not None:
            hist[i] = line[i] - sig[i]
    return line, sig, hist


def ppo(close: Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[Out, Out, Out]:
    """Percentage price oscillator: (ppo, signal, hist)."""
    ef = ema(close, fast)
    es = ema(close, slow)
    n = len(close)
    line: Out = [None] * n
    for i in range(n):
        if ef[i] is not None and es[i] is not None and es[i] != 0:
            line[i] = 100.0 * (ef[i] - es[i]) / es[i]
    sig = _ema_ignore_none(line, signal)
    hist: Out = [None] * n
    for i in range(n):
        if line[i] is not None and sig[i] is not None:
            hist[i] = line[i] - sig[i]
    return line, sig, hist


def roc(close: Series, period: int = 12) -> Out:
    """Rate of change (percent)."""
    _check(close, period, "roc")
    n = len(close)
    out: Out = [None] * n
    for i in range(period, n):
        base = close[i - period]
        out[i] = 100.0 * safe_div(close[i] - base, base, 0.0)
    return out


def momentum(close: Series, period: int = 10) -> Out:
    _check(close, period, "momentum")
    n = len(close)
    out: Out = [None] * n
    for i in range(period, n):
        out[i] = close[i] - close[i - period]
    return out


def tsi(close: Series, long_period: int = 25, short_period: int = 13) -> Out:
    """True strength index (double-smoothed momentum)."""
    n = len(close)
    if n < 2:
        return [None] * n
    mom = [0.0] + [close[i] - close[i - 1] for i in range(1, n)]
    abs_mom = [abs(m) for m in mom]
    sm1 = _ema_ignore_none(mom, long_period)
    sm2 = _ema_ignore_none(sm1, short_period)
    as1 = _ema_ignore_none(abs_mom, long_period)
    as2 = _ema_ignore_none(as1, short_period)
    out: Out = [None] * n
    warm = long_period + short_period
    for i in range(n):
        if i >= warm and as2[i]:
            out[i] = 100.0 * (sm2[i] or 0.0) / as2[i]
    return out


# ---------------------------------------------------------------------------
# Ranges / channels / pivots
# ---------------------------------------------------------------------------
def donchian(
    high: Series, low: Series, period: int = 20
) -> Tuple[Out, Out, Out]:
    """Donchian channel: (upper, middle, lower)."""
    _check(high, period, "donchian")
    n = len(high)
    upper: Out = [None] * n
    lower: Out = [None] * n
    middle: Out = [None] * n
    for i in range(period - 1, n):
        hi = max(high[i - period + 1 : i + 1])
        lo = min(low[i - period + 1 : i + 1])
        upper[i] = hi
        lower[i] = lo
        middle[i] = 0.5 * (hi + lo)
    return upper, middle, lower


def keltner(
    high: Series,
    low: Series,
    close: Series,
    period: int = 20,
    atr_period: int = 10,
    mult: float = 2.0,
) -> Tuple[Out, Out, Out]:
    """Keltner channel: (upper, middle, lower)."""
    mid = ema(close, period)
    atr_vals = atr(high, low, close, atr_period)
    upper: Out = [None] * len(close)
    lower: Out = [None] * len(close)
    for i in range(len(close)):
        if mid[i] is not None and atr_vals[i] is not None:
            upper[i] = mid[i] + mult * atr_vals[i]
            lower[i] = mid[i] - mult * atr_vals[i]
    return upper, mid, lower


def pivot_points(
    prev_high: float, prev_low: float, prev_close: float
) -> Tuple[float, float, float, float, float, float, float]:
    """Classic floor pivots: (P, R1, R2, R3, S1, S2, S3)."""
    p = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2.0 * p - prev_low
    s1 = 2.0 * p - prev_high
    r2 = p + (prev_high - prev_low)
    s2 = p - (prev_high - prev_low)
    r3 = prev_high + 2.0 * (p - prev_low)
    s3 = prev_low - 2.0 * (prev_high - p)
    return p, r1, r2, r3, s1, s2, s3


# ---------------------------------------------------------------------------
# Volatility primitives
# ---------------------------------------------------------------------------
def true_range_series(high: Series, low: Series, close: Series) -> List[float]:
    return true_range(high, low, close)


def atr(high: Series, low: Series, close: Series, period: int = 14) -> Out:
    """Average true range (Wilder)."""
    _check(close, period, "atr")
    tr = true_range(high, low, close)
    return rma(tr, period)


def natr(high: Series, low: Series, close: Series, period: int = 14) -> Out:
    """Normalized ATR as a percentage of close."""
    a = atr(high, low, close, period)
    out: Out = [None] * len(close)
    for i in range(len(close)):
        if a[i] is not None and close[i]:
            out[i] = 100.0 * a[i] / close[i]
    return out


def volatility_ratio(high: Series, low: Series, close: Series,
                     fast: int = 5, slow: int = 20) -> Out:
    """Fast ATR / slow ATR — expansion > 1, contraction < 1."""
    a_fast = atr(high, low, close, fast)
    a_slow = atr(high, low, close, slow)
    out: Out = [None] * len(close)
    for i in range(len(close)):
        if a_fast[i] is not None and a_slow[i]:
            out[i] = a_fast[i] / a_slow[i]
    return out


# ---------------------------------------------------------------------------
# Helpers that keep ``None`` warmups intact
# ---------------------------------------------------------------------------
def _sma_ignore_none(series: Out, period: int) -> Out:
    values = [nz(v) for v in series]
    raw = sma(values, period)
    # extend warmup: everything before first defined input stays None
    first_defined = next((i for i, v in enumerate(series) if v is not None), len(series))
    out: Out = list(raw)
    for i in range(min(len(out), first_defined + period - 1)):
        out[i] = None
    return out


def _ema_ignore_none(series: Out, period: int) -> Out:
    values = [nz(v) for v in series]
    raw = ema(values, period)
    first_defined = next((i for i, v in enumerate(series) if v is not None), len(series))
    out: Out = list(raw)
    for i in range(min(len(out), first_defined + period - 1)):
        out[i] = None
    return out


def crossover(a: Out, b: Out) -> List[Optional[bool]]:
    """True where line *a* crosses above *b*, False below, None unknown."""
    n = min(len(a), len(b))
    out: List[Optional[bool]] = [None] * n
    for i in range(1, n):
        if None in (a[i], b[i], a[i - 1], b[i - 1]):
            continue
        if a[i] > b[i] and a[i - 1] <= b[i - 1]:
            out[i] = True
        elif a[i] < b[i] and a[i - 1] >= b[i - 1]:
            out[i] = False
        else:
            out[i] = out[i - 1] if out[i - 1] is not None else False
    return out


def last_defined(series: Out, default: Optional[float] = None) -> Optional[float]:
    for v in reversed(series):
        if v is not None:
            return v
    return default


__all__ = [
    "Series",
    "Out",
    "sma",
    "wma",
    "ema",
    "rma",
    "smma",
    "dema",
    "tema",
    "hma",
    "vwma",
    "alma",
    "kama",
    "rsi",
    "stoch_rsi",
    "macd",
    "ppo",
    "roc",
    "momentum",
    "tsi",
    "donchian",
    "keltner",
    "pivot_points",
    "true_range_series",
    "atr",
    "natr",
    "volatility_ratio",
    "crossover",
    "last_defined",
]
