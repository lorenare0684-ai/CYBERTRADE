"""Quotex wire constants (unofficial — community-documented protocol).

See ``docs/QUOTEX_PROTOCOL.md`` for provenance and the disclaimer that this
is reverse-engineered community knowledge, not an official API contract.
"""

from __future__ import annotations

# Endpoints ------------------------------------------------------------------
HTTP_BASE = "https://qxbroker.com"
HTTP_BASE_ALT = "https://quotex.com"
WS_URL = "wss://ws2.qxbroker.com/socket.io/?EIO=3&transport=websocket"
WS_URL_ALT = "wss://ws.qxbroker.com/socket.io/?EIO=3&transport=websocket"
SIGNIN_PATH = "/api/signin"
SIGNOUT_PATH = "/api/signout"
PROFILE_PATH = "/api/profile"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ORIGIN = "https://qxbroker.com"
REFERER = "https://qxbroker.com/en/trade"

# Socket.IO event names ------------------------------------------------------
EV_AUTHORIZATION = "authorization"
EV_ORDERS_OPEN = "orders/open"          # place a binary option
EV_ORDER_OPEN_ALT = "buyOption"         # legacy name used by older clients
EV_ORDERS_CANCEL = "orders/close"       # early sale / cancel window
EV_SELL_OPTION = "sellOption"
EV_BALANCE = "balance"
EV_CHANGE_BALANCE = "changeBalance"     # switch demo/real purse
EV_CANDLE_HISTORY = "candleHistory"
EV_CANDLE = "candle"
EV_SUBSCRIBE_CANDLE = "subscribeCandle"
EV_UNSUBSCRIBE_CANDLE = "unsubscribeCandle"
EV_INSTRUMENT = "instrument"
EV_PORTFOLIO = "portfolio"
EV_PROFIT = "profit"
EV_NOTIFICATION = "notification"
EV_USER_DATA = "userData"

# Known server → client events (parsed defensively)
SV_CANDLES = "candles"
SV_CANDLE_HISTORY = "candleHistory"
SV_CANDLE = "candle"
SV_TICK = "tick"
SV_BALANCE = "balance"
SV_BALANCE_UPDATE = "balanceUpdate"
SV_ORDER = "order"
SV_ORDERS = "orders"
SV_ORDER_RESULT = "orderResult"
SV_PORTFOLIO = "portfolio"
SV_PROFIT = "profit"
SV_ERROR = "error"
SV_NOTIFICATION = "notification"
SV_AUTH_SUCCESS = "authorization"

# Option types (optionType field on orders/open)
OPTION_TYPE_DIGITAL = 1
OPTION_TYPE_BINARY = 2
OPTION_TYPE_TURBO = 3

# Account kinds
ACCOUNT_DEMO = "PRACTICE"
ACCOUNT_REAL = "REAL"

DURATIONS = (5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)

# Timeframe → seconds used by candle subscriptions
CANDLE_TIMEFRAMES = {
    "5s": 5,
    "15s": 15,
    "30s": 30,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

__all__ = [
    "HTTP_BASE",
    "WS_URL",
    "SIGNIN_PATH",
    "USER_AGENT",
    "ORIGIN",
    "EV_AUTHORIZATION",
    "EV_ORDERS_OPEN",
    "EV_ORDER_OPEN_ALT",
    "EV_ORDERS_CANCEL",
    "EV_SELL_OPTION",
    "EV_BALANCE",
    "EV_CHANGE_BALANCE",
    "EV_CANDLE_HISTORY",
    "EV_CANDLE",
    "EV_SUBSCRIBE_CANDLE",
    "EV_UNSUBSCRIBE_CANDLE",
    "EV_INSTRUMENT",
    "EV_PORTFOLIO",
    "EV_PROFIT",
    "EV_NOTIFICATION",
    "OPTION_TYPE_DIGITAL",
    "OPTION_TYPE_BINARY",
    "ACCOUNT_DEMO",
    "ACCOUNT_REAL",
    "DURATIONS",
    "CANDLE_TIMEFRAMES",
]
