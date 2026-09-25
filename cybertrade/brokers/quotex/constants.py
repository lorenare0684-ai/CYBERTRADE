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
# Early sale / cancel: the venue's frame is ``orders/cancel`` with a
# ``ticket`` (the venue order id).  Older docs named ``sellOption`` /
# ``orders/close`` — the live venue answers neither (``orders/close`` is
# the *server → client* settlement event, not a request).
EV_ORDERS_CANCEL = "orders/cancel"
EV_SELL_OPTION = EV_ORDERS_CANCEL
EV_SETTINGS_APPLY = "settings/apply"    # chart/expiry settings before an order
EV_PORTFOLIO = "portfolio"
EV_PROFIT = "profit"
EV_NOTIFICATION = "notification"
EV_USER_DATA = "userData"

# Known server → client events (parsed defensively)
SV_S_AUTHORIZATION = "s_authorization"  # auth accepted (the real ack)
SV_AUTH_REJECT = "authorization/reject"  # auth refused — re-pair, don't retry
SV_S_ACCOUNT_CHANGE = "s_account/change"  # purse-switch confirm (s_ = server ack)
SV_AUTH_SUCCESS = SV_S_AUTHORIZATION
SV_INSTRUMENTS_LIST = "instruments/list"  # positional rows, binary attachment
SV_HISTORY_LOAD = "history/load"        # history reply (binary attachment)
SV_HISTORY_LIST_V2 = "history/list/v2"  # pushed candle batches (binary)
SV_CANDLE_GENERATED = "candle-generated"  # live closed-candle push
SV_TRADER_HISTORY = "trader/history"
SV_SENTIMENT = "sentiment"
SV_QUOTES = "quotes"  # synthesized: bare [[asset, ts, price, dir]] batches
SV_QUOTES_STREAM = "quotes/stream"  # live: 451-["quotes/stream"] + [[asset, ts, price, dir]]
SV_DEPTH_CHANGE = "depth/change"    # live: 451-["depth/change"] + [[asset, depth]]
QUOTE_EVENTS = (SV_QUOTES, SV_QUOTES_STREAM)
SV_CANDLES = "candles"
SV_CANDLE_HISTORY = "candleHistory"  # legacy tolerance only
SV_CANDLE = "candle"                 # legacy tolerance only
SV_TICK = "tick"                     # legacy tolerance only
SV_BALANCE = "balance"               # {demoBalance, liveBalance, …}
SV_BALANCE_UPDATE = "balanceUpdate"
SV_S_BALANCE_LIST = "s_balance/list"  # live (Sep-2026): purse balances after auth / account change
SV_SETTINGS_LIST = "settings/list"    # live: profile settings {id/uid, nickname, demoBalance, liveBalance, …}
BALANCE_EVENTS = (SV_BALANCE, SV_BALANCE_UPDATE, SV_S_BALANCE_LIST)
SV_ORDERS_OPENED_LIST = "orders/opened/list"  # live: open contracts snapshot after auth
SV_ORDERS_CLOSED_LIST = "orders/closed/list"  # live: recent settled contracts snapshot
SV_ORDER = "order"
SV_ORDERS = "orders"
SV_ORDER_RESULT = "orderResult"
# Order lifecycle (binary attachments — ``451-["s_orders/open",…]`` + raw
# frame).  The open ack echoes ``requestId`` and carries the venue ``id``
# (the *ticket* needed for sell-back); settlement arrives as ``deals``
# rows (``{deals: [{id, profit, closePrice, …}]}``) or ``orders/closed``.
SV_S_ORDERS_OPEN = "s_orders/open"
SV_ORDERS_OPENED = "orders/opened"
SV_S_ORDERS_CLOSE = "s_orders/close"
SV_ORDERS_CLOSED = "orders/closed"
SV_ORDERS_CLOSE = "orders/close"
SV_DEALS = "deals"
ORDER_OPEN_EVENTS = (SV_S_ORDERS_OPEN, "orders/open", SV_ORDERS_OPENED, SV_ORDERS_OPENED_LIST)
ORDER_CLOSE_EVENTS = (SV_S_ORDERS_CLOSE, SV_ORDERS_CLOSE, SV_ORDERS_CLOSED, SV_DEALS,
                      SV_ORDERS_CLOSED_LIST)
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

# Option types (optionType field on orders/open) — pyquotex parity:
#   1   = "TIME" contract, ``time`` is a period-aligned expiry timestamp
#   3   = fast option (same aligned expiry; what the trade tab sends by default)
#   100 = "TIMER" contract, ``time`` is the duration in seconds
# TIMER is the bot default: it works for OTC and non-OTC, every duration
# from 5s up, and expires exactly ``duration`` after the fill — which is
# what the local settlement clock assumes.
OPTION_TYPE_TIME = 1
OPTION_TYPE_FAST = 3
OPTION_TYPE_TIMER = 100
OPTION_TYPE_DIGITAL = OPTION_TYPE_TIME   # legacy aliases
OPTION_TYPE_BINARY = 2
OPTION_TYPE_TURBO = OPTION_TYPE_FAST
TIME_MODE_TIMER = "TIMER"
TIME_MODE_TIME = "TIME"

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
    "EV_SETTINGS_APPLY",
    "EV_PORTFOLIO",
    "EV_PROFIT",
    "EV_NOTIFICATION",
    "SV_S_AUTHORIZATION",
    "SV_AUTH_REJECT",
    "SV_S_ACCOUNT_CHANGE",
    "SV_AUTH_SUCCESS",
    "SV_INSTRUMENTS_LIST",
    "SV_HISTORY_LOAD",
    "SV_HISTORY_LIST_V2",
    "SV_CANDLE_GENERATED",
    "SV_TRADER_HISTORY",
    "SV_SENTIMENT",
    "SV_QUOTES",
    "SV_QUOTES_STREAM",
    "SV_DEPTH_CHANGE",
    "QUOTE_EVENTS",
    "SV_S_BALANCE_LIST",
    "SV_SETTINGS_LIST",
    "BALANCE_EVENTS",
    "SV_ORDERS_OPENED_LIST",
    "SV_ORDERS_CLOSED_LIST",
    "SV_S_ORDERS_OPEN",
    "SV_ORDERS_OPENED",
    "SV_S_ORDERS_CLOSE",
    "SV_ORDERS_CLOSED",
    "SV_DEALS",
    "ORDER_OPEN_EVENTS",
    "ORDER_CLOSE_EVENTS",
    "OPTION_TYPE_TIME",
    "OPTION_TYPE_FAST",
    "OPTION_TYPE_TIMER",
    "OPTION_TYPE_DIGITAL",
    "OPTION_TYPE_BINARY",
    "TIME_MODE_TIMER",
    "TIME_MODE_TIME",
    "ACCOUNT_DEMO",
    "ACCOUNT_REAL",
    "DURATIONS",
    "CANDLE_TIMEFRAMES",
]
