"""Backtesting subpackage."""

from __future__ import annotations

from .engine import Backtester, BacktestResult, SimTrade
from .optimize import OptimizationResult, Trial, WalkForwardOptimizer
from .report import BacktestReport, build_report, matrix_table
from .scenarios import GAUNTLET, describe_all, generate_gauntlet, scenario_names

__all__ = [
    "Backtester",
    "BacktestResult",
    "SimTrade",
    "BacktestReport",
    "build_report",
    "matrix_table",
    "GAUNTLET",
    "scenario_names",
    "generate_gauntlet",
    "describe_all",
    "OptimizationResult",
    "Trial",
    "WalkForwardOptimizer",
]
