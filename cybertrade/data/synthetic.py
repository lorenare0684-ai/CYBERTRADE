"""Synthetic market generators covering every regime the survivor must face.

Regime models
-------------
- ``gbm``             geometric Brownian motion (baseline diffusion)
- ``bull_trend``      persistent bullish drift with noise
- ``bear_trend``      persistent bearish drift with noise
- ``range_chop``      Ornstein–Uhlenbeck mean reversion around an anchor
- ``low_vol_grind``   compressed volatility drift — death by a thousand cuts
- ``high_vol_expansion`` volatility bursts, wide bars, violent swings
- ``flash_crash``     sudden violent drop + partial recovery
- ``gap_open``        discontinuous jumps between bars
- ``news_spike``      instant impulse wicks then normalization
- ``liquidity_vacuum`` spread blow-outs and price teleporting
- ``regime_whipsaw``  rapid regime switching to punish overfit systems

All generators are pure Python with explicit RNG seams so tests are stable.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence

from ..constants import MarketRegime
from ..exceptions import DataError
from ..utils import timex
from .models import Candle

SCENARIO_NAMES: tuple[str, ...] = (
    "gbm",
    "bull_trend",
    "bear_trend",
    "range_chop",
    "low_vol_grind",
    "high_vol_expansion",
    "flash_crash",
    "gap_open",
    "news_spike",
    "liquidity_vacuum",
    "regime_whipsaw",
)


@dataclass
class MarketParams:
    """Physical knobs of the simulated market."""

    start_price: float = 1.1000
    base_vol: float = 0.0006          # per-bar return stdev
    drift: float = 0.0
    mean_reversion: float = 0.05      # OU pull strength per bar
    anchor: float = 0.0
    jump_prob: float = 0.0
    jump_scale: float = 0.0
    vol_of_vol: float = 0.0
    spread_bps: float = 0.4
    timeframe_seconds: int = 60
    seed: int = 1337

    def validate(self) -> None:
        if self.start_price <= 0:
            raise DataError("start_price must be > 0")
        if self.base_vol < 0:
            raise DataError("base_vol must be >= 0")
        if self.timeframe_seconds <= 0:
            raise DataError("timeframe_seconds must be > 0")


@dataclass
class MarketState:
    """Mutable state threaded through generation."""

    price: float
    vol: float
    anchor: float
    regime: MarketRegime = MarketRegime.UNKNOWN
    bar: int = 0
    stress: float = 0.0              # 0..1 — feeds spread/liquidity models
    events: List[str] = field(default_factory=list)


class PriceProcess:
    """Base class: evolves a price one bar at a time."""

    def __init__(self, params: MarketParams, rng: Optional[random.Random] = None) -> None:
        params.validate()
        self.params = params
        self.rng = rng or random.Random(params.seed)

    def initial_state(self) -> MarketState:
        p = self.params
        return MarketState(
            price=p.start_price,
            vol=p.base_vol,
            anchor=p.anchor or p.start_price,
        )

    def next_return(self, state: MarketState) -> float:
        raise NotImplementedError

    def step(self, state: MarketState) -> float:
        """Advance one bar, mutate *state*, return the new price."""
        r = self.next_return(state)
        state.price = max(1e-9, state.price * math.exp(r))
        state.bar += 1
        return state.price


class GBMProcess(PriceProcess):
    def next_return(self, state: MarketState) -> float:
        p = self.params
        z = self.rng.gauss(0.0, 1.0)
        if p.vol_of_vol > 0:
            state.vol = max(
                p.base_vol * 0.25,
                state.vol * math.exp(p.vol_of_vol * self.rng.gauss(0.0, 1.0) - 0.5 * p.vol_of_vol**2),
            )
        else:
            state.vol = p.base_vol
        r = p.drift + state.vol * z
        if p.jump_prob > 0 and self.rng.random() < p.jump_prob:
            r += self.rng.choice((-1.0, 1.0)) * p.jump_scale * abs(self.rng.gauss(1.0, 0.3))
            state.events.append("jump")
        return r


class TrendProcess(GBMProcess):
    def __init__(self, params: MarketParams, direction: int = 1, **kw) -> None:
        super().__init__(params, **kw)
        self.direction = 1 if direction >= 0 else -1

    def next_return(self, state: MarketState) -> float:
        r = super().next_return(state)
        drift = abs(self.params.drift) or (self.params.base_vol * 0.45)
        persistence = 0.85
        reg = self.direction * drift * persistence
        state.regime = (
            MarketRegime.BULL_TREND if self.direction > 0 else MarketRegime.BEAR_TREND
        )
        return r + reg


class MeanRevertProcess(PriceProcess):
    """Ornstein–Uhlenbeck in log space."""

    def next_return(self, state: MarketState) -> float:
        p = self.params
        anchor = state.anchor or p.start_price
        pull = p.mean_reversion * math.log(anchor / max(state.price, 1e-9))
        r = pull + p.base_vol * self.rng.gauss(0.0, 1.0)
        state.regime = MarketRegime.RANGE
        return r


class LowVolGrind(GBMProcess):
    def next_return(self, state: MarketState) -> float:
        p = self.params
        saved = p.base_vol
        p.base_vol = saved * 0.35
        try:
            r = super().next_return(state)
        finally:
            p.base_vol = saved
        r += (p.drift or saved * 0.15)      # slow drip
        state.regime = MarketRegime.LOW_VOL
        return r


class HighVolExpansion(GBMProcess):
    def next_return(self, state: MarketState) -> float:
        p = self.params
        saved = p.base_vol
        p.base_vol = saved * 3.2
        try:
            r = super().next_return(state)
        finally:
            p.base_vol = saved
        state.regime = MarketRegime.HIGH_VOL
        state.stress = min(1.0, state.stress + 0.05)
        return r


class FlashCrashProcess(GBMProcess):
    """Occasional violent down-spikes with reflexive partial recovery."""

    CRASH_PROB = 0.008

    def next_return(self, state: MarketState) -> float:
        r = super().next_return(state)
        if self.rng.random() < self.CRASH_PROB:
            drop = self.rng.uniform(0.015, 0.05)
            state.events.append("flash_crash")
            state.stress = 1.0
            state.regime = MarketRegime.CRISIS
            # half of the crash retraces over the next few bars (stored as stress)
            return r - drop
        if state.stress > 0:
            r += 0.25 * state.stress * self.params.base_vol * self.rng.uniform(2.0, 6.0)
            state.stress = max(0.0, state.stress - 0.12)
            if state.stress == 0:
                state.regime = MarketRegime.HIGH_VOL
        return r


class GapProcess(GBMProcess):
    """Discontinuous open jumps between bars."""

    GAP_PROB = 0.02

    def next_return(self, state: MarketState) -> float:
        r = super().next_return(state)
        if self.rng.random() < self.GAP_PROB:
            gap = self.rng.choice((-1.0, 1.0)) * self.rng.uniform(0.004, 0.012)
            state.events.append("gap")
            state.regime = MarketRegime.GAP
            return r + gap
        return r


class NewsSpikeProcess(GBMProcess):
    """Impulse wick: extreme single-bar move that mean-reverts quickly."""

    SPIKE_PROB = 0.01

    def next_return(self, state: MarketState) -> float:
        r = super().next_return(state)
        if self.rng.random() < self.SPIKE_PROB:
            spike = self.rng.choice((-1.0, 1.0)) * self.rng.uniform(0.008, 0.02)
            state.events.append("news_spike")
            state.stress = 0.8
            return r + spike
        if state.stress > 0:
            r -= 0.3 * state.stress * math.copysign(self.params.base_vol * 3, r if r else 1)
            state.stress = max(0.0, state.stress - 0.15)
        return r


class LiquidityVacuumProcess(GBMProcess):
    """Teleporting prints and huge effective spreads."""

    def next_return(self, state: MarketState) -> float:
        r = super().next_return(state)
        if self.rng.random() < 0.03:
            state.events.append("liquidity_gap")
            state.stress = 1.0
            return r + self.rng.choice((-1.0, 1.0)) * self.rng.uniform(0.002, 0.008)
        state.stress = min(1.0, state.stress + 0.01)
        return r


class RegimeWhipsaw(GBMProcess):
    """Rapid alternation between trend and mean-reversion to punish curves."""

    def __init__(self, params: MarketParams, **kw) -> None:
        super().__init__(params, **kw)
        self._mode = "trend"
        self._left = self.rng.randint(8, 30)
        self._mr = MeanRevertProcess(params, rng=self.rng)
        self._up = TrendProcess(params, direction=1, rng=self.rng)
        self._down = TrendProcess(params, direction=-1, rng=self.rng)

    def next_return(self, state: MarketState) -> float:
        self._left -= 1
        if self._left <= 0:
            self._mode = self.rng.choice(("trend", "range"))
            self._left = self.rng.randint(8, 30)
            state.events.append(f"switch_{self._mode}")
        if self._mode == "range":
            state.regime = MarketRegime.RANGE
            return self._mr.next_return(state)
        proc = self._up if self.rng.random() < 0.5 else self._down
        return proc.next_return(state)


_PROCESS_MAP = {
    "gbm": lambda p, rng: GBMProcess(p, rng=rng),
    "bull_trend": lambda p, rng: TrendProcess(p, direction=1, rng=rng),
    "bear_trend": lambda p, rng: TrendProcess(p, direction=-1, rng=rng),
    "range_chop": lambda p, rng: MeanRevertProcess(p, rng=rng),
    "low_vol_grind": lambda p, rng: LowVolGrind(p, rng=rng),
    "high_vol_expansion": lambda p, rng: HighVolExpansion(p, rng=rng),
    "flash_crash": lambda p, rng: FlashCrashProcess(p, rng=rng),
    "gap_open": lambda p, rng: GapProcess(p, rng=rng),
    "news_spike": lambda p, rng: NewsSpikeProcess(p, rng=rng),
    "liquidity_vacuum": lambda p, rng: LiquidityVacuumProcess(p, rng=rng),
    "regime_whipsaw": lambda p, rng: RegimeWhipsaw(p, rng=rng),
}


def make_process(
    scenario: str,
    params: Optional[MarketParams] = None,
    seed: int = 1337,
) -> PriceProcess:
    if scenario not in _PROCESS_MAP:
        raise DataError(f"unknown scenario {scenario!r}; pick from {SCENARIO_NAMES}")
    p = params or MarketParams()
    p.seed = seed
    rng = random.Random(seed)
    return _PROCESS_MAP[scenario](p, rng)


def generate_candles(
    scenario: str,
    bars: int = 500,
    params: Optional[MarketParams] = None,
    seed: int = 1337,
    start_ts: Optional[float] = None,
    asset: str = "SIM",
) -> List[Candle]:
    """Produce *bars* closed candles for a named market scenario."""
    if bars < 1:
        raise DataError("bars must be >= 1")
    proc = make_process(scenario, params, seed=seed)
    state = proc.initial_state()
    p = proc.params
    tf = p.timeframe_seconds
    end_ts = start_ts if start_ts is not None else timex.now()
    open0 = timex.bucket_start(end_ts, tf) - (bars - 1) * tf

    out: List[Candle] = []
    prev = state.price
    for i in range(bars):
        price = proc.step(state)
        o = prev
        c = price
        wick = abs(state.vol) * prev * 0.6
        h = max(o, c) + abs(proc.rng.gauss(0, wick if wick > 0 else 1e-9))
        l = min(o, c) - abs(proc.rng.gauss(0, wick if wick > 0 else 1e-9))
        out.append(
            Candle(
                asset=asset,
                timeframe_seconds=tf,
                open_ts=open0 + i * tf,
                open=o,
                high=max(o, h, c),
                low=min(o, l, c),
                close=c,
                volume=float(proc.rng.randint(50, 400)),
                closed=True,
            )
        )
        prev = c
    return out


class MarketSimulator:
    """Stateful tick-level simulator used by the paper feed and web demo.

    Generates ticks inside each candle bucket consistent with a scenario so the
    live terminal, charts, and engine see realistic intra-bar behaviour.
    """

    def __init__(
        self,
        scenario: str = "gbm",
        params: Optional[MarketParams] = None,
        seed: int = 1337,
        asset: str = "SIM",
    ) -> None:
        self.asset = asset
        self.scenario = scenario
        self.process = make_process(scenario, params, seed=seed)
        self.state = self.process.initial_state()
        self._tick_rng = random.Random(seed ^ 0x5F5F)
        self.tf = self.process.params.timeframe_seconds

    @property
    def price(self) -> float:
        return self.state.price

    @property
    def spread(self) -> float:
        base = self.process.params.spread_bps / 10_000.0
        return self.state.price * base * (1.0 + 3.0 * self.state.stress)

    def tick(self) -> tuple[float, float]:
        """Emit (price, spread) for one print without rolling a full bar."""
        jitter = self.process.params.base_vol * 0.35
        noise = self._tick_rng.gauss(0.0, jitter)
        px = self.state.price * math.exp(noise)
        return px, self.spread

    def step_bar(self) -> float:
        return self.process.step(self.state)

    def set_scenario(self, scenario: str) -> None:
        """Hot-swap the market regime, preserving price continuity."""
        old_price = self.state.price
        old_anchor = self.state.anchor
        self.scenario = scenario
        self.process = make_process(scenario, self.process.params)
        self.state = self.process.initial_state()
        self.state.price = old_price
        self.state.anchor = old_anchor or old_price
        self.tf = self.process.params.timeframe_seconds

    def bar_generator(self, bars: int) -> Iterator[float]:
        for _ in range(bars):
            yield self.step_bar()


def scenario_catalog() -> List[dict]:
    """Describe every stress scenario for UIs and docs."""
    return [
        {"name": "bull_trend", "regime": "bull_trend", "risk": "revenge shorting a trend"},
        {"name": "bear_trend", "regime": "bear_trend", "risk": "catching falling knives"},
        {"name": "range_chop", "regime": "range", "risk": "whipsawed trend entries"},
        {"name": "low_vol_grind", "regime": "low_vol", "risk": "overtrading noise"},
        {"name": "high_vol_expansion", "regime": "high_vol", "risk": "size blowups"},
        {"name": "flash_crash", "regime": "crisis", "risk": "instant ruin"},
        {"name": "gap_open", "regime": "gap", "risk": "strike slippage through entries"},
        {"name": "news_spike", "regime": "high_vol", "risk": "wick-outs at the top"},
        {"name": "liquidity_vacuum", "regime": "crisis", "risk": "spread death"},
        {"name": "regime_whipsaw", "regime": "mixed", "risk": "overfit strategy death"},
        {"name": "gbm", "regime": "unknown", "risk": "baseline diffusion"},
    ]


__all__ = [
    "MarketParams",
    "MarketState",
    "PriceProcess",
    "MarketSimulator",
    "SCENARIO_NAMES",
    "make_process",
    "generate_candles",
    "scenario_catalog",
]
