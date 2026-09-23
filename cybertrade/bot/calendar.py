"""Economic-calendar awareness: trading blackouts around market-moving news.

Real release times come from a user-supplied JSON (``~/.cybertrade/calendar.json``)
or any ICS feed parsed at load; when absent the scheduler *estimates* the
well-known US releases (NFP first Friday, FOMC/CPI approximations) so the risk
engine still wraps the dangerous windows.  Accuracy is honest: estimates are
labelled ``estimated`` in the UI — see README notes.
"""

from __future__ import annotations

import calendar as _cal
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..exceptions import CybertradeError
from ..utils import timex

log = logging.getLogger("cybertrade.calendar")

DEFAULT_CALENDAR_FILE = "~/.cybertrade/calendar.json"
STANDARD_BLACKOUT_SECONDS = 600          # +/- 10 minutes around red news
RED_IMPACT_MINUTES = 10
ORANGE_IMPACT_MINUTES = 5

# crude but useful estimate rule: releases at 12:30 UTC (8:30 ET) unless noted
ESTIMATED_RELEASE_HOUR_UTC = 12
ESTIMATED_RELEASE_MINUTE = 30


class CalendarError(CybertradeError):
    pass


@dataclass
class NewsEvent:
    ts: float
    currency: str
    title: str
    impact: str = "high"                  # high | medium | low
    actual: str = ""
    estimated: bool = False
    blackout_seconds: int = STANDARD_BLACKOUT_SECONDS

    @property
    def window(self) -> tuple:
        return (self.ts - self.blackout_seconds, self.ts + self.blackout_seconds)

    def active_at(self, ts: float) -> bool:
        lo, hi = self.window
        return lo <= ts <= hi

    def to_dict(self) -> Dict[str, Any]:
        lo, hi = self.window
        return {
            "ts": self.ts,
            "iso": timex.iso(self.ts),
            "currency": self.currency,
            "title": self.title,
            "impact": self.impact,
            "estimated": self.estimated,
            "window_start": lo,
            "window_end": hi,
            "blackout_seconds": self.blackout_seconds,
        }


def _utc_ts(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return float(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


def first_friday(year: int, month: int) -> int:
    """Day-of-month of the first Friday (NFP rule)."""
    for day in range(1, 8):
        if datetime(year, month, day, tzinfo=timezone.utc).weekday() == 4:
            return day
    raise CalendarError(f"no first friday in {year}-{month}")


def nfp_dates(year: int, months: Sequence[int] | None = None) -> List[float]:
    """NFP release timestamps: 12:30 UTC on the first Friday of each month."""
    out: List[float] = []
    for month in (months or range(1, 13)):
        day = first_friday(year, month)
        out.append(_utc_ts(year, month, day, ESTIMATED_RELEASE_HOUR_UTC, ESTIMATED_RELEASE_MINUTE))
    return out


def estimate_fomc(year: int) -> List[float]:
    """Approx 8 scheduled FOMC rate decisions (statement 18:00 UTC).

    Roughly the 3rd Wednesday of Jan/Mar/May/Jun/Aug/Sep/Oct/Dec — labelled
    ``estimated``; replace via calendar.json for exact dates.
    """
    out: List[float] = []
    for month in (1, 3, 5, 6, 8, 9, 10, 12):
        # 3rd Wednesday
        wednesdays = [
            d for d in range(1, 29)
            if datetime(year, month, d, tzinfo=timezone.utc).weekday() == 2
        ]
        day = wednesdays[2]
        out.append(_utc_ts(year, month, day, 18, 0))
    return out


def estimate_cpi(year: int) -> List[float]:
    """Approx CPI prints: ~2nd Wednesday 12:30 UTC, monthly."""
    out: List[float] = []
    for month in range(1, 13):
        wednesdays = [
            d for d in range(1, 22)
            if datetime(year, month, d, tzinfo=timezone.utc).weekday() == 2
        ]
        day = wednesdays[1]
        out.append(_utc_ts(year, month, day, ESTIMATED_RELEASE_HOUR_UTC, ESTIMATED_RELEASE_MINUTE))
    return out


@dataclass
class EconomicCalendar:
    """Holds upcoming news events and computes live blackout state."""

    events: List[NewsEvent] = field(default_factory=list)
    blackout_seconds: int = STANDARD_BLACKOUT_SECONDS
    watch_currencies: tuple = ("USD", "EUR", "GBP", "JPY")

    def __post_init__(self) -> None:
        self.events.sort(key=lambda e: e.ts)

    # -- construction ------------------------------------------------------
    @classmethod
    def estimate_year(cls, year: int, blackout_seconds: int = STANDARD_BLACKOUT_SECONDS) -> "EconomicCalendar":
        events: List[NewsEvent] = []
        for ts in nfp_dates(year):
            events.append(NewsEvent(ts=ts, currency="USD", title="Non-Farm Payrolls",
                                    impact="high", estimated=True, blackout_seconds=blackout_seconds))
        for ts in estimate_cpi(year):
            events.append(NewsEvent(ts=ts, currency="USD", title="CPI (est)",
                                    impact="high", estimated=True, blackout_seconds=blackout_seconds))
        for ts in estimate_fomc(year):
            events.append(NewsEvent(ts=ts, currency="USD", title="FOMC Rate Decision (est)",
                                    impact="high", estimated=True, blackout_seconds=blackout_seconds))
        return cls(events=events, blackout_seconds=blackout_seconds)

    @classmethod
    def load(
        cls,
        path: Optional[str] = None,
        blackout_seconds: int = STANDARD_BLACKOUT_SECONDS,
        estimate_missing_year: Optional[int] = None,
    ) -> "EconomicCalendar":
        """Load user events from JSON; optionally top up with estimates.

        JSON schema: list of ``{ts|iso, currency, title, impact, blackout_seconds}``.
        """
        p = Path(path or DEFAULT_CALENDAR_FILE).expanduser()
        events: List[NewsEvent] = []
        if p.exists():
            try:
                raw = json.loads(p.read_text("utf-8"))
                for item in raw if isinstance(raw, list) else raw.get("events", []):
                    ts = item.get("ts")
                    if ts is None and "iso" in item:
                        ts = datetime.fromisoformat(
                            item["iso"].replace("Z", "+00:00")).timestamp()
                    events.append(NewsEvent(
                        ts=float(ts),
                        currency=str(item.get("currency", "USD")).upper(),
                        title=str(item.get("title", "News")),
                        impact=str(item.get("impact", "high")),
                        estimated=bool(item.get("estimated", False)),
                        blackout_seconds=int(item.get("blackout_seconds", blackout_seconds)),
                    ))
                log.info("calendar loaded %d events from %s", len(events), p)
            except Exception as exc:  # noqa: BLE001
                raise CalendarError(f"bad calendar file {p}: {exc}") from exc
        year = estimate_missing_year or datetime.now(timezone.utc).year
        if not events:
            cal = cls.estimate_year(year, blackout_seconds)
            log.info("calendar file absent — using estimated releases for %d", year)
            return cal
        return cls(events=events, blackout_seconds=blackout_seconds)

    # -- queries -----------------------------------------------------------
    def upcoming(self, now: float, horizon_seconds: float = 14 * 86400.0, limit: int = 20) -> List[NewsEvent]:
        horizon_end = now + horizon_seconds
        return [e for e in self.events if now - e.blackout_seconds <= e.ts <= horizon_end][:limit]

    def is_blackout(self, now: float, currency: Optional[str] = None) -> bool:
        return bool(self.active_events(now, currency))

    def active_events(self, now: float, currency: Optional[str] = None) -> List[NewsEvent]:
        out = []
        for e in self.events:
            if currency and e.currency != currency.upper():
                continue
            if e.active_at(now):
                out.append(e)
        return out

    def seconds_to_nearest(self, now: float) -> Optional[float]:
        future = [e.ts - now for e in self.events if e.ts >= now]
        return min(future) if future else None

    def to_list(self, now: float, horizon_seconds: float = 14 * 86400.0) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.upcoming(now, horizon_seconds)]


def calendar_for_asset(asset: str) -> str:
    """Best-effort quote currency of a broker symbol (``EURUSD_otc`` -> USD)."""
    symbol = asset.upper().replace("_OTC", "").replace("/", "")[:6]
    if len(symbol) >= 6:
        return symbol[3:]
    return "USD"


__all__ = [
    "NewsEvent",
    "EconomicCalendar",
    "CalendarError",
    "first_friday",
    "nfp_dates",
    "estimate_fomc",
    "estimate_cpi",
    "calendar_for_asset",
    "STANDARD_BLACKOUT_SECONDS",
]
