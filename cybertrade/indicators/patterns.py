"""Candlestick pattern recognition.

Every detector returns ``List[Optional[int]]`` aligned to the input candles:
``None`` = warmup / undecided, 0 = no pattern, +1 = bullish event,
-1 = bearish event.  They operate on raw OHLC lists and never mutate inputs.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

Series = Sequence[float]
Marks = List[Optional[int]]

_BODY_MIN_FRAC = 0.1     # body must be >= 10% of the bar range to count
_WICK_MAX_FRAC = 0.08    # for a doji-like bar


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _range(h: float, l: float) -> float:
    return max(h - l, 1e-12)


def _upper_wick(o: float, h: float, c: float) -> float:
    return h - max(o, c)


def _lower_wick(o: float, l: float, c: float) -> float:
    return min(o, c) - l


def _align(n: int) -> Marks:
    return [None] * n


def doji(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Indecision doji: body smaller than 8% of range."""
    n = len(close)
    out = _align(n)
    for i in range(n):
        r = _range(high[i], low[i])
        if _body(open_[i], close[i]) <= _WICK_MAX_FRAC * r:
            out[i] = 0
        else:
            out[i] = 0
    # mark index so consumers know detection ran; direction 0 == neutral
    return [0 if v is not None else None for v in out]


def hammer(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Hammer / hanging man by context (bullish when after a decline).

    Geometry (range-relative so doji-sized bodies still count):
    long lower shadow (>= 55% of range), small body (<= 35%), tiny upper.
    """
    n = len(close)
    out = _align(n)
    for i in range(n):
        r = _range(high[i], low[i])
        body = _body(open_[i], close[i])
        lw = _lower_wick(open_[i], low[i], close[i])
        uw = _upper_wick(open_[i], high[i], close[i])
        if lw >= 0.55 * r and body <= 0.35 * r and uw <= 0.15 * r and lw >= 1.5 * max(body, 0.05 * r):
            ctx = 0
            if i >= 3:
                drift = close[i - 1] - close[i - 3]
                ctx = 1 if drift < 0 else -1
            out[i] = ctx or 1
        else:
            out[i] = 0
    return out


def engulfing(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Bullish/bearish engulfing."""
    n = len(close)
    out = _align(n)
    for i in range(1, n):
        po, pc = open_[i - 1], close[i - 1]
        o, c = open_[i], close[i]
        if c > o and pc < po and c >= po and o <= pc and _body(o, c) > _body(po, pc):
            out[i] = 1
        elif c < o and pc > po and c <= po and o >= pc and _body(o, c) > _body(po, pc):
            out[i] = -1
        else:
            out[i] = 0
    return out


def morning_star(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Morning star (bullish 3-bar reversal)."""
    n = len(close)
    out = _align(n)
    for i in range(2, n):
        o1, c1 = open_[i - 2], close[i - 2]
        o2, c2 = open_[i - 1], close[i - 1]
        o3, c3 = open_[i], close[i]
        if (
            c1 < o1
            and _body(o2, c2) < 0.35 * _body(o1, c1)
            and c3 > o3
            and c3 > (o1 + c1) / 2.0
        ):
            out[i] = 1
        else:
            out[i] = 0
    return out


def evening_star(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Evening star (bearish 3-bar reversal)."""
    n = len(close)
    out = _align(n)
    for i in range(2, n):
        o1, c1 = open_[i - 2], close[i - 2]
        o2, c2 = open_[i - 1], close[i - 1]
        o3, c3 = open_[i], close[i]
        if (
            c1 > o1
            and _body(o2, c2) < 0.35 * _body(o1, c1)
            and c3 < o3
            and c3 < (o1 + c1) / 2.0
        ):
            out[i] = -1
        else:
            out[i] = 0
    return out


def three_soldiers(
    open_: Series, high: Series, low: Series, close: Series
) -> Marks:
    """Three white soldiers / three black crows."""
    n = len(close)
    out = _align(n)
    for i in range(2, n):
        ups = all(close[j] > open_[j] for j in range(i - 2, i + 1))
        downs = all(close[j] < open_[j] for j in range(i - 2, i + 1))
        ascending = close[i] > close[i - 1] > close[i - 2]
        descending = close[i] < close[i - 1] < close[i - 2]
        if ups and ascending:
            out[i] = 1
        elif downs and descending:
            out[i] = -1
        else:
            out[i] = 0
    return out


def pin_bar(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Long-tail pin/rejection bar (barrier-hunt favourite)."""
    n = len(close)
    out = _align(n)
    for i in range(n):
        r = _range(high[i], low[i])
        body = _body(open_[i], close[i])
        lw = _lower_wick(open_[i], low[i], close[i])
        uw = _upper_wick(open_[i], high[i], close[i])
        if lw >= 0.6 * r and body <= 0.3 * r:
            out[i] = 1
        elif uw >= 0.6 * r and body <= 0.3 * r:
            out[i] = -1
        else:
            out[i] = 0
    return out


def marubozu(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Full-body domination bar (conviction)."""
    n = len(close)
    out = _align(n)
    for i in range(n):
        r = _range(high[i], low[i])
        body = _body(open_[i], close[i])
        if body >= 0.9 * r:
            out[i] = 1 if close[i] > open_[i] else -1
        else:
            out[i] = 0
    return out


def inside_bar(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Inside bar (compression / coiling)."""
    n = len(close)
    out = _align(n)
    for i in range(1, n):
        if high[i] <= high[i - 1] and low[i] >= low[i - 1]:
            out[i] = 0
        else:
            out[i] = 0
    return out


def outside_bar(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Outside bar (expansion)."""
    n = len(close)
    out = _align(n)
    for i in range(1, n):
        if high[i] >= high[i - 1] and low[i] <= low[i - 1] and _range(high[i], low[i]) > _range(high[i - 1], low[i - 1]):
            out[i] = 1 if close[i] > open_[i] else -1
        else:
            out[i] = 0
    return out


def tweezer(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Tweezer top/bottom (matched highs/lows)."""
    n = len(close)
    out = _align(n)
    tol = 1e-9
    for i in range(1, n):
        r = max(_range(high[i], low[i]), _range(high[i - 1], low[i - 1]))
        if abs(high[i] - high[i - 1]) <= 0.05 * r and close[i - 1] > open_[i - 1] and close[i] < open_[i]:
            out[i] = -1
        elif abs(low[i] - low[i - 1]) <= 0.05 * r and close[i - 1] < open_[i - 1] and close[i] > open_[i]:
            out[i] = 1
        else:
            out[i] = 0
    return out


def harami(open_: Series, high: Series, low: Series, close: Series) -> Marks:
    """Harami (pregnant bar — potential reversal)."""
    n = len(close)
    out = _align(n)
    for i in range(1, n):
        po, pc = open_[i - 1], close[i - 1]
        o, c = open_[i], close[i]
        parent = _body(po, pc)
        child = _body(o, c)
        if parent <= 0 or child >= 0.6 * parent:
            out[i] = 0
            continue
        inside = min(o, c) >= min(po, pc) and max(o, c) <= max(po, pc)
        if not inside:
            out[i] = 0
        elif pc < po and c > o:
            out[i] = 1
        elif pc > po and c < o:
            out[i] = -1
        else:
            out[i] = 0
    return out


def score_patterns(
    open_: Series,
    high: Series,
    low: Series,
    close: Series,
    detectors: Optional[Sequence[Tuple[str, object]]] = None,
) -> Tuple[List[float], List[dict]]:
    """Blend all detectors into a per-bar score in [-1, 1] plus detail dicts."""
    if detectors is None:
        detectors = all_detectors()
    n = len(close)
    scores = [0.0] * n
    details: List[dict] = [{} for _ in range(n)]
    count = 0.0
    for name, fn in detectors:
        marks = fn(open_, high, low, close)
        for i, m in enumerate(marks):
            if m is None:
                continue
            if m != 0:
                scores[i] += m
                details[i][name] = m
        count = max(count, 1.0)
    total = max(1, len(detectors))
    return [s / total for s in scores], details


def all_detectors() -> List[Tuple[str, object]]:
    return [
        ("hammer", hammer),
        ("engulfing", engulfing),
        ("morning_star", morning_star),
        ("evening_star", evening_star),
        ("three_soldiers", three_soldiers),
        ("pin_bar", pin_bar),
        ("marubozu", marubozu),
        ("outside_bar", outside_bar),
        ("tweezer", tweezer),
        ("harami", harami),
    ]


__all__ = [
    "doji",
    "hammer",
    "engulfing",
    "morning_star",
    "evening_star",
    "three_soldiers",
    "pin_bar",
    "marubozu",
    "inside_bar",
    "outside_bar",
    "tweezer",
    "harami",
    "score_patterns",
    "all_detectors",
]
