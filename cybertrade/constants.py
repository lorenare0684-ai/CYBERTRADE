"""Global constants: enums, asset catalog, timing, and theme-safe literals."""

from __future__ import annotations

from enum import Enum, IntEnum, auto


class Side(str, Enum):
    """Order side / signal direction."""

    CALL = "call"      # binary option: price up at expiry   (BUY)
    PUT = "put"        # binary option: price down at expiry (SELL)
    FLAT = "flat"      # no position / stand down

    @property
    def is_long(self) -> bool:
        return self is Side.CALL

    @property
    def is_trade(self) -> bool:
        return self in (Side.CALL, Side.PUT)

    def flipped(self) -> "Side":
        if self is Side.CALL:
            return Side.PUT
        if self is Side.PUT:
            return Side.CALL
        return Side.FLAT


class OrderType(str, Enum):
    BINARY = "binary"          # fixed-payout digital option
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderStatus(IntEnum):
    PENDING = auto()
    SUBMITTED = auto()
    ACCEPTED = auto()
    REJECTED = auto()
    CANCELLED = auto()
    EXPIRED = auto()
    WON = auto()               # binary settled in the money
    LOST = auto()              # binary settled out of the money
    REFUNDED = auto()          # draw / at the money — stake returned


class Timeframe(str, Enum):
    S5 = "5s"
    S15 = "15s"
    S30 = "30s"
    M1 = "1m"
    M2 = "2m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        return _TIMEFRAME_SECONDS[self]

    @classmethod
    def from_seconds(cls, seconds: int) -> "Timeframe":
        for tf, secs in _TIMEFRAME_SECONDS.items():
            if secs == seconds:
                return tf
        raise ValueError(f"unknown timeframe seconds={seconds}")


_TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.S5: 5,
    Timeframe.S15: 15,
    Timeframe.S30: 30,
    Timeframe.M1: 60,
    Timeframe.M2: 120,
    Timeframe.M3: 180,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
}


class MarketRegime(str, Enum):
    """Coarse market state used by the ensemble and the survivor playbook."""

    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    RANGE = "range"
    LOW_VOL = "low_vol"
    HIGH_VOL = "high_vol"
    CRISIS = "crisis"
    GAP = "gap"
    UNKNOWN = "unknown"


class EngineState(str, Enum):
    BOOT = "boot"
    DISARMED = "disarmed"      # wired up, refuses to trade
    ARMED = "armed"            # paper trading permitted
    LIVE = "live"              # real order flow (danger zone)
    DRAWDOWN_LOCK = "drawdown_lock"
    KILL = "kill"              # emergency stop
    SHUTDOWN = "shutdown"


class SignalStrength(IntEnum):
    NONE = 0
    WEAK = 1
    MODERATE = 2
    STRONG = 3
    EXTREME = 4


class RegimeType(IntEnum):
    """Internal numeric regime ids (fast comparisons)."""

    UNKNOWN = 0
    TREND_UP = 1
    TREND_DOWN = 2
    RANGE = 3
    VOL_EXPANSION = 4
    VOL_CONTRACTION = 5
    CRISIS = 6


# ---------------------------------------------------------------------------
# Asset catalog — Quotex-style symbol names (OTC pairs included).
# ---------------------------------------------------------------------------
ASSET_CATALOG: dict[str, dict[str, object]] = {
    "EURUSD_otc": {"kind": "forex_otc", "payout": 0.85, "tz": "UTC", "pip": 0.0001},
    "GBPUSD_otc": {"kind": "forex_otc", "payout": 0.84, "tz": "UTC", "pip": 0.0001},
    "USDJPY_otc": {"kind": "forex_otc", "payout": 0.85, "tz": "UTC", "pip": 0.01},
    "AUDUSD_otc": {"kind": "forex_otc", "payout": 0.83, "tz": "UTC", "pip": 0.0001},
    "USDCAD_otc": {"kind": "forex_otc", "payout": 0.82, "tz": "UTC", "pip": 0.0001},
    "USDCHF_otc": {"kind": "forex_otc", "payout": 0.82, "tz": "UTC", "pip": 0.0001},
    "NZDUSD_otc": {"kind": "forex_otc", "payout": 0.81, "tz": "UTC", "pip": 0.0001},
    "EURJPY_otc": {"kind": "forex_otc", "payout": 0.83, "tz": "UTC", "pip": 0.01},
    "GBPJPY_otc": {"kind": "forex_otc", "payout": 0.84, "tz": "UTC", "pip": 0.01},
    "EURGBP_otc": {"kind": "forex_otc", "payout": 0.82, "tz": "UTC", "pip": 0.0001},
    "AUDCAD_otc": {"kind": "forex_otc", "payout": 0.80, "tz": "UTC", "pip": 0.0001},
    "AUDJPY_otc": {"kind": "forex_otc", "payout": 0.81, "tz": "UTC", "pip": 0.01},
    "CADJPY_otc": {"kind": "forex_otc", "payout": 0.80, "tz": "UTC", "pip": 0.01},
    "CHFJPY_otc": {"kind": "forex_otc", "payout": 0.80, "tz": "UTC", "pip": 0.01},
    "EURCHF_otc": {"kind": "forex_otc", "payout": 0.80, "tz": "UTC", "pip": 0.0001},
    "AUDCHF_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001},
    "EURUSD": {"kind": "forex", "payout": 0.80, "tz": "UTC", "pip": 0.0001},
    "GBPUSD": {"kind": "forex", "payout": 0.80, "tz": "UTC", "pip": 0.0001},
    "USDJPY": {"kind": "forex", "payout": 0.80, "tz": "UTC", "pip": 0.01},
    "BTCUSD_otc": {"kind": "crypto_otc", "payout": 0.87, "tz": "UTC", "pip": 1.0},
    "ETHUSD_otc": {"kind": "crypto_otc", "payout": 0.86, "tz": "UTC", "pip": 0.1},
    "LTCUSD_otc": {"kind": "crypto_otc", "payout": 0.84, "tz": "UTC", "pip": 0.01},
    "XAUUSD_otc": {"kind": "metal_otc", "payout": 0.85, "tz": "UTC", "pip": 0.01},
    "XAGUSD_otc": {"kind": "metal_otc", "payout": 0.83, "tz": "UTC", "pip": 0.001},
    "US30_otc": {"kind": "index_otc", "payout": 0.82, "tz": "UTC", "pip": 1.0},
    "US100_otc": {"kind": "index_otc", "payout": 0.82, "tz": "UTC", "pip": 1.0},
    "US500_otc": {"kind": "index_otc", "payout": 0.82, "tz": "UTC", "pip": 0.1},
    "JPN225_otc": {"kind": "index_otc", "payout": 0.80, "tz": "UTC", "pip": 1.0},
    "GER30_otc": {"kind": "index_otc", "payout": 0.80, "tz": "UTC", "pip": 1.0},
}

DEFAULT_ASSETS: tuple[str, ...] = (
    "EURUSD_otc",
    "GBPUSD_otc",
    "USDJPY_otc",
    "BTCUSD_otc",
    "XAUUSD_otc",
    "US500_otc",
)

# Binary option expirations offered by Quotex-style brokers (seconds).
EXPIRATIONS: tuple[int, ...] = (5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)

# ---------------------------------------------------------------------------
# Network endpoints (unofficial — see docs/QUOTEX_PROTOCOL.md and DISCLAIMER).
# ---------------------------------------------------------------------------
QX_HTTP_BASE = "https://qxbroker.com"
QX_HTTP_BASE_ALT = "https://quotex.com"
QX_WS_URL = "wss://ws2.qxbroker.com/socket.io/?EIO=3&transport=websocket"
QX_WS_URL_ALT = "wss://ws.qxbroker.com/socket.io/?EIO=3&transport=websocket"
QX_SIGNIN_PATH = "/api/signin"
QX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
QX_ORIGIN = "https://qxbroker.com"

# Engine.IO v3 packet types.
EIO_OPEN = "0"
EIO_CLOSE = "1"
EIO_PING = "2"
EIO_PONG = "3"
EIO_MESSAGE = "4"
EIO_UPGRADE = "5"
EIO_NOOP = "6"

# Socket.IO packet types (appended to Engine.IO MESSAGE).
SIO_CONNECT = "0"
SIO_DISCONNECT = "1"
SIO_EVENT = "2"
SIO_ACK = "3"
SIO_ERROR = "4"
SIO_BINARY_EVENT = "5"
SIO_BINARY_ACK = "6"

DEFAULT_WS_PING_INTERVAL = 25.0
DEFAULT_WS_RECONNECT_DELAY = 3.0
DEFAULT_WS_MAX_RECONNECT_DELAY = 60.0

__all__ = [
    "Side",
    "OrderType",
    "OrderStatus",
    "Timeframe",
    "MarketRegime",
    "EngineState",
    "SignalStrength",
    "RegimeType",
    "ASSET_CATALOG",
    "DEFAULT_ASSETS",
    "EXPIRATIONS",
    "QX_HTTP_BASE",
    "QX_WS_URL",
    "QX_SIGNIN_PATH",
    "QX_USER_AGENT",
    "QX_ORIGIN",
    "EIO_OPEN",
    "EIO_CLOSE",
    "EIO_PING",
    "EIO_PONG",
    "EIO_MESSAGE",
    "SIO_CONNECT",
    "SIO_DISCONNECT",
    "SIO_EVENT",
    "SIO_ACK",
    "SIO_ERROR",
    "DEFAULT_WS_PING_INTERVAL",
]
