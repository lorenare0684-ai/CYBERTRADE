"""Market data feeds: replay and live adapter interface.

There is no synthetic market in this build.  Live trading consumes venue
candles only (:mod:`cybertrade.data.livefeed`); :class:`ReplayFeed` exists
for replaying a recorded venue tape during tests and is refused by the
engine in live mode exactly like any generator-backed feed would be.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence

from ..events import Topic, default_bus
from ..exceptions import FeedError
from ..utils import timex
from .history import CandleSeries, MultiTimeframeBook
from .models import Candle, Tick

log = logging.getLogger("cybertrade.feed")


class Feed:
    """Abstract market data source.

    Implementations push ticks via :meth:`_emit_tick`; consumers subscribe on
    the event bus or register plain callbacks for engine-local speed.

    ``is_synthetic`` marks non-venue feeds (replay/generator): the engine
    refuses them outright — live trading rides venue candles only.
    """

    is_synthetic: bool = False

    def __init__(self, assets: Sequence[str]) -> None:
        self.assets = list(assets)
        self._callbacks: List[Callable[[Tick], None]] = []
        self._running = False
        self._lock = threading.RLock()

    # -- wiring ------------------------------------------------------------
    def add_listener(self, callback: Callable[[Tick], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def remove_listener(self, callback: Callable[[Tick], None]) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def _emit_tick(self, tick: Tick) -> None:
        with self._lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(tick)
            except Exception:  # noqa: BLE001
                log.exception("tick listener failed asset=%s", tick.asset)
        default_bus.publish(Topic.TICK, tick, source=self.__class__.__name__)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def history(self, asset: str, bars: int, timeframe_seconds: int) -> List[Candle]:
        raise FeedError(f"{self.__class__.__name__} provides no history")

    def last_price(self, asset: str) -> Optional[float]:
        raise FeedError(f"{self.__class__.__name__} provides no quotes")


class ReplayFeed(Feed):
    """Replays a recorded tape as ticks — still not live venue data."""

    is_synthetic = True
    """Replay historical (or generated) candles at accelerated speed."""

    def __init__(
        self,
        asset: str,
        candles: Sequence[Candle],
        delay: float = 0.0,
        timeframe_seconds: int = 60,
        loop: bool = False,
    ) -> None:
        super().__init__([asset])
        if not candles:
            raise FeedError("ReplayFeed needs at least one candle")
        self._candles = list(candles)
        self.delay = delay
        self.timeframe_seconds = timeframe_seconds
        self.loop = loop
        self._index = 0
        self._last_price: Optional[float] = None
        self._thread: Optional[threading.Thread] = None

    def history(self, asset: str, bars: int, timeframe_seconds: int) -> List[Candle]:
        return self._candles[:bars]

    def last_price(self, asset: str) -> Optional[float]:
        return self._last_price

    def replay_once(self) -> List[Tick]:
        """Synchronously emit every tick (used by backtests/tests)."""
        ticks: List[Tick] = []
        for c in self._candles:
            tick = Tick(asset=self.assets[0], price=c.close, ts=c.close_ts - 0.5)
            self._last_price = c.close
            ticks.append(tick)
            self._emit_tick(tick)
        return ticks

    def start(self) -> None:
        super().start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="feed-replay")
        self._thread.start()

    def stop(self) -> None:
        super().stop()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        asset = self.assets[0]
        while self._running:
            c = self._candles[self._index]
            tick = Tick(asset=asset, price=c.close, ts=c.close_ts - 0.5)
            self._last_price = c.close
            self._emit_tick(tick)
            self._index += 1
            if self._index >= len(self._candles):
                if self.loop:
                    self._index = 0
                else:
                    break
            if self.delay > 0:
                time.sleep(self.delay)


class QuoteBook:
    """Rolling best-bid/ask book per asset (for spread-aware risk checks)."""

    def __init__(self, depth: int = 64) -> None:
        self._bids: Dict[str, List[float]] = {}
        self._asks: Dict[str, List[float]] = {}
        self._depth = depth

    def on_tick(self, tick: Tick) -> None:
        bids = self._bids.setdefault(tick.asset, [])
        asks = self._asks.setdefault(tick.asset, [])
        bids.append(tick.bid if tick.bid > 0 else tick.price)
        asks.append(tick.ask if tick.ask > 0 else tick.price)
        if len(bids) > self._depth:
            del bids[:-self._depth]
        if len(asks) > self._depth:
            del asks[:-self._depth]

    def spread(self, asset: str) -> float:
        bids = self._bids.get(asset) or [0.0]
        asks = self._asks.get(asset) or [0.0]
        return max(0.0, asks[-1] - bids[-1])

    def avg_spread(self, asset: str) -> float:
        bids = self._bids.get(asset) or [0.0]
        asks = self._asks.get(asset) or [0.0]
        n = min(len(bids), len(asks))
        if n == 0:
            return 0.0
        return sum(asks[-n:][i] - bids[-n:][i] for i in range(n)) / n

    def mid(self, asset: str) -> float:
        bids = self._bids.get(asset) or [0.0]
        asks = self._asks.get(asset) or [0.0]
        return 0.5 * (bids[-1] + asks[-1])

    def liquidity_score(self, asset: str) -> float:
        """0..1 score — 1 is tight stable spread, 0 is a vacuum."""
        avg = self.avg_spread(asset)
        if avg <= 0:
            return 1.0
        last = self.spread(asset)
        mult = last / avg if avg else 1.0
        score = 1.0 / (1.0 + max(0.0, mult - 1.0))
        return max(0.0, min(1.0, score))


__all__ = ["Feed", "ReplayFeed", "QuoteBook"]
