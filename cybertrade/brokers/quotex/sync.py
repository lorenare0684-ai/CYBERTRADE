"""History warm-start: pull venue candles into local books before trading.

Live strategies need indicator warmup before the first decision.  The
``candleHistory`` round-trip fills :class:`QuotexAPI`'s cache; these helpers
convert that cache into the engine's series books.  Everything is
failure-tolerant — a slow venue delays warmup, it never blocks boot forever.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence

from ...data.history import CandleSeries, HistoryBuffer, MultiTimeframeBook
from ...data.models import Candle

log = logging.getLogger("cybertrade.qx.sync")


def qxcandles_into_series(candles: Sequence[Candle], series: CandleSeries) -> int:
    """Push closed venue candles into a series (oldest first, dupe-safe)."""
    added = 0
    seen = {c.open_ts for c in series.candles()}
    for c in sorted(candles, key=lambda x: x.open_ts):
        if c.open_ts in seen:
            continue
        cc = c.copy()
        cc.closed = True
        series.push_closed(cc)
        seen.add(c.open_ts)
        added += 1
    return added


def warm_asset(
    api,
    asset: str,
    timeframe_seconds: int = 60,
    bars: int = 250,
    series: Optional[CandleSeries] = None,
    wait: float = 3.0,
) -> int:
    """Fetch history for one asset into ``series`` (or a fresh series).

    Returns the number of candles added.  Network errors are swallowed into
    ``0`` — warmup is best-effort by design.
    """
    target = series if series is not None else CandleSeries(
        asset, timeframe_seconds, maxlen=max(500, bars + 50)
    )
    try:
        candles = api.get_candles(asset, timeframe_seconds, count=bars, wait=wait)
    except Exception as exc:  # noqa: BLE001
        log.warning("history warm failed %s@%ss: %s", asset, timeframe_seconds, exc)
        return 0
    added = qxcandles_into_series(candles, target)
    if added:
        log.info("warmed %s@%ss +%d candles", asset, timeframe_seconds, added)
    else:
        log.debug("warmed %s@%ss +0 candles (venue silent)", asset,
                  timeframe_seconds)
    return added


def warm_book(
    api,
    book: MultiTimeframeBook,
    bars: int = 250,
    wait: float = 3.0,
) -> int:
    """Warm every tracked timeframe of a multi-timeframe book."""
    total = 0
    for tf, series in book.books.items():
        total += warm_asset(api, book.asset, tf, bars=bars, series=series, wait=wait)
    return total


def warm_universe(
    api,
    assets: Iterable[str],
    timeframe_seconds: int = 60,
    bars: int = 250,
    buffer: Optional[HistoryBuffer] = None,
    wait: float = 3.0,
) -> int:
    """Warm a whole universe (optionally into a shared HistoryBuffer)."""
    total = 0
    for asset in assets:
        if buffer is not None:
            series = buffer.series(asset, timeframe_seconds)
            total += warm_asset(api, asset, timeframe_seconds, bars=bars,
                                series=series, wait=wait)
        else:
            total += warm_asset(api, asset, timeframe_seconds, bars=bars, wait=wait)
    return total


def backfill_gaps(
    api,
    series: CandleSeries,
    *,
    gap_bars: int = 2,
    wait: float = 3.0,
    now: Optional[float] = None,
) -> int:
    """Pull only the bars a series is actually missing (Phase-30).

    A live chart does not re-request its whole history every refresh — it
    notices the last closed bucket and fetches the hole.  Returns candles
    added; ``0`` when the tail is fresh enough or the venue is slow (never
    raises — continuity is best-effort, honesty comes from warmup).
    """
    from ...utils import timex

    tf = int(series.timeframe_seconds)
    tail = series.candles(limit=1)
    if not tail:
        return 0
    last = tail[-1]
    current = timex.bucket_start(timex.now() if now is None else now, tf)
    missing = int((current - last.open_ts) // tf)
    if missing <= gap_bars:
        return 0
    count = min(500, missing + 5)
    try:
        fetched = api.get_candles(series.asset, tf, count=count, wait=wait)
    except Exception as exc:  # noqa: BLE001
        log.debug("gap backfill failed %s@%ss: %s", series.asset, tf, exc)
        return 0
    added = qxcandles_into_series(fetched, series)
    if added:
        log.info("gap backfill %s@%ss +%d (missing≈%d)",
                 series.asset, tf, added, missing)
    return added


__all__ = [
    "qxcandles_into_series",
    "warm_asset",
    "warm_book",
    "warm_universe",
    "backfill_gaps",
]
