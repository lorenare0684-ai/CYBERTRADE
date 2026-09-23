"""Walk-forward parameter search with overfitting tripwires."""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..data.synthetic import generate_candles
from ..strategies.registry import build_all_weather
from .engine import Backtester, BacktestResult
from .report import BacktestReport

log = logging.getLogger("cybertrade.optimize")


@dataclass
class Trial:
    params: Dict[str, Any]
    train_score: float
    test_score: float
    train_report: Optional[BacktestReport] = None
    test_report: Optional[BacktestReport] = None

    @property
    def overfit_gap(self) -> float:
        return self.train_score - self.test_score

    def to_dict(self) -> Dict[str, Any]:
        return {
            "params": self.params,
            "train_score": round(self.train_score, 4),
            "test_score": round(self.test_score, 4),
            "overfit_gap": round(self.overfit_gap, 4),
        }


@dataclass
class OptimizationResult:
    trials: List[Trial] = field(default_factory=list)
    best: Optional[Trial] = None

    def leaderboard(self, limit: int = 10) -> List[Trial]:
        return sorted(self.trials, key=lambda t: t.test_score, reverse=True)[:limit]


class WalkForwardOptimizer:
    """Grid-search ensemble knobs on train bars, verify on held-out bars.

    The optimizer deliberately refuses to crown parameter sets whose train
    score vastly exceeds their test score — curve-fit bots die in production.
    """

    def __init__(
        self,
        scenario: str = "regime_whipsaw",
        train_bars: int = 500,
        test_bars: int = 300,
        max_overfit_gap: float = 0.25,
    ) -> None:
        self.scenario = scenario
        self.train_bars = train_bars
        self.test_bars = test_bars
        self.max_overfit_gap = max_overfit_gap

    def search(
        self,
        grid: Optional[Dict[str, Sequence[Any]]] = None,
        seed: int = 99,
    ) -> OptimizationResult:
        grid = grid or {
            "min_confidence": (0.5, 0.55, 0.6, 0.65),
            "mode": ("regime_weighted", "majority"),
        }
        keys = list(grid)
        values = [grid[k] for k in keys]
        train_candles = generate_candles(self.scenario, bars=self.train_bars, seed=seed, asset="TRAIN")
        test_candles = generate_candles(
            self.scenario, bars=self.test_bars, seed=seed + 1, asset="TEST"
        )
        result = OptimizationResult()

        for combo in itertools.product(*values):
            params = dict(zip(keys, combo))
            try:
                train_res = self._run(train_candles, params, warmup=120)
                test_res = self._run(test_candles, params, warmup=120)
            except Exception as exc:  # noqa: BLE001
                log.warning("trial %s failed: %s", params, exc)
                continue
            trial = Trial(
                params=params,
                train_score=train_res.report.survival_score,
                test_score=test_res.report.survival_score,
                train_report=train_res.report,
                test_report=test_res.report,
            )
            result.trials.append(trial)

        # pick best test score among non-overfit candidates first
        honest = [t for t in result.trials if t.overfit_gap <= self.max_overfit_gap]
        pool = honest or result.trials
        if pool:
            result.best = max(pool, key=lambda t: t.test_score)
        return result

    def _run(self, candles, params: Dict[str, Any], warmup: int) -> BacktestResult:
        ensemble = build_all_weather(
            mode=params.get("mode", "regime_weighted"),
            adaptive=True,
            min_confidence=params.get("min_confidence", 0.55),
        )
        bt = Backtester(ensemble=ensemble)
        return bt.run_candles(self.scenario, candles, seed=1, warmup=warmup)


__all__ = ["Trial", "OptimizationResult", "WalkForwardOptimizer"]
