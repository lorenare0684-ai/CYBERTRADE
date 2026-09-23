"""Live venue feed — Quotex candles only, never synthetic (Phase-29).

Switching to live mode means switching the DATA: every candle, tick, and
detector input comes from the venue websocket/session, never from a
generator. ``SyntheticFeed.is_synthetic`` is True; this class is False, and
``TradingEngine`` refuses to boot a live broker mode (``quotex``/``dryrun``)
against any synthetic feed — no silent fallback, ever.

Warmup pulls real history through ``sync.warm_book``; the running stream
rides ``QuotexAPI`` tick handlers into the same MultiTimeframeBook the
engine's detector already reads. If the venue yields zero candles, boot
fails loudly (a blind trader is worse than a stopped one).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Sequence

from ..constants import Timeframe
from ..exceptions import FeedError
from .feed import Feed
from .history import MultiTimeframeBook
from .models import Candle, Tick

log = logging.getLogger("cybertrade.livefeed")


class LiveQuotexFeed(Feed):
    """Venue-backed market data — the only feed allowed in live modes."""

    is_synthetic = False

    def __init__(
        self,
        api,
        assets: Sequence[str],
        timeframe_seconds: int = 60,
        warm_bars: int = 400,
        refresh_seconds: float = 30.0,
    ) -> None:
        super().__init__(assets)
        self.api = api
        self.timeframe_seconds = max(5, int(timeframe_seconds))
        self.warm_bars = int(warm_bars)
        self.refresh_seconds = float(refresh_seconds)
        self._books = {
            a: MultiTimeframeBook(
                a, timeframes=self._default_timeframes(),
                maxlen=max(500, self.warm_bars + 100),
            )
            for a in self.assets
        }
        self._last = {a: 0.0 for a in self.assets}
        self._refresher: Optional[threading.Thread] = None

    def _default_timeframes(self):
        base = max(5, self.timeframe_seconds)
        tfs = [Timeframe.S5 if base <= 5 else Timeframe.M1]
        if base <= 60:
            tfs.append(Timeframe.M1 if base > 5 else Timeframe.M5)
        tfs.extend([Timeframe.M5, Timeframe.M15])
        seen, out = set(), []
        for tf in tfs:
            if tf not in seen:
                seen.add(tf)
                out.append(tf)
        return out

    # -- feed api ----------------------------------------------------------
    def book(self, asset: str) -> MultiTimeframeBook:
        if asset not in self._books:
            raise FeedError(f"unknown asset {asset}")
        return self._books[asset]

    def history(self, asset: str, bars: int, timeframe_seconds: int) -> List[Candle]:
        book = self._books.get(asset)
        if book is not None:
            try:
                candles = book.book(timeframe_seconds).candles(limit=bars)
                if candles:
                    return candles
            except Exception:  # noqa: BLE001 — fall through to the venue
                pass
        getter = getattr(self.api, "get_candles", None)
        if getter is None:
            raise FeedError(f"no history for {asset}")
        return list(getter(asset, timeframe_seconds, count=bars, wait=5.0))

    def last_price(self, asset: str) -> Optional[float]:
        px = self._last.get(asset)
        if px:
            return px
        getter = getattr(self.api, "last_price", None)
        if getter is None:
            return None
        try:
            return getter(asset)
        except Exception:  # noqa: BLE001
            return None

    def regime_hint(self, asset: str) -> str:
        return "live"

    # -- lifecycle ---------------------------------------------------------
    def warmup(self) -> None:
        """Pull REAL venue history into the books — zero candles = refuse."""
        from ..brokers.quotex.sync import warm_book

        total = 0
        for asset in self.assets:
            try:
                total += warm_book(
                    self.api, self._books[asset],
                    bars=self.warm_bars, wait=5.0,
                )
            except Exception as exc:  # noqa: BLE001 — per-asset tolerance
                log.warning("live warm failed %s: %s", asset, exc)
            if not self._last.get(asset):
                px = None
                getter = getattr(self.api, "last_price", None)
                if getter is not None:
                    try:
                        px = getter(asset)
                    except Exception:  # noqa: BLE001
                        px = None
                if px is None:
                    try:
                        recent = self._books[asset].book(
                            self.timeframe_seconds).candles(limit=1)
                        if recent:
                            px = recent[-1].close
                    except Exception:  # noqa: BLE001
                        px = None
                if px:
                    self._last[asset] = float(px)
        if total <= 0:
            raise FeedError(
                "live warmup got 0 candles — venue session dead? "
                "run `cybertrade quotex login` and solve the CAPTCHA once"
            )
        log.info("live warmup complete candles=%d assets=%d", total, len(self.assets))

    def start(self) -> None:
        super().start()
        adder = getattr(self.api, "add_tick_handler", None)
        if adder is not None:
            adder(self._on_venue_tick)
        sub = getattr(self.api, "subscribe", None)
        if sub is not None:
            for asset in self.assets:
                try:
                    sub(asset, self.timeframe_seconds)
                except Exception as exc:  # noqa: BLE001
                    log.warning("subscribe failed %s: %s", asset, exc)
        if self.refresh_seconds > 0:
            self._refresher = threading.Thread(
                target=self._refresh_loop, daemon=True, name="livefeed-refresh"
            )
            self._refresher.start()
        log.info("live feed started assets=%d", len(self.assets))

    def stop(self) -> None:
        super().stop()
        t = self._refresher
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._refresher = None

    # -- stream ------------------------------------------------------------
    def _on_venue_tick(self, tick: Tick) -> None:
        if not self._running:
            return
        if tick.asset not in self._books:
            return
        self._books[tick.asset].on_price(tick.price, tick.ts)
        self._last[tick.asset] = tick.price
        self._emit_tick(tick)

    def _refresh_loop(self) -> None:
        """Belt-and-braces: re-pull closed candles so bars stay honest."""
        from ..brokers.quotex.sync import qxcandles_into_series

        while self._running:
            time.sleep(self.refresh_seconds)
            if not self._running:
                break
            for asset in self.assets:
                try:
                    candles = self.api.get_candles(
                        asset, self.timeframe_seconds,
                        count=min(self.warm_bars, 300), wait=3.0,
                    )
                    if candles:
                        qxcandles_into_series(
                            candles,
                            self._books[asset].book(self.timeframe_seconds),
                        )
                except Exception:  # noqa: BLE001 — a slow venue never kills the feed
                    log.debug("live refresh failed for %s", asset, exc_info=True)


# typing convenience for annotations above
from typing import List  # noqa: E402  (kept local to avoid import cycle noise)

__all__ = ["LiveQuotexFeed"]
