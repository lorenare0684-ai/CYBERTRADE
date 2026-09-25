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
    ARMED = "armed"            # live order flow permitted (gate passed)
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
    # -- forex --
    "EURUSD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 1},
    "GBPUSD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 56},
    "USDJPY": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 63},
    "AUDUSD": {"kind": "forex", "payout": 0.82, "tz": "UTC", "pip": 0.0001, "id": 40},
    "USDCAD": {"kind": "forex", "payout": 0.82, "tz": "UTC", "pip": 0.0001, "id": 61},
    "USDCHF": {"kind": "forex", "payout": 0.82, "tz": "UTC", "pip": 0.0001, "id": 62},
    "NZDUSD": {"kind": "forex", "payout": 0.82, "tz": "UTC", "pip": 0.0001, "id": 60},
    "AUDCAD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 36},
    "AUDCHF": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 37},
    "AUDJPY": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 38},
    "AUDNZD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 39},
    "CADCHF": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 41},
    "CADJPY": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 42},
    "CHFJPY": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 43},
    "EURAUD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 44},
    "EURCAD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 45},
    "EURCHF": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 46},
    "EURGBP": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 47},
    "EURJPY": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 48},
    "EURNZD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 49},
    "GBPAUD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 51},
    "GBPCAD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 52},
    "GBPCHF": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 53},
    "GBPJPY": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 54},
    "GBPNZD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001},
    "NZDCAD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001},
    "NZDCHF": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001},
    "NZDJPY": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 58},
    "EURSGD": {"kind": "forex", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 123},
    "USDSEK": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "USDNOK": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "USDDKK": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURSEK": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURNOK": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURDKK": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "USDPLN": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURPLN": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURHUF": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURCZK": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURRON": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURMXN": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "USDMXN": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURTRY": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "GBPTRY": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "USDTRY": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "EURZAR": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "GBPZAR": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "USDZAR": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "USDSGD": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "USDHKD": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    "USDINR": {"kind": "forex", "payout": 0.78, "tz": "UTC", "pip": 0.0001},
    # -- forex_otc --
    "EURUSD_otc": {"kind": "forex_otc", "payout": 0.85, "tz": "UTC", "pip": 0.0001, "id": 66},
    "GBPUSD_otc": {"kind": "forex_otc", "payout": 0.84, "tz": "UTC", "pip": 0.0001, "id": 86},
    "USDJPY_otc": {"kind": "forex_otc", "payout": 0.85, "tz": "UTC", "pip": 0.01, "id": 93},
    "AUDUSD_otc": {"kind": "forex_otc", "payout": 0.83, "tz": "UTC", "pip": 0.0001, "id": 71},
    "USDCAD_otc": {"kind": "forex_otc", "payout": 0.82, "tz": "UTC", "pip": 0.0001, "id": 91},
    "USDCHF_otc": {"kind": "forex_otc", "payout": 0.82, "tz": "UTC", "pip": 0.0001, "id": 92},
    "NZDUSD_otc": {"kind": "forex_otc", "payout": 0.81, "tz": "UTC", "pip": 0.0001, "id": 90},
    "AUDCAD_otc": {"kind": "forex_otc", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 67},
    "AUDCHF_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 68},
    "AUDJPY_otc": {"kind": "forex_otc", "payout": 0.81, "tz": "UTC", "pip": 0.01, "id": 69},
    "AUDNZD_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 70},
    "CADCHF_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 72},
    "CADJPY_otc": {"kind": "forex_otc", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 73},
    "CHFJPY_otc": {"kind": "forex_otc", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 74},
    "EURAUD_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 75},
    "EURCAD_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 76},
    "EURCHF_otc": {"kind": "forex_otc", "payout": 0.8, "tz": "UTC", "pip": 0.0001, "id": 77},
    "EURGBP_otc": {"kind": "forex_otc", "payout": 0.82, "tz": "UTC", "pip": 0.0001, "id": 78},
    "EURJPY_otc": {"kind": "forex_otc", "payout": 0.83, "tz": "UTC", "pip": 0.01, "id": 79},
    "EURNZD_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 80},
    "GBPAUD_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 81},
    "GBPCAD_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 82},
    "GBPCHF_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 83},
    "GBPJPY_otc": {"kind": "forex_otc", "payout": 0.84, "tz": "UTC", "pip": 0.01, "id": 84},
    "GBPNZD_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001},
    "NZDCAD_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001},
    "NZDCHF_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001},
    "NZDJPY_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.01, "id": 89},
    "EURSGD_otc": {"kind": "forex_otc", "payout": 0.79, "tz": "UTC", "pip": 0.0001, "id": 303},
    "USDSEK_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDNOK_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDDKK_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURSEK_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURNOK_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURDKK_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDPLN_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURPLN_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURHUF_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURCZK_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURRON_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURMXN_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDMXN_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURTRY_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "GBPTRY_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDTRY_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "EURZAR_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "GBPZAR_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDZAR_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDSGD_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDHKD_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDINR_otc": {"kind": "forex_otc", "payout": 0.76, "tz": "UTC", "pip": 0.0001},
    "USDARS_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001},
    "USDBDT_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001},
    "USDCOP_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001},
    "USDDZD_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001},
    "USDEGP_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001},
    "USDIDR_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001},
    "USDNGN_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001},
    "USDPHP_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001},
    "USDPKR_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001},
    "BRLUSD_otc": {"kind": "forex_otc", "payout": 0.75, "tz": "UTC", "pip": 0.0001, "id": 332},
    # -- crypto --
    "BTCUSD": {"kind": "crypto", "payout": 0.84, "tz": "UTC", "pip": 1.0},
    "ETHUSD": {"kind": "crypto", "payout": 0.84, "tz": "UTC", "pip": 0.1},
    "LTCUSD": {"kind": "crypto", "payout": 0.84, "tz": "UTC", "pip": 0.01},
    "XRPUSD": {"kind": "crypto", "payout": 0.84, "tz": "UTC", "pip": 0.001},
    "BCHUSD": {"kind": "crypto", "payout": 0.84, "tz": "UTC", "pip": 0.01},
    "BNBUSD": {"kind": "crypto", "payout": 0.84, "tz": "UTC", "pip": 0.01},
    "SOLUSD": {"kind": "crypto", "payout": 0.84, "tz": "UTC", "pip": 0.01},
    "DOGUSD": {"kind": "crypto", "payout": 0.84, "tz": "UTC", "pip": 0.001},
    # -- crypto_otc --
    "BTCUSD_otc": {"kind": "crypto_otc", "payout": 0.87, "tz": "UTC", "pip": 1.0, "id": 352},
    "ETHUSD_otc": {"kind": "crypto_otc", "payout": 0.86, "tz": "UTC", "pip": 0.1, "id": 360},
    "LTCUSD_otc": {"kind": "crypto_otc", "payout": 0.84, "tz": "UTC", "pip": 0.01},
    "XRPUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 364},
    "BCHUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.01, "id": 363},
    "BNBUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.01, "id": 362},
    "SOLUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.01},
    "DOGUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 353},
    "TONUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001},
    "DOTUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001},
    "ETCUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001},
    "ZECUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001},
    "TRUUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001},
    "LINUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001},
    "DASUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001},
    "FLOUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 356},
    "BONUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 358},
    "ATOUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 368},
    "ADAUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 376},
    "APTUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 377},
    "ARBUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 378},
    "AVAUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 379},
    "AXSUSD_otc": {"kind": "crypto_otc", "payout": 0.82, "tz": "UTC", "pip": 0.001, "id": 380},
    # -- metal --
    "XAUUSD": {"kind": "metal", "payout": 0.9, "tz": "UTC", "pip": 0.01, "id": 2},
    "XAGUSD": {"kind": "metal", "payout": 0.86, "tz": "UTC", "pip": 0.001, "id": 65},
    # -- metal_otc --
    "XAUUSD_otc": {"kind": "metal_otc", "payout": 0.85, "tz": "UTC", "pip": 0.01, "id": 169},
    "XAGUSD_otc": {"kind": "metal_otc", "payout": 0.83, "tz": "UTC", "pip": 0.001, "id": 167},
    # -- commodity --
    "USCrude": {"kind": "commodity", "payout": 0.82, "tz": "UTC", "pip": 0.01},
    "UKBrent": {"kind": "commodity", "payout": 0.82, "tz": "UTC", "pip": 0.01},
    # -- commodity_otc --
    "USCrude_otc": {"kind": "commodity_otc", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 165},
    "UKBrent_otc": {"kind": "commodity_otc", "payout": 0.8, "tz": "UTC", "pip": 0.01, "id": 164},
    # -- index --
    "AXJAUD": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 315},
    "CHIA50": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 328},
    "DJIUSD": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 317},
    "F40EUR": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 318},
    "FTSGBP": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 319},
    "GEREUR": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 316},
    "HSIHKD": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 320},
    "IBXEUR": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 321},
    "IT4EUR": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 326},
    "JPXJPY": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 327},
    "NDXUSD": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 322},
    "SPXUSD": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 323},
    "STXEUR": {"kind": "index", "payout": 0.82, "tz": "UTC", "pip": 1.0, "id": 325},
    # -- index_otc --
    "US30_otc": {"kind": "index_otc", "payout": 0.81, "tz": "UTC", "pip": 1.0},
    "US100_otc": {"kind": "index_otc", "payout": 0.81, "tz": "UTC", "pip": 1.0},
    "US500_otc": {"kind": "index_otc", "payout": 0.82, "tz": "UTC", "pip": 0.1},
    "JPN225_otc": {"kind": "index_otc", "payout": 0.8, "tz": "UTC", "pip": 1.0},
    "GER30_otc": {"kind": "index_otc", "payout": 0.8, "tz": "UTC", "pip": 1.0},
    # -- stock_otc --
    "MCD_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01, "id": 175},
    "MSFT_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01, "id": 176},
    "FB_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01, "id": 187},
    "INTC_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01, "id": 190},
    "AXP_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01, "id": 291},
    "BA_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01, "id": 292},
    "JNJ_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01, "id": 296},
    "PFE_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01, "id": 297},
    "AAPL_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "TSLA_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "AMZN_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "GOOGL_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "NFLX_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "NVDA_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "AMD_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "BABA_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "DIS_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "PYPL_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "ADBE_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "CRM_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "ORCL_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "IBM_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "JPM_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "V_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "MA_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "BAC_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "XOM_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "CVX_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "KO_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "PEP_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "WMT_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "NKE_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
    "SBUX_otc": {"kind": "stock_otc", "payout": 0.72, "tz": "UTC", "pip": 0.01},
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
