"""Market data feeds: synthetic, replay, and live adapter interface."""

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
from .synthetic import MarketSimulator, MarketParams, generate_candles

log = logging.getLogger("cybertrade.feed")


class Feed:
    """Abstract market data source.

    Implementations push ticks via :meth:`_emit_tick`; consumers subscribe on
    the event bus or register plain callbacks for engine-local speed.
    """

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


class SyntheticFeed(Feed):
    """Threaded simulated market — one :class:`MarketSimulator` per asset.

    Different assets can run different scenarios so the terminal can face
    "every market condition" simultaneously (EURUSD in a crash while BTC
    grinds, etc.).  Scenarios can be hot-swapped at runtime.
    """

    def __init__(
        self,
        assets: Sequence[str] | None = None,
        scenarios: Optional[Dict[str, str]] = None,
        timeframe_seconds: int = 60,
        tick_interval: float = 0.25,
        params_by_asset: Optional[Dict[str, MarketParams]] = None,
        seed: int = 1337,
        warmup_bars: int = 400,
    ) -> None:
        super().__init__(assets or ["SIM"])
        self.timeframe_seconds = timeframe_seconds
        self.tick_interval = tick_interval
        self.scenarios = scenarios or {}
        self.seed = seed
        self.warmup_bars = warmup_bars
        self._sims: Dict[str, MarketSimulator] = {}
        self._books: Dict[str, MultiTimeframeBook] = {}
        self._last: Dict[str, float] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._bar_timers: Dict[str, float] = {}
        params_by_asset = params_by_asset or {}

        for i, asset in enumerate(self.assets):
            scenario = self.scenarios.get(asset, "gbm")
            params = params_by_asset.get(asset, MarketParams(timeframe_seconds=timeframe_seconds))
            params.timeframe_seconds = timeframe_seconds
            self._sims[asset] = MarketSimulator(
                scenario=scenario,
                params=params,
                seed=seed + i * 97,
                asset=asset,
            )
            self._books[asset] = MultiTimeframeBook(
                asset,
                timeframes=self._default_timeframes(),
                maxlen=max(500, warmup_bars + 100),
            )
            self._last[asset] = self._sims[asset].price

    def _default_timeframes(self):
        from ..constants import Timeframe

        base = max(5, self.timeframe_seconds)
        tfs = [Timeframe.S5 if base <= 5 else Timeframe.M1]
        if base <= 60:
            tfs.append(Timeframe.M1 if base > 5 else Timeframe.M5)
        tfs.extend([Timeframe.M5, Timeframe.M15])
        seen = set()
        out = []
        for tf in tfs:
            if tf not in seen:
                seen.add(tf)
                out.append(tf)
        return out

    # -- feed api ----------------------------------------------------------
    def history(self, asset: str, bars: int, timeframe_seconds: int) -> List[Candle]:
        sim = self._sims.get(asset)
        if sim is None:
            raise FeedError(f"unknown asset {asset}")
        return generate_candles(
            sim.scenario,
            bars=bars,
            params=sim.process.params,
            seed=self.seed,
            asset=asset,
        )

    def warmup(self) -> None:
        """Preload books so indicators have history at boot."""
        for asset, sim in self._sims.items():
            candles = generate_candles(
                sim.scenario,
                bars=self.warmup_bars,
                params=sim.process.params,
                seed=self.seed,
                asset=asset,
            )
            for c in candles:
                self._books[asset].on_price(c.close, c.close_ts - 1)
            if candles:
                # continuity: the simulator must RESUME at the warmup end,
                # or the first live tick teleports price to start_price and
                # every detector downstream sees a phantom crash bar.
                sim.state.price = candles[-1].close
                if sim.state.anchor:
                    pass  # keep configured anchor (mean-revert center)
                self._last[asset] = candles[-1].close
            else:
                self._last[asset] = sim.price
        log.info("synthetic warmup complete bars=%d assets=%d", self.warmup_bars, len(self._sims))

    def last_price(self, asset: str) -> Optional[float]:
        return self._last.get(asset)

    def book(self, asset: str) -> MultiTimeframeBook:
        if asset not in self._books:
            raise FeedError(f"unknown asset {asset}")
        return self._books[asset]

    def regime_hint(self, asset: str) -> str:
        sim = self._sims.get(asset)
        return sim.state.regime.value if sim else "unknown"

    def set_scenario(self, asset: str, scenario: str) -> None:
        """Hot-swap one asset's market regime (web/GUI command)."""
        sim = self._sims.get(asset)
        if sim is None:
            raise FeedError(f"unknown asset {asset}")
        sim.set_scenario(scenario)
        self.scenarios[asset] = scenario
        log.info("scenario switch %s -> %s", asset, scenario)
        default_bus.publish(
            Topic.LOG,
            {"ts": timex.now(), "level": "INFO", "name": "feed",
             "msg": f"scenario {asset} -> {scenario}"},
            source="feed",
        )

    def current_scenarios(self) -> Dict[str, str]:
        return {a: s.scenario for a, s in self._sims.items()}

    # -- runtime -----------------------------------------------------------
    def start(self) -> None:
        super().start()
        for asset in self.assets:
            if asset in self._threads and self._threads[asset].is_alive():
                continue
            th = threading.Thread(
                target=self._run_asset, args=(asset,), daemon=True, name=f"feed-{asset}"
            )
            self._threads[asset] = th
            th.start()
        log.info("synthetic feed started assets=%s", self.assets)

    def stop(self) -> None:
        super().stop()
        for th in self._threads.values():
            th.join(timeout=2.0)
        log.info("synthetic feed stopped")

    def _run_asset(self, asset: str) -> None:
        sim = self._sims[asset]
        book = self._books[asset]
        next_bar = time.time() + self.timeframe_seconds
        rng = sim._tick_rng  # deliberate: same stream as tick jitter
        while self._running:
            px, spread = sim.tick()
            half = spread / 2.0
            tick = Tick(asset=asset, price=px, bid=px - half, ask=px + half)
            self._last[asset] = px
            book.on_price(px, tick.ts)
            self._emit_tick(tick)
            now = time.time()
            if now >= next_bar:
                sim.step_bar()
                next_bar = now + self.timeframe_seconds
            # slight interval jitter keeps cadence organic
            time.sleep(max(0.01, self.tick_interval * rng.uniform(0.8, 1.2)))


class ReplayFeed(Feed):
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


__all__ = ["Feed", "SyntheticFeed", "ReplayFeed", "QuoteBook"]
