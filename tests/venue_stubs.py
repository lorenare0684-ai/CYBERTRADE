"""Shared live-mode test doubles for the live-only build.

This build has no paper broker, no dry-run broker and no synthetic market,
so the suite's venue is a *stub venue*:

- :class:`VenueFeed` — deterministic venue-shaped candles.  Crucially its
  ``is_synthetic`` is **False**, exactly like :class:`LiveQuotexFeed`: the
  engine's airlock refuses generator-backed feeds in live modes, and a test
  feed that lied about that would be testing a wiring bug instead of the
  product.
- :class:`VenueStub` — a :class:`Broker` that fills immediately, escrows the
  stake, settles at expiry and records every order, so engine/risk/OMS tests
  exercise the real production seam with no network and no invented market.
- :class:`FakeQuotexAPI` — just enough of the venue facade for the *real*
  ``LiveQuotexFeed`` + ``QuotexBroker`` pair to boot and trade, which is what
  the end-to-end live-path tests drive.

Nothing here fakes a fill price the engine didn't ask for, and nothing here
re-introduces paper semantics.
"""

from __future__ import annotations

import math
import os
import tempfile
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence

from cybertrade.data.feed import Feed
from cybertrade.data.history import MultiTimeframeBook
from cybertrade.data.models import (
    AccountSnapshot,
    Candle,
    Fill,
    Order,
    Position,
    Settlement,
    Tick,
)
from cybertrade.execution.broker import Broker

ASSET = "EURUSD_otc"
ASSETS = [ASSET, "BTCUSD_otc"]
START_TS = 1_700_000_000.0


def venue_candles(asset: str = ASSET, n: int = 160, bars: Optional[int] = None,
                  timeframe_seconds: int = 60,
                  start_ts: float = START_TS, start_price: float = 1.1000,
                  seed: int = 7, shape: str = "mixed") -> List[Candle]:
    """Deterministic, repeatable venue-shaped OHLC path.

    A recorded tape, not a market simulator: the same arguments always give
    the same candles, which is all the suite needs to be reproducible.
    ``shape`` picks the tape's character so regime/strategy tests can ask
    for a trending, falling or range-bound record the way a real session
    would supply one:
    ``trend_up`` / ``trend_down`` / ``range`` / ``mixed``.
    """
    if bars is not None:
        n = bars
    out: List[Candle] = []
    price = start_price
    for i in range(n):
        # deterministic wobble + slow drift, clamped positive
        if shape == "trend_up":
            drift = 0.00035
            wobble = 0.00012 * math.sin(i * 0.9 + seed)
        elif shape == "trend_down":
            drift = -0.00035
            wobble = 0.00012 * math.sin(i * 0.9 + seed)
        elif shape == "range":
            # full oscillation periods around a fixed centre: no net drift
            drift = 0.0
            wobble = 0.0007 * math.sin(2.0 * math.pi * i / 30.0 + seed * 0.01)
        else:  # mixed
            drift = 0.00004 * math.sin(i / 9.0 + seed) + 0.000012 * i
            wobble = 0.0009 * math.sin(i * 1.7 + seed) + 0.0004 * math.cos(i * 0.6 + seed)
        if shape == "range":
            # anchored to a fixed centre: oscillation, not accumulation
            close = max(0.01, start_price + wobble)
        else:
            close = max(0.01, price + drift + wobble)
        open_ = price
        # wick padding scales with the bar's own move, so a calm range tape
        # does not get an artificially fat candle range
        pad = min(0.0004, abs(close - open_) * 0.5 + 0.00003)
        high = max(open_, close) + pad
        low = min(open_, close) - pad
        out.append(Candle(
            asset=asset, timeframe_seconds=timeframe_seconds,
            open_ts=start_ts + i * timeframe_seconds,
            open=open_, high=high, low=low, close=close,
        ))
        price = close
    return out


class VenueFeed(Feed):
    """Deterministic venue feed — ``is_synthetic`` is False by design."""

    is_synthetic = False

    def __init__(self, assets: Optional[Sequence[str]] = None, n: int = 160,
                 timeframe_seconds: int = 60, start_price: float = 1.1000) -> None:
        super().__init__(list(assets or [ASSET]))
        self.timeframe_seconds = timeframe_seconds
        self._series: Dict[str, List[Candle]] = {
            a: venue_candles(a, n=n, timeframe_seconds=timeframe_seconds,
                             start_price=start_price + 0.01 * i)
            for i, a in enumerate(self.assets)
        }
        self._books: Dict[str, MultiTimeframeBook] = {}
        self._last: Dict[str, float] = {a: s[-1].close for a, s in self._series.items()}

    # -- feed api ----------------------------------------------------------
    def warmup(self) -> None:
        """Books are already full — a live venue does not need a warmup sim."""

    def history(self, asset: str, bars: int, timeframe_seconds: int) -> List[Candle]:
        series = self._series.get(asset) or []
        return list(series[-bars:]) if bars else list(series)

    def last_price(self, asset: str) -> Optional[float]:
        return self._last.get(asset)

    def book(self, asset: str) -> MultiTimeframeBook:
        if asset not in self._books:
            book = MultiTimeframeBook(asset, timeframes=[], maxlen=1000)
            for c in self._series.get(asset, []):
                book.on_price(c.close, c.open_ts)
            self._books[asset] = book
        return self._books[asset]

    def regime_hint(self, asset: str) -> str:
        return "live"

    def advance(self, bars: int = 1) -> None:
        """Append fresh closed candles (a venue tape keeps moving)."""
        for asset, series in self._series.items():
            last = series[-1]
            new = venue_candles(
                asset, n=bars, timeframe_seconds=self.timeframe_seconds,
                start_ts=last.open_ts + self.timeframe_seconds,
                start_price=last.close, seed=11,
            )
            series.extend(new)
            self._last[asset] = new[-1].close
            if asset in self._books:
                for c in new:
                    self._books[asset].on_price(c.close, c.open_ts)


class VenueStub(Broker):
    """In-memory venue: real fills, escrow, expiry settlement, sell-back.

    Price-driven like a venue, not like a paper simulator: the entry strike
    is the last quote seen, a CALL wins when the expiry quote is above the
    strike, a PUT when it is below, and an exactly-equal quote refunds the
    stake (ATM).  ``win_decider(asset, index)`` overrides that for tests
    that need to pin a win/loss sequence.
    """

    def __init__(self, balance: float = 1000.0, payout: float = 0.85,
                 user: str = "account-a", salvage_rate: float = 0.25,
                 name: str = "QUOTEX-DEMO") -> None:
        super().__init__()
        self.balance = float(balance)
        self.payout = float(payout)
        self.salvage_rate = float(salvage_rate)
        self._name = name
        self.api = SimpleNamespace(
            demo=True,
            session=SimpleNamespace(user_id=user, ssid="stub-session"),
            balance=SimpleNamespace(user_id=user),
        )
        self.orders: List[Order] = []
        self.fills: List[Fill] = []
        self.settlements: List[Settlement] = []
        self._positions: Dict[str, Position] = {}
        self._pending_salvage: List[Settlement] = []
        self._prices: Dict[str, float] = {}
        self._escrow = 0.0
        self._settle_index: Dict[str, int] = {}
        self.win_decider = None
        self._connected = False
        self.reject_next = False

    # -- identity ----------------------------------------------------------
    @property
    def name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    # -- market ------------------------------------------------------------
    def on_tick(self, tick: Tick) -> None:
        self._prices[tick.asset] = float(tick.price)

    def last_price(self, asset: str) -> Optional[float]:
        return self._prices.get(asset)

    def payout_for(self, asset: str, expiry_seconds: int) -> float:
        return self.payout

    # -- trading -----------------------------------------------------------
    def submit(self, order: Order) -> Fill:
        if self.reject_next:
            self.reject_next = False
            from cybertrade.exceptions import OrderRejected

            raise OrderRejected("stub rejected this order")
        if order.amount > self.balance + 1e-9:
            from cybertrade.exceptions import OrderRejected

            raise OrderRejected("insufficient funds")
        strike = order.limit_price or self._prices.get(order.asset) or 1.1
        # per-order adverse slippage from the OMS meta, like a venue fill
        slip_bps = float((order.meta or {}).get("slippage_bps") or 0.0)
        slip = strike * slip_bps / 10_000.0
        entry = strike + slip if order.side.is_long else strike - slip
        self.orders.append(order)
        idx = self._settle_index.get(order.asset, 0)
        self._settle_index[order.asset] = idx + 1
        fill = Fill(
            order_id=order.id, asset=order.asset, side=order.side,
            price=entry, amount=order.amount,
            payout=self.payout_for(order.asset, order.expiry_seconds),
            ts=order.ts, slippage=abs(slip), broker_id=f"stub-{len(self.fills) + 1}",
        )
        self.fills.append(fill)
        pos = Position(
            fill=fill, expiry_ts=order.ts + order.expiry_seconds,
            strategy=order.strategy or "test",
        )
        self._positions[pos.id] = pos
        self._escrow += order.amount
        self.balance -= order.amount
        return fill

    def account(self) -> AccountSnapshot:
        equity = self.balance + self._escrow
        return AccountSnapshot(
            balance=self.balance, equity=equity,
            margin_used=self._escrow, open_positions=len(self._positions),
            peak_balance=max(equity, 1000.0),
        )

    def open_positions(self) -> List[Position]:
        return list(self._positions.values())

    def settle_due(self, now: Optional[float] = None) -> List[Settlement]:
        from cybertrade.utils import timex

        now = timex.now() if now is None else now
        out: List[Settlement] = list(self._pending_salvage)
        self._pending_salvage.clear()
        for pos in list(self._positions.values()):
            if pos.expiry_ts > now:
                continue
            asset = pos.fill.asset
            strike = pos.fill.price
            expiry_price = self._prices.get(asset, strike)
            if self.win_decider is not None:
                won = bool(self.win_decider(asset, self._settle_index.get(asset, 1) - 1))
                refunded = False
            elif expiry_price > strike:
                won, refunded = (pos.fill.side.is_long, False)
            elif expiry_price < strike:
                won, refunded = (not pos.fill.side.is_long, False)
            else:
                won, refunded = (False, True)   # ATM refund, like the venue
            payout = pos.fill.amount * self.payout if won else 0.0
            if won:
                self.balance += pos.fill.amount * (1.0 + self.payout)
            elif refunded:
                self.balance += pos.fill.amount
            self._escrow -= pos.fill.amount
            del self._positions[pos.id]
            settlement = Settlement(
                fill_id=pos.fill.id, order_id=pos.fill.order_id,
                asset=asset, side=pos.fill.side,
                strike=strike, expiry_price=expiry_price,
                stake=pos.fill.amount, payout=self.payout if won else 0.0,
                won=won, refunded=refunded, ts=now,
            )
            self.settlements.append(settlement)
            out.append(settlement)
        return out

    def close_position(self, position_id: str) -> bool:
        """Venue sell-back: book the salvage, settle it when the contract
        would have expired (an early sale is still a settlement event)."""
        pos = self._positions.get(position_id)
        if pos is None:
            return False
        strike = pos.fill.price
        mark = self._prices.get(pos.fill.asset, strike)
        if (mark > strike) == pos.fill.side.is_long and mark != strike:
            # in the money at the mark: full modelled payout, no salvage haircut
            salvage, won = 0.0, True
        else:
            salvage, won = pos.fill.amount * self.salvage_rate, False
        if won:
            # stake returns with the full modelled payout
            self.balance += pos.fill.amount * (1.0 + self.payout)
        else:
            # stake is forfeit; only the salvage mark comes back
            self.balance += salvage
        self._escrow -= pos.fill.amount
        del self._positions[position_id]
        self._pending_salvage.append(Settlement(
            fill_id=pos.fill.id, order_id=pos.fill.order_id,
            asset=pos.fill.asset, side=pos.fill.side,
            strike=strike, expiry_price=mark,
            stake=pos.fill.amount, payout=self.payout if won else 0.0,
            won=won, salvage=salvage, ts=pos.expiry_ts,
        ))
        return True


class FakeQuotexAPI:
    """Venue facade double for the real LiveQuotexFeed + QuotexBroker pair."""

    def __init__(self, assets: Optional[Sequence[str]] = None, bars: int = 200,
                 balance: float = 1000.0, demo: bool = True,
                 open_trades: Optional[list] = None) -> None:
        self.assets_list = list(assets or [ASSET])
        self.bars = bars
        self.demo = demo
        self.connected = False
        self.session = SimpleNamespace(user_id="account-a", ssid="fake-ssid")
        self.balance = SimpleNamespace(user_id="account-a", amount=balance)
        self.subscribed: List[str] = []
        self.handlers: List = []
        self.listeners: List = []
        self._candles = {
            a: venue_candles(a, n=bars) for a in self.assets_list
        }
        self._open_trades = open_trades or []

    # -- session -----------------------------------------------------------
    def set_ssid(self, ssid: str, cookies: str = "") -> None:
        self.session.ssid = ssid

    def connect(self, authorize: bool = True) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    def login(self, username, password, is_demo=True) -> bool:  # pragma: no cover
        self.connected = True
        return True

    # -- market data -------------------------------------------------------
    def get_candles(self, asset, tf=60, count=200, wait=3.0):
        return list(self._candles.get(asset, [])[-count:])

    def last_price(self, asset):
        series = self._candles.get(asset) or []
        return series[-1].close if series else None

    def subscribe(self, asset, timeframe_seconds=60):
        self.subscribed.append(asset)

    def add_tick_handler(self, fn):
        self.handlers.append(fn)

    def add_listener(self, fn):
        self.listeners.append(fn)

    def payout_for(self, asset, expiry_seconds=60):
        return 0.85

    def request_instruments(self):
        return {}

    def account_snapshot(self):
        return AccountSnapshot(
            balance=self.balance.amount, equity=self.balance.amount,
            margin_used=0, open_positions=0, peak_balance=self.balance.amount,
        )

    def open_trades(self):
        return list(self._open_trades)


def make_engine(case, *, assets=None, broker=None, feed=None, mutate=None,
                durable: bool = False):
    """Build a booted LIVE engine on temp paths owned by ``case``.

    ``case`` is any ``unittest.TestCase``; the temp dir and the engine's
    shutdown are registered as cleanups so a failing test still tears the
    venue down.  ``mutate(cfg)`` lets a test bend config before boot.
    """
    from cybertrade.bot.engine import TradingEngine
    from cybertrade.config import AppConfig

    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    cfg = AppConfig()
    cfg.journal_path = os.path.join(tmp.name, "journal.db")
    cfg.calibration_path = os.path.join(tmp.name, "cal.json")
    cfg.operator_path = os.path.join(tmp.name, "operator.json")
    cfg.continuity_path = os.path.join(tmp.name, "continuity.json")
    cfg.heartbeat_path = os.path.join(tmp.name, "heartbeat.json")
    if assets is not None:
        cfg.strategy.universe = list(assets)
    if mutate is not None:
        mutate(cfg)
    engine = TradingEngine(
        cfg,
        feed=feed if feed is not None else VenueFeed(assets=cfg.strategy.universe),
        broker=broker if broker is not None else VenueStub(),
        durable=durable,
    )
    engine.boot()
    case.addCleanup(engine.shutdown)
    return engine


__all__ = [
    "ASSET", "ASSETS", "START_TS", "venue_candles",
    "VenueFeed", "VenueStub", "FakeQuotexAPI", "make_engine",
]
