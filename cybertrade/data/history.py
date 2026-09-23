"""Candle history stores and multi-timeframe resampling."""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional, Sequence

from ..constants import Timeframe
from ..exceptions import DataError
from ..utils import timex
from .models import Candle, candles_to_series


class CandleSeries:
    """Bounded, thread-safe series of OHLCV candles for one asset/timeframe."""

    def __init__(
        self,
        asset: str,
        timeframe_seconds: int,
        maxlen: int = 2000,
    ) -> None:
        if maxlen < 2:
            raise DataError("CandleSeries maxlen must be >= 2")
        self.asset = asset
        self.timeframe_seconds = int(timeframe_seconds)
        self._candles: Deque[Candle] = deque(maxlen=maxlen)
        self._lock = threading.RLock()

    # -- writes ------------------------------------------------------------
    def update(self, price: float, ts: Optional[float] = None) -> Candle:
        """Fold a price print into the live candle, rolling on bucket change."""
        ts = timex.now() if ts is None else ts
        bucket = timex.bucket_start(ts, self.timeframe_seconds)
        with self._lock:
            if not self._candles or self._candles[-1].open_ts != bucket:
                if self._candles:
                    self._candles[-1].closed = True
                self._candles.append(
                    Candle(
                        asset=self.asset,
                        timeframe_seconds=self.timeframe_seconds,
                        open_ts=bucket,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume=1.0,
                        closed=False,
                    )
                )
            else:
                self._candles[-1].update(price, ts)
            return self._candles[-1].copy()

    def push_closed(self, candle: Candle) -> None:
        """Append a fully-formed closed candle (history load / resample)."""
        with self._lock:
            self._candles.append(candle.copy())

    def close_live(self) -> Optional[Candle]:
        """Mark the live candle closed and return it."""
        with self._lock:
            if not self._candles:
                return None
            self._candles[-1].closed = True
            return self._candles[-1].copy()

    # -- reads -------------------------------------------------------------
    def __len__(self) -> int:
        with self._lock:
            return len(self._candles)

    def candles(self, limit: Optional[int] = None, closed_only: bool = False) -> List[Candle]:
        with self._lock:
            items = list(self._candles)
        if closed_only and items:
            items = items[:-1] if not items[-1].closed else items
        if limit is not None and limit > 0:
            items = items[-limit:]
        return [c.copy() for c in items]

    def closes(self) -> List[float]:
        with self._lock:
            return [c.close for c in self._candles]

    def as_series(self) -> Dict[str, List[float]]:
        return candles_to_series(self.candles())

    @property
    def last(self) -> Optional[Candle]:
        with self._lock:
            return self._candles[-1].copy() if self._candles else None

    @property
    def last_price(self) -> Optional[float]:
        with self._lock:
            return self._candles[-1].close if self._candles else None

    def last_price_or(self, default: float = 0.0) -> float:
        price = self.last_price
        return default if price is None else price

    def slice(self, count: int) -> List[Candle]:
        return self.candles(limit=count)


class MultiTimeframeBook:
    """Per-asset candle books across several timeframes, resampled from ticks.

    The lowest timeframe is updated from prints; higher timeframes roll up so a
    single tick stream powers every chart and strategy timeframe.
    """

    def __init__(
        self,
        asset: str,
        timeframes: Sequence[Timeframe] | None = None,
        maxlen: int = 2000,
    ) -> None:
        self.asset = asset
        self.timeframes = sorted(
            timeframes or (Timeframe.M1, Timeframe.M5, Timeframe.M15),
            key=lambda tf: tf.seconds,
        )
        self.books: Dict[int, CandleSeries] = {
            tf.seconds: CandleSeries(asset, tf.seconds, maxlen=maxlen) for tf in self.timeframes
        }
        self._lock = threading.RLock()

    def on_price(self, price: float, ts: Optional[float] = None) -> Dict[int, Candle]:
        """Update every timeframe book; returns {tf_seconds: live_candle}."""
        ts = timex.now() if ts is None else ts
        out: Dict[int, Candle] = {}
        with self._lock:
            for secs, book in self.books.items():
                out[secs] = book.update(price, ts)
        return out

    def book(self, timeframe_seconds: int) -> CandleSeries:
        try:
            return self.books[timeframe_seconds]
        except KeyError as exc:
            raise DataError(f"timeframe {timeframe_seconds}s not tracked for {self.asset}") from exc

    def closes(self, timeframe_seconds: int) -> List[float]:
        return self.book(timeframe_seconds).closes()


def resample(candles: Sequence[Candle], target_seconds: int) -> List[Candle]:
    """Resample closed candles to a larger timeframe."""
    if target_seconds <= 0:
        raise DataError("target_seconds must be > 0")
    out: List[Candle] = []
    bucket: Optional[Candle] = None
    for c in sorted(candles, key=lambda x: x.open_ts):
        if c.timeframe_seconds > target_seconds:
            raise DataError("cannot resample to a smaller timeframe")
        start = timex.bucket_start(c.open_ts, target_seconds)
        if bucket is None or bucket.open_ts != start:
            if bucket is not None:
                bucket.closed = True
                out.append(bucket)
            bucket = Candle(
                asset=c.asset,
                timeframe_seconds=target_seconds,
                open_ts=start,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                closed=True,
            )
        else:
            bucket.high = max(bucket.high, c.high)
            bucket.low = min(bucket.low, c.low)
            bucket.close = c.close
            bucket.volume += c.volume
    if bucket is not None:
        bucket.closed = True
        out.append(bucket)
    return out


def load_history(
    asset: str,
    closes: Sequence[float],
    timeframe_seconds: int,
    end_ts: Optional[float] = None,
) -> List[Candle]:
    """Build synthetic closed candles from a close-only series (warmup data)."""
    end = end_ts if end_ts is not None else timex.now()
    end_bucket = timex.bucket_start(end, timeframe_seconds)
    n = len(closes)
    out: List[Candle] = []
    prev = closes[0] if closes else 0.0
    for i, close in enumerate(closes):
        ts = end_bucket - (n - 1 - i) * timeframe_seconds
        open_ = prev
        high = max(open_, close)
        low = min(open_, close)
        out.append(
            Candle(
                asset=asset,
                timeframe_seconds=timeframe_seconds,
                open_ts=ts,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1.0,
                closed=True,
            )
        )
        prev = close
    return out


class HistoryBuffer:
    """Named collection of :class:`CandleSeries` keyed by (asset, tf)."""

    def __init__(self, maxlen: int = 2000) -> None:
        self._series: Dict[tuple, CandleSeries] = {}
        self._maxlen = maxlen
        self._lock = threading.RLock()

    def series(self, asset: str, timeframe_seconds: int) -> CandleSeries:
        key = (asset, timeframe_seconds)
        with self._lock:
            if key not in self._series:
                self._series[key] = CandleSeries(asset, timeframe_seconds, self._maxlen)
            return self._series[key]

    def ingest(self, asset: str, candles: Iterable[Candle]) -> None:
        for c in candles:
            self.series(asset, c.timeframe_seconds).push_closed(c)

    def assets(self) -> List[str]:
        with self._lock:
            return sorted({k[0] for k in self._series})


__all__ = [
    "CandleSeries",
    "MultiTimeframeBook",
    "HistoryBuffer",
    "resample",
    "load_history",
]
