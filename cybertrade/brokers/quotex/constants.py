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
# Provenance: live-broker captures mirrored by cleitonleonel/pyquotex
# (master, Sep-2026 — offline replay tests pin these shapes) and
# corroborated by zagmi/qxbroker + usmanch96/quotex-historical-data.
# CamelCase "subscribeCandle"/"candleHistory"/"changeBalance" names from
# older community docs are NOT answered by the venue — they are gone.
EV_AUTHORIZATION = "authorization"
EV_TICK = "tick"                                  # app heartbeat, ~every 5s
EV_INDICATOR_LIST = "indicator/list"              # post-auth bootstrap…
EV_DRAWING_LOAD = "drawing/load"
EV_PENDING_LIST = "pending/list"
EV_CHART_NOTIFICATION_GET = "chart_notification/get"
EV_INSTRUMENTS_GET = "instruments/get"            # …request the listing
EV_INSTRUMENTS_UPDATE = "instruments/update"      # realtime subscribe
EV_INSTRUMENTS_UNSUBSCRIBE = "instruments/unsubscribe"
EV_DEPTH_FOLLOW = "depth/follow"
EV_DEPTH_UNFOLLOW = "depth/unfollow"
EV_HISTORY_LOAD = "history/load"                  # candle history request
EV_HISTORY_SUBSCRIBE_ALL = "history/subscribe_all"
EV_ACCOUNT_CHANGE = "account/change"              # switch demo/real purse
EV_ORDERS_OPEN = "orders/open"          # place a binary option
EV_ORDER_OPEN_ALT = "buyOption"         # legacy name used by older clients
EV_ORDERS_CANCEL = "orders/close"       # early sale / cancel window
EV_SELL_OPTION = "sellOption"
EV_PORTFOLIO = "portfolio"
EV_PROFIT = "profit"
EV_NOTIFICATION = "notification"
EV_USER_DATA = "userData"

# Known server → client events (parsed defensively)
SV_S_AUTHORIZATION = "s_authorization"  # auth accepted (the real ack)
SV_AUTH_REJECT = "authorization/reject"  # auth refused — re-pair, don't retry
SV_AUTH_SUCCESS = SV_S_AUTHORIZATION
SV_INSTRUMENTS_LIST = "instruments/list"  # positional rows, binary attachment
SV_HISTORY_LOAD = "history/load"        # history reply (binary attachment)
SV_HISTORY_LIST_V2 = "history/list/v2"  # pushed candle batches (binary)
SV_CANDLE_GENERATED = "candle-generated"  # live closed-candle push
SV_TRADER_HISTORY = "trader/history"
SV_SENTIMENT = "sentiment"
SV_QUOTES = "quotes"  # synthesized: bare [[asset, ts, price, dir]] batches
SV_CANDLES = "candles"
SV_CANDLE_HISTORY = "candleHistory"  # legacy tolerance only
SV_CANDLE = "candle"                 # legacy tolerance only
SV_TICK = "tick"                     # legacy tolerance only
SV_BALANCE = "balance"               # {demoBalance, liveBalance, …}
SV_BALANCE_UPDATE = "balanceUpdate"
SV_ORDER = "order"
SV_ORDERS = "orders"
SV_ORDER_RESULT = "orderResult"
SV_PORTFOLIO = "portfolio"
SV_ASSETS_LIST = "assets_list"
# Every spelling the instrument listing has been seen under (community
# clients disagree; the dispatcher accepts them all).
INSTRUMENT_EVENTS = (
    "instruments/list",
    "instrument",
    "instruments",
    "assets_list",
    "assetList",
    "assets",
    "asset_list",
)
SV_PROFIT = "profit"
SV_ERROR = "error"
SV_NOTIFICATION = "notification"

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
    "HTTP_BASE_ALT",
    "WS_URL",
    "WS_URL_ALT",
    "SIGNIN_PATH",
    "USER_AGENT",
    "ORIGIN",
    "REFERER",
    "EV_AUTHORIZATION",
    "EV_TICK",
    "EV_INDICATOR_LIST",
    "EV_DRAWING_LOAD",
    "EV_PENDING_LIST",
    "EV_CHART_NOTIFICATION_GET",
    "EV_INSTRUMENTS_GET",
    "EV_INSTRUMENTS_UPDATE",
    "EV_INSTRUMENTS_UNSUBSCRIBE",
    "EV_DEPTH_FOLLOW",
    "EV_DEPTH_UNFOLLOW",
    "EV_HISTORY_LOAD",
    "EV_HISTORY_SUBSCRIBE_ALL",
    "EV_ACCOUNT_CHANGE",
    "EV_ORDERS_OPEN",
    "EV_ORDER_OPEN_ALT",
    "EV_ORDERS_CANCEL",
    "EV_SELL_OPTION",
    "EV_PORTFOLIO",
    "EV_PROFIT",
    "EV_NOTIFICATION",
    "SV_S_AUTHORIZATION",
    "SV_AUTH_REJECT",
    "SV_AUTH_SUCCESS",
    "SV_INSTRUMENTS_LIST",
    "SV_HISTORY_LOAD",
    "SV_HISTORY_LIST_V2",
    "SV_CANDLE_GENERATED",
    "SV_TRADER_HISTORY",
    "SV_SENTIMENT",
    "SV_QUOTES",
    "OPTION_TYPE_DIGITAL",
    "OPTION_TYPE_BINARY",
    "ACCOUNT_DEMO",
    "ACCOUNT_REAL",
    "DURATIONS",
    "CANDLE_TIMEFRAMES",
]
