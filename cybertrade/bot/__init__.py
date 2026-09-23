"""Bot subpackage: engine, survivor, watchdog, health."""

from __future__ import annotations

from .engine import TradingEngine
from .health import HealthMonitor, HealthSnapshot
from .survivor import Posture, Survivor, SurvivorDecision, scenario_expectations
from .watchdog import Anomaly, Heartbeat, Watchdog
from .drills import CRISIS_SCENARIOS, DrillStats, StressDrill, VERDICTS, run_gauntlet

__all__ = [
    "TradingEngine",
    "Survivor",
    "SurvivorDecision",
    "Posture",
    "scenario_expectations",
    "Watchdog",
    "Heartbeat",
    "Anomaly",
    "HealthMonitor",
    "HealthSnapshot",
    "CRISIS_SCENARIOS",
    "DrillStats",
    "StressDrill",
    "VERDICTS",
    "run_gauntlet",
]
