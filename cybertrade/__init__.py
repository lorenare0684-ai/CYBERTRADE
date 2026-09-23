"""
CYBERTRADE // NEON PROTOCOL
===========================

An algorithmic trading terminal for Quotex-style binary/digital options with a
cyberpunk interface. Pure Python standard library — zero third-party runtime
dependencies.

Subpackages
-----------
- ``cybertrade.data``         Market data models, feeds, synthetic regimes
- ``cybertrade.indicators``   30+ technical indicators and candlestick patterns
- ``cybertrade.regime``       Market-regime detection (trend/range/vol/crisis)
- ``cybertrade.strategies``   20+ strategies plus regime-weighted ensemble
- ``cybertrade.risk``         Position sizing, drawdown governor, circuit breakers
- ``cybertrade.execution``    Broker interface, paper broker, OMS, ledger
- ``cybertrade.network``      RFC6455 WebSocket client, Engine.IO codec, HTTP
- ``cybertrade.brokers`       Quotex website/WS integration (unofficial)
- ``cybertrade.bot``          Trading engine, watchdog, all-weather survivor
- ``cybertrade.backtest``     Backtester, stress scenarios, reports, optimizer
- ``cybertrade.journal``      SQLite trade journal and analytics
- ``cybertrade.gui``          Tkinter cyberpunk desktop terminal
- ``cybertrade.web``          Stdlib web terminal (browser twin of the GUI)
- ``cybertrade.cli``          Command-line interface

Safety
------
Paper trading is the default. Live order flow against a broker may violate that
broker's Terms of Service and can lose money faster than it is made. Nothing in
this package is financial advice. See DISCLAIMER.md.
"""

from __future__ import annotations

__version__ = "1.0.0"
__codename__ = "NEON PROTOCOL"
__author__ = "CYBERTRADE"

APP_NAME = "CYBERTRADE"
APP_TAGLINE = "// NEON PROTOCOL — ALL-WEATHER TRADING SYSTEM"

__all__ = ["__version__", "__codename__", "__author__", "APP_NAME", "APP_TAGLINE"]
