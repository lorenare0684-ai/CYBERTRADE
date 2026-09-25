"""Exception hierarchy for CYBERTRADE."""

from __future__ import annotations


class CybertradeError(Exception):
    """Base class for every error raised inside the package."""


class ConfigError(CybertradeError):
    """Invalid or corrupt configuration."""


class DataError(CybertradeError):
    """Malformed or missing market data."""


class FeedError(DataError):
    """A market-data feed failed or misbehaved."""


class IndicatorError(CybertradeError):
    """Bad indicator parameters or input series."""


class StrategyError(CybertradeError):
    """Strategy misconfiguration or runtime failure."""


class RiskRejection(CybertradeError):
    """An order was vetoed by the risk manager (this is the system working)."""

    def __init__(self, reason: str, code: str = "RISK") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class ExecutionError(CybertradeError):
    """Order routing / broker failure."""


class BrokerError(ExecutionError):
    """Generic broker-side failure."""


class BrokerAuthError(BrokerError):
    """Login rejected / session expired."""


class BrokerConnectionError(BrokerError):
    """Transport failure (DNS, TLS, socket, websocket)."""


class ProtocolError(CybertradeError):
    """Wire-protocol parse/serialize failure."""


class NetworkError(CybertradeError):
    """Low-level network failure."""


class OrderRejected(ExecutionError):
    """Broker refused an order."""

    def __init__(self, reason: str, code: str = "REJECTED") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class BacktestError(CybertradeError):
    """Backtest setup or execution failure."""


class JournalError(CybertradeError):
    """SQLite journal failure."""


class KillSwitchEngaged(CybertradeError):
    """Emergency shutdown triggered; every trading path must stop."""


__all__ = [
    "CybertradeError",
    "ConfigError",
    "DataError",
    "FeedError",
    "IndicatorError",
    "StrategyError",
    "RiskRejection",
    "ExecutionError",
    "BrokerError",
    "BrokerAuthError",
    "BrokerConnectionError",
    "ProtocolError",
    "NetworkError",
    "OrderRejected",
    "BacktestError",
    "JournalError",
    "KillSwitchEngaged",
]
