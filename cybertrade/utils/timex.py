"""Time helpers (UTC-first) for candle bucketing, schedules, and expiry math."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Tuple

# Quotex-style market hours are approximated; OTC assets trade 24/7 on the
# broker's synthetic feed while classic FX closes over the weekend.
FX_MARKET_CLOSE_FRIDAY_HOUR = 20   # 20:00 UTC
FX_MARKET_OPEN_SUNDAY_HOUR = 21    # 21:00 UTC


def now() -> float:
    return time.time()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def from_ts(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def to_ts(dt: datetime) -> float:
    return dt.timestamp()


def iso(ts: float | None = None) -> str:
    dt = from_ts(ts) if ts is not None else utc_now()
    return dt.isoformat(timespec="milliseconds")


def bucket_start(ts: float, seconds: int) -> float:
    """Floor *ts* to the opening timestamp of its bucket."""
    if seconds <= 0:
        raise ValueError("seconds must be > 0")
    return (int(ts) // seconds) * seconds


def bucket_id(ts: float, seconds: int) -> int:
    return int(ts) // seconds


def bucket_end(ts: float, seconds: int) -> float:
    return bucket_start(ts, seconds) + seconds


def seconds_to_candle_close(ts: float, seconds: int) -> float:
    return bucket_end(ts, seconds) - ts


def align_range(
    start_ts: float, end_ts: float, seconds: int
) -> Tuple[float, float]:
    """Expand a range to whole buckets."""
    s = bucket_start(start_ts, seconds)
    e = bucket_end(end_ts, seconds)
    return s, e


def add_expiry(ts: float, expiry_seconds: int) -> float:
    return ts + max(1, int(expiry_seconds))


def is_weekend_lock(ts: float) -> bool:
    """True when classic markets are closed (Fri 20:00 UTC -> Sun 21:00 UTC)."""
    dt = from_ts(ts)
    wd = dt.weekday()  # Mon=0 ... Sun=6
    if wd == 5:  # Saturday
        return True
    if wd == 4 and dt.hour >= FX_MARKET_CLOSE_FRIDAY_HOUR:
        return True
    if wd == 6 and dt.hour < FX_MARKET_OPEN_SUNDAY_HOUR:
        return True
    return False


def is_friday_cutoff(ts: float, cutoff_hour: int) -> bool:
    dt = from_ts(ts)
    return dt.weekday() == 4 and dt.hour >= cutoff_hour


def session_of_day(ts: float) -> str:
    """Coarse FX session label for the timestamp (UTC)."""
    hour = from_ts(ts).hour
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "overlap"
    if 16 <= hour < 20:
        return "newyork"
    return "offhours"


def countdown(target_ts: float, now_ts: float | None = None) -> str:
    """Human countdown ``HH:MM:SS`` to *target_ts*."""
    remaining = max(0.0, target_ts - (now_ts if now_ts is not None else now()))
    h, rem = divmod(int(remaining), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def date_slug(ts: float | None = None) -> str:
    dt = from_ts(ts) if ts is not None else utc_now()
    return dt.strftime("%Y%m%d_%H%M%S")


def next_daily_boundary(ts: float) -> float:
    """Next UTC midnight after *ts*."""
    dt = from_ts(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return to_ts(dt + timedelta(days=1))


def rolling_window(ts: float, seconds: float) -> Tuple[float, float]:
    return ts - seconds, ts


__all__ = [
    "now",
    "utc_now",
    "from_ts",
    "to_ts",
    "iso",
    "bucket_start",
    "bucket_id",
    "bucket_end",
    "seconds_to_candle_close",
    "align_range",
    "add_expiry",
    "is_weekend_lock",
    "is_friday_cutoff",
    "session_of_day",
    "countdown",
    "date_slug",
    "next_daily_boundary",
    "rolling_window",
]
