"""Market data: models, history, feeds, and synthetic regimes."""

from __future__ import annotations

from .feed import Feed, QuoteBook, ReplayFeed, SyntheticFeed
from .history import (
    CandleSeries,
    HistoryBuffer,
    MultiTimeframeBook,
    load_history,
    resample,
)
from .models import (
    AccountSnapshot,
    Candle,
    Fill,
    FLAT_SIGNAL,
    Order,
    Position,
    Settlement,
    Signal,
    Tick,
    TradeRecord,
    candles_to_series,
    confidence_to_strength,
    summarize_trades,
)
from .synthetic import (
    MarketParams,
    MarketSimulator,
    SCENARIO_NAMES,
    generate_candles,
    make_process,
    scenario_catalog,
)

__all__ = [
    "Tick",
    "Candle",
    "candles_to_series",
    "Signal",
    "FLAT_SIGNAL",
    "confidence_to_strength",
    "Order",
    "Fill",
    "Settlement",
    "Position",
    "AccountSnapshot",
    "TradeRecord",
    "summarize_trades",
    "CandleSeries",
    "MultiTimeframeBook",
    "HistoryBuffer",
    "resample",
    "load_history",
    "Feed",
    "SyntheticFeed",
    "ReplayFeed",
    "QuoteBook",
    "MarketParams",
    "MarketSimulator",
    "SCENARIO_NAMES",
    "generate_candles",
    "make_process",
    "scenario_catalog",
]
