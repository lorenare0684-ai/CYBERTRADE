"""Phase-20: crisis drills — chaos engineering for the defense stack.

Operators rehearse crashes before the market serves one.  A drill drives a
real scenario process (``data.synthetic`` — the same generators the gauntlet
backtest uses) through the live engine, tick by tick, into the exact seam
real quotes enter (``TradingEngine._on_tick``), while the whole defense
chain stays armed: regime detector, survivor postures, risk guards,
checkpoint, watchdog, and the Phase-19 lifeboat.  The book the drill shocks
is the one you were already holding when the storm hit.

Nothing here is theater: prices come from the named crisis generators, and
the report card is scored from what the defense stack *actually* did —
posture floor reached, contracts salvaged, kill switch tripped — not from
what we hope it would do.  A drill whose storm never engaged the defenses
says so honestly (``SURVIVED (untested)``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..constants import Side
from ..data.models import Order, Tick
from ..data.synthetic import make_process
from ..exceptions import DataError
from ..utils import timex
from .survivor import Posture

log = logging.getLogger("cybertrade.drills")

CRISIS_SCENARIOS: tuple[str, ...] = (
    "flash_crash",
    "gap_open",
    "news_spike",
    "liquidity_vacuum",
    "regime_whipsaw",
)

VERDICTS: tuple[str, ...] = (
    "SURVIVED (untested)",
    "SURVIVED (defended)",
    "SURVIVED (lifeboat)",
    "KILLED",
)


@dataclass
class DrillStats:
    """What the defense stack actually did during one drill."""

    name: str
    started_ts: float = field(default_factory=timex.now)
    ticks: int = 0
    posture_min: str = Posture.ATTACK
    salvaged: int = 0
    killed: bool = False
    finished_ts: float = 0.0

    def note_posture(self, posture: str) -> None:
        self.posture_min = Posture.most_defensive(self.posture_min, posture)

    @property
    def verdict(self) -> str:
        if self.killed:
            return "KILLED"
        if self.salvaged:
            return "SURVIVED (lifeboat)"
        if self.posture_min in (Posture.DEFENSE, Posture.LOCKDOWN):
            return "SURVIVED (defended)"
        return "SURVIVED (untested)"  # honest: the storm never engaged us

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ticks": self.ticks,
            "posture_min": self.posture_min,
            "salvaged": self.salvaged,
            "killed": self.killed,
            "verdict": self.verdict,
            "started_ts": self.started_ts,
            "finished_ts": self.finished_ts,
        }


class StressDrill:
    """One armed shock at a time; scenario prices, scale-free.

    ``shock_price(asset, base)`` returns ``base * scenario_price / p0`` — the
    scenario's path normalized to the asset's own scale — and advances the
    process one step.  Inactive or drained ⇒ identity.
    """

    def __init__(self, seed: int = 1337) -> None:
        self.seed = int(seed)
        self.name: str = ""
        self.stats: Optional[DrillStats] = None
        self._procs: Dict[str, Any] = {}
        self._states: Dict[str, Any] = {}
        self._p0: Dict[str, float] = {}
        self._left: Dict[str, int] = {}

    def arm(
        self,
        name: str,
        assets: Sequence[str],
        ticks: int = 400,
        seed: Optional[int] = None,
    ) -> DrillStats:
        if not assets:
            raise DataError("drill needs at least one asset")
        base_seed = self.seed if seed is None else int(seed)
        self._procs, self._states, self._p0, self._left = {}, {}, {}, {}
        for i, asset in enumerate(assets):
            proc = make_process(name, seed=base_seed + i)  # DataError on unknown
            state = proc.initial_state()
            self._procs[asset] = proc
            self._states[asset] = state
            self._p0[asset] = float(state.price)
            self._left[asset] = int(ticks)
        self.name = name
        self.stats = DrillStats(name=name)
        return self.stats

    def active(self) -> bool:
        return bool(self._procs) and any(n > 0 for n in self._left.values())

    def shock_price(self, asset: str, base: float) -> float:
        proc = self._procs.get(asset)
        if proc is None or self._left.get(asset, 0) <= 0:
            return float(base)
        state = self._states[asset]
        price = float(proc.step(state))
        self._left[asset] -= 1
        if self.stats is not None:
            self.stats.ticks += 1
        p0 = self._p0[asset] or 1.0
        return float(base) * (price / p0)


def run_gauntlet(
    engine: Any,
    *,
    scenarios: Sequence[str] = CRISIS_SCENARIOS,
    ticks: int = 400,
    seed: int = 1337,
    stake: float = 10.0,
    expiry_seconds: int = 30,
    book: int = 1,
    start_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Shock the live engine once per scenario with a fresh book open.

    The engine is driven tick-by-tick exactly as the feed would (the same
    ``_on_tick`` seam), virtual time advancing one bar per step so the
    regime detector sees real candles and expiries resolve inside the run.
    The pre-placed *book* is deliberate: the drill measures what the
    defenses do to risk you already hold, not whether the entry gate would
    have opened.  Returns one scored row per scenario.
    """
    asset = engine.feed.assets[0]
    rows: List[Dict[str, Any]] = []
    for k, name in enumerate(scenarios):
        # Re-anchor to the venue clock every storm: position expiries are
        # booked against the real clock at submit, so virtual time must
        # start there or pump() expires the book before the defenses look.
        now = float(start_ts if start_ts is not None else timex.now())
        # the book you were holding when the storm hit
        engine.broker.on_tick(Tick(asset=asset, price=1.10, ts=now))
        feed_book = engine.feed.book(asset)  # the detector's candle source
        booked = 0
        for _ in range(book):
            fill = engine.broker.submit(Order(
                asset=asset, side=Side.CALL, amount=stake,
                expiry_seconds=expiry_seconds, payout=0.85,
                strategy="drill", tag="",
            ))
            if fill is not None:
                booked += 1
        n0 = len(engine.oms.trades)
        stats = engine.drill.arm(name, assets=[asset], ticks=ticks,
                                 seed=seed + 97 * k)
        for i in range(ticks + expiry_seconds + 5):
            ts = now + i * 60.0  # one bar per step — the generators are per-bar physics
            engine._on_tick(Tick(asset=asset, price=1.10, ts=ts))
            marked = engine.broker.last_price(asset)  # post-shock mark
            if marked is not None:
                feed_book.on_price(marked, ts)  # same storm, detector's pipe
            try:
                engine.cycle(now=ts)
            except Exception as exc:  # noqa: BLE001 — kill ends the round
                if type(exc).__name__ == "KillSwitchEngaged":
                    stats.killed = True
                    engine.clear_kill()
                else:
                    raise
        row = engine.drill_report() or {}
        row["booked"] = booked
        row["pnl"] = round(
            sum(r.pnl for r in engine.oms.trades[n0:]), 2
        )
        rows.append(row)
    return rows
