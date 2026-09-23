"""Volume-based indicators (volume often synthetic on OTC feeds — treat softly)."""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ..utils.mathx import safe_div
from .core import Out, Series, _check, ema, sma


def obv(close: Series, volume: Series) -> Out:
    """On-balance volume."""
    n = len(close)
    out: Out = [None] * n
    if n == 0:
        return out
    acc = 0.0
    out[0] = acc
    for i in range(1, n):
        v = volume[i] if i < len(volume) else 0.0
        if close[i] > close[i - 1]:
            acc += v
        elif close[i] < close[i - 1]:
            acc -= v
        out[i] = acc
    return out


def vwap(
    high: Series, low: Series, close: Series, volume: Series, reset_period: int = 0
) -> Out:
    """Volume-weighted average price (session-resetting when reset_period > 0)."""
    n = len(close)
    out: Out = [None] * n
    pv = 0.0
    vol = 0.0
    for i in range(n):
        if reset_period and i % reset_period == 0:
            pv = 0.0
            vol = 0.0
        tp = (high[i] + low[i] + close[i]) / 3.0
        v = volume[i] if i < len(volume) else 0.0
        pv += tp * v
        vol += v
        out[i] = safe_div(pv, vol, tp)
    return out


def chaikin_money_flow(
    high: Series,
    low: Series,
    close: Series,
    volume: Series,
    period: int = 20,
) -> Out:
    """Chaikin money flow (-1..1)."""
    n = len(close)
    out: Out = [None] * n
    for i in range(period - 1, n):
        mfv = 0.0
        vol = 0.0
        for j in range(i - period + 1, i + 1):
            rng = high[j] - low[j]
            mult = safe_div((close[j] - low[j]) - (high[j] - close[j]), rng, 0.0)
            v = volume[j] if j < len(volume) else 0.0
            mfv += mult * v
            vol += v
        out[i] = safe_div(mfv, vol, 0.0)
    return out


def accumulation_distribution(
    high: Series, low: Series, close: Series, volume: Series
) -> Out:
    """Accumulation/distribution line."""
    n = len(close)
    out: Out = [None] * n
    acc = 0.0
    for i in range(n):
        rng = high[i] - low[i]
        mult = safe_div((close[i] - low[i]) - (high[i] - close[i]), rng, 0.0)
        acc += mult * (volume[i] if i < len(volume) else 0.0)
        out[i] = acc
    return out


def force_index(
    close: Series, volume: Series, period: int = 13
) -> Out:
    """Ehlers force index (EMA of volume * change)."""
    n = len(close)
    raw = [0.0] * n
    for i in range(1, n):
        raw[i] = (close[i] - close[i - 1]) * (volume[i] if i < len(volume) else 0.0)
    return ema(raw, period)


def ease_of_movement(
    high: Series, low: Series, volume: Series, period: int = 14
) -> Out:
    """Ease of movement: distance moved per unit of volume."""
    n = len(high)
    emv = [0.0] * n
    for i in range(1, n):
        rng = high[i] - low[i]
        v = volume[i] if i < len(volume) else 0.0
        if rng > 0 and v > 0:
            mid = 0.5 * (high[i] + low[i])
            prev_mid = 0.5 * (high[i - 1] + low[i - 1])
            box = safe_div(v, 1e9, 0.0) if v > 1e6 else safe_div(v, 1.0, 0.0)
            br = safe_div(v, rng, 0.0) / 1e4
            emv[i] = (mid - prev_mid) / (br if br > 0 else 1.0)
    return sma(emv, period)


def volume_oscillator(volume: Series, fast: int = 5, slow: int = 10) -> Out:
    """Percent difference between fast and slow volume EMAs."""
    ef = ema(volume, fast)
    es = ema(volume, slow)
    out: Out = [None] * len(volume)
    for i in range(len(volume)):
        if ef[i] is not None and es[i]:
            out[i] = 100.0 * (ef[i] - es[i]) / es[i]
    return out


def volume_price_trend(close: Series, volume: Series) -> Out:
    """Volume-price trend line."""
    n = len(close)
    out: Out = [None] * n
    acc = 0.0
    for i in range(n):
        if i > 0 and close[i - 1] > 0:
            acc += safe_div(close[i] - close[i - 1], close[i - 1], 0.0) * (
                volume[i] if i < len(volume) else 0.0
            )
        out[i] = acc
    return out


def negative_volume_index(close: Series, volume: Series) -> Out:
    """NVI — moves only on falling volume (smart-money proxy)."""
    n = len(close)
    out: Out = [None] * n
    if n == 0:
        return out
    acc = 1000.0
    out[0] = acc
    for i in range(1, n):
        v = volume[i] if i < len(volume) else 0.0
        pv = volume[i - 1] if i - 1 < len(volume) else 0.0
        if v < pv and close[i - 1] > 0:
            acc *= close[i] / close[i - 1]
        out[i] = acc
    return out


def positive_volume_index(close: Series, volume: Series) -> Out:
    n = len(close)
    out: Out = [None] * n
    if n == 0:
        return out
    acc = 1000.0
    out[0] = acc
    for i in range(1, n):
        v = volume[i] if i < len(volume) else 0.0
        pv = volume[i - 1] if i - 1 < len(volume) else 0.0
        if v > pv and close[i - 1] > 0:
            acc *= close[i] / close[i - 1]
        out[i] = acc
    return out


def klinger_oscillator(
    high: Series,
    low: Series,
    close: Series,
    volume: Series,
    fast: int = 34,
    slow: int = 55,
    signal: int = 13,
) -> Tuple[Out, Out]:
    """Klinger volume oscillator and signal (simplified two-EMA form)."""
    n = len(close)
    dm = [0.0] * n
    trend = [1] * n
    vf = [0.0] * n
    for i in range(1, n):
        hlc = (high[i] + low[i] + close[i]) - (high[i - 1] + low[i - 1] + close[i - 1])
        dm[i] = high[i] - low[i]
        trend[i] = 1 if hlc > 0 else (-1 if hlc < 0 else trend[i - 1])
        v = volume[i] if i < len(volume) else 0.0
        cm = dm[i] + dm[i - 1] if i else dm[i]
        if cm == 0:
            cm = 1.0
        vf[i] = v * abs(2.0 * (dm[i] / cm - 1.0)) * trend[i] * 100.0
    ef = ema(vf, fast)
    es = ema(vf, slow)
    line: Out = [None] * n
    for i in range(n):
        if ef[i] is not None and es[i] is not None:
            line[i] = ef[i] - es[i]
    sig = _smooth(line, signal)
    return line, sig


def _smooth(series: Out, period: int) -> Out:
    n = len(series)
    out: Out = [None] * n
    for i in range(n):
        window = [v for v in series[max(0, i - period + 1) : i + 1] if v is not None]
        if len(window) == period:
            out[i] = sum(window) / period
    return out


__all__ = [
    "obv",
    "vwap",
    "chaikin_money_flow",
    "accumulation_distribution",
    "force_index",
    "ease_of_movement",
    "volume_oscillator",
    "volume_price_trend",
    "negative_volume_index",
    "positive_volume_index",
    "klinger_oscillator",
]
