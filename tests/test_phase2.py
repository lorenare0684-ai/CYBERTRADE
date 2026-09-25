"""Phase-2 tests: divergence, correlation, Monte Carlo, calendar, alerts, plugins, scenarios."""

from __future__ import annotations

import math
import os
import random
import shutil
import tempfile
import time
import unittest

from cybertrade.bot.alerts import Alert, AlertCenter
from cybertrade.bot.calendar import (
    EconomicCalendar,
    NewsEvent,
    calendar_for_asset,
    estimate_fomc,
    first_friday,
    nfp_dates,
)
from cybertrade.config import AppConfig
from cybertrade.constants import MarketRegime, Side
from cybertrade.data.models import Candle
from tests.venue_stubs import VenueFeed, make_engine, venue_candles
from cybertrade.events import Topic, default_bus
from cybertrade.indicators.core import rsi
from cybertrade.indicators.divergence import (
    detect_divergence,
    divergence_score,
    find_pivots,
    last_divergence,
)
from cybertrade.regime.detector import RegimeReading
from cybertrade.risk.correlation import CorrelationMonitor
from cybertrade.risk.montecarlo import simulate, simulate_from_records
from cybertrade.strategies.base import Strategy, StrategyContext
from cybertrade.strategies.divergence import (
    CCIDivergenceFade,
    MacdHiddenDivergence,
    MultiTimeframeConfluence,
    RSIDivergenceEdge,
)
from cybertrade.strategies.plugins import PluginRegistry


def _zig(n: int, c: float = 100.0, a: float = 0.12) -> list:
    return [c + (a if i % 2 else -a) for i in range(n)]


def _candles_from(closes, tf: int = 60, ts0: float = 1700000000.0):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Candle(
            asset="EURUSD", timeframe_seconds=tf, open_ts=ts0 + i * tf,
            open=o, high=max(o, c) + 0.05, low=min(o, c) - 0.05,
            close=c, volume=5.0, closed=True,
        ))
    return out


class TestDivergenceCore(unittest.TestCase):
    """Textbook constructed cases — all four kinds, strict no-lookahead."""

    def test_regular_bull(self):
        close = [100.0] * 12 + [99, 98, 97, 96, 95, 96, 97, 98, 99, 100] \
            + [100.0] * 14 + [99, 98, 97, 95.5, 94.5, 95.5, 97, 98, 99, 100]
        osc = [50.0] * 12 + [45, 40, 35, 28, 22, 28, 35, 42, 48, 50] \
            + [50.0] * 14 + [48, 45, 42, 38, 36, 40, 44, 47, 49, 50]
        d = last_divergence(close, osc, left=3, right=3, within=25)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "regular_bull")
        self.assertTrue(d.is_bull and d.is_regular)
        self.assertEqual(d.side, "call")
        self.assertGreaterEqual(d.strength, 0.6)

    def test_regular_bear(self):
        close = [100.0] * 12 + [101, 102, 103, 104, 105, 104, 103, 102, 101, 100] \
            + [100.0] * 14 + [101, 102, 103, 104.5, 105.5, 104.5, 103, 102, 101, 100]
        osc = [50.0] * 12 + [55, 60, 65, 72, 78, 72, 65, 58, 52, 50] \
            + [50.0] * 14 + [52, 55, 58, 62, 64, 60, 56, 53, 51, 50]
        d = last_divergence(close, osc, left=3, right=3, within=25)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "regular_bear")
        self.assertEqual(d.side, "put")

    def test_hidden_bull(self):
        close = [100.0] * 12 + [99, 98, 97, 96, 95, 96, 97, 98, 99, 100] \
            + [100.0] * 14 + [99, 98.5, 98, 97.5, 97, 97.5, 98, 99, 99.5, 100]
        osc = [50.0] * 12 + [45, 40, 35, 28, 22, 28, 35, 42, 48, 50] \
            + [50.0] * 14 + [48, 42, 34, 26, 20, 26, 34, 42, 48, 50]
        d = last_divergence(close, osc, left=3, right=3, within=25)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "hidden_bull")
        self.assertFalse(d.is_regular)

    def test_hidden_bear(self):
        close = [100.0] * 12 + [101, 102, 103, 104, 105, 104, 103, 102, 101, 100] \
            + [100.0] * 14 + [101, 102, 103, 103.5, 104, 103.5, 103, 102, 101, 100]
        osc = [50.0] * 12 + [55, 60, 65, 72, 78, 72, 65, 58, 52, 50] \
            + [50.0] * 14 + [55, 62, 70, 76, 82, 76, 70, 62, 55, 50]
        d = last_divergence(close, osc, left=3, right=3, within=25)
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "hidden_bear")

    def test_pivot_no_lookahead(self):
        # pivot at i is only reported with right=3 bars of confirmation lag
        values = [5, 4, 3, 2, 1, 2, 3, 4, 5]
        piv = find_pivots(values, 2, 2, is_high=False)
        self.assertEqual([p.index for p in piv], [4])
        # truncating before confirm bars hides the pivot entirely
        hidden = find_pivots(values[:6], 2, 2, is_high=False)
        self.assertEqual(hidden, [])

    def test_pivots_are_strict(self):
        flat = [1.0] * 10
        self.assertEqual(find_pivots(flat, 2, 2, is_high=False), [])
        # equal neighbours never form a pivot (strict inequality)
        seq = [3, 2, 1, 1, 1, 2, 3]
        self.assertEqual(find_pivots(seq, 2, 2, is_high=False), [])

    def test_divergence_score_sign(self):
        close = [100.0] * 12 + [99, 98, 97, 96, 95, 96, 97, 98, 99, 100] \
            + [100.0] * 14 + [99, 98, 97, 95.5, 94.5, 95.5, 97, 98, 99, 100]
        osc = [50.0] * 12 + [45, 40, 35, 28, 22, 28, 35, 42, 48, 50] \
            + [50.0] * 14 + [48, 45, 42, 38, 36, 40, 44, 47, 49, 50]
        score = divergence_score(close, osc, within=25)
        self.assertGreater(score, 0.0)          # bullish disagreement
        self.assertLessEqual(abs(score), 1.0)


class TestDivergenceStrategies(unittest.TestCase):
    """RSI-emergent divergences on realistic noisy closes (no flat starts)."""

    def setUp(self):
        closes = _zig(24)
        closes += [99.2, 97.5, 96.2, 95.1, 95.0, 95.8, 97.4, 98.8, 99.6, 100.0]  # panic to 95.0
        closes += _zig(10)
        closes += [99.8, 99.4, 98.9, 98.3, 97.6, 96.9, 96.2, 95.6, 95.1, 94.6]   # grind to 94.6
        closes += [95.4, 96.6, 97.8, 98.8, 99.5, 100.0]
        self.closes = closes
        self.candles = _candles_from(closes)
        self.ctx = StrategyContext(
            asset="EURUSD", candles=self.candles, timeframe_seconds=60,
            ts=1700000000.0 + len(closes) * 60,
            regime=RegimeReading(regime=MarketRegime.RANGE),
        )

    def test_rsi_divergence_edge_fires_call(self):
        sig = RSIDivergenceEdge().generate(self.ctx)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, Side.CALL)
        self.assertIn("regular_bull", sig.reason)
        self.assertGreaterEqual(sig.confidence, 0.5)
        self.assertEqual(sig.strategy, "rsi_divergence")

    def test_cci_fade_sees_same_structure(self):
        sig = CCIDivergenceFade().generate(self.ctx)
        # CCI must at least survive the path without crashing (may or may not fire)
        self.assertTrue(sig is None or sig.side in (Side.CALL, Side.PUT))

    def test_mtf_confluence_runs_clean(self):
        # 5x resample needs >= 15 HTF bars -> 80+ base bars; our series qualifies
        sig = MultiTimeframeConfluence().generate(self.ctx)
        self.assertTrue(sig is None or sig.side in (Side.CALL, Side.PUT))

    def test_macd_hidden_only_in_trend(self):
        # hidden continuation refuses RANGE regime
        sig = MacdHiddenDivergence().generate(self.ctx)
        self.assertIsNone(sig)

    def test_min_bars_guard(self):
        short = StrategyContext(asset="EURUSD", candles=self.candles[:20],
                                timeframe_seconds=60, ts=0.0)
        self.assertIsNone(RSIDivergenceEdge().generate(short))

    def test_disabled_strategy_silent(self):
        s = RSIDivergenceEdge()
        s.enabled = False
        self.assertIsNone(s.generate(self.ctx))


class TestCorrelation(unittest.TestCase):
    def _feed(self, mon: CorrelationMonitor, n: int = 80, seed: int = 7):
        r = random.Random(seed)
        px = {"EURUSD": 1.0, "GBPUSD": 1.0, "BTCUSD": 1.0}
        for _ in range(n):
            ch = {a: r.gauss(0, 0.001) for a in px}
            ch["GBPUSD"] = ch["EURUSD"] * 0.9 + r.gauss(0, 0.0002)
            for a in px:
                px[a] *= 1 + ch[a]
                mon.on_price(a, px[a])

    def test_pairwise_and_clusters(self):
        mon = CorrelationMonitor(window=120, cluster_threshold=0.6)
        self._feed(mon)
        self.assertGreater(mon.correlation("EURUSD", "GBPUSD"), 0.8)
        self.assertLess(abs(mon.correlation("EURUSD", "BTCUSD")), 0.6)
        clusters = mon.clusters()
        self.assertEqual(clusters.get("EURUSD"), clusters.get("GBPUSD"))
        self.assertNotEqual(clusters.get("BTCUSD"), clusters.get("EURUSD"))

    def test_fallback_and_self_corr(self):
        mon = CorrelationMonitor()
        self.assertEqual(mon.correlation("A", "A"), 1.0)
        # unobserved asset falls back to its quote currency heuristic
        self.assertEqual(mon.cluster_of("EURUSD"), "USD")

    def test_cluster_exposure(self):
        mon = CorrelationMonitor()
        self.assertEqual(mon.cluster_exposure("USD", {"EURUSD": 1, "GBPUSD": 1}), 2)


class TestMonteCarlo(unittest.TestCase):
    def test_determinism_same_seed(self):
        pnls = [8.5, -10.0] * 30 + [8.5] * 12
        a = simulate(pnls, runs=50, horizon=40, seed=42)
        b = simulate(pnls, runs=50, horizon=40, seed=42)
        self.assertEqual(a.p50_terminal, b.p50_terminal)
        self.assertEqual(a.risk_of_ruin, b.risk_of_ruin)

    def test_ruin_bounds_ordering(self):
        good = [8.5] * 60 + [-10.0] * 40          # 60% WR at 0.85 payout
        bad = [8.5] * 35 + [-10.0] * 65           # 35% WR
        rg = simulate(good, runs=200, horizon=150, seed=3)
        rb = simulate(bad, runs=200, horizon=150, seed=3)
        self.assertGreaterEqual(rb.risk_of_ruin, rg.risk_of_ruin)
        self.assertLessEqual(rb.p50_terminal, rg.p50_terminal)
        self.assertGreaterEqual(rg.expectancy_per_trade, rb.expectancy_per_trade)

    def test_empty_sample_neutral(self):
        rep = simulate([], starting_balance=500.0)
        self.assertEqual(rep.p50_terminal, 500.0)
        self.assertEqual(rep.risk_of_ruin, 0.0)

    def test_from_records_and_verdict(self):
        class FakeTrade:
            pnl = 5.0

        rep = simulate_from_records([FakeTrade()] * 40 + [FakeTrade()] , starting_balance=1000.0,
                                    runs=30, horizon=20)
        self.assertIn(rep.verdict(), ("SURVIVABLE", "CAUTION", "DANGEROUS", "RUIN LIKELY"))
        d = rep.to_dict()
        self.assertIn("bands", d)

    def test_negative_expectancy_verdict_not_survivable(self):
        pnls = [8.5] * 30 + [-10.0] * 70
        rep = simulate(pnls, runs=300, horizon=250, seed=9, ruin_frac=0.5)
        self.assertNotEqual(rep.verdict(), "SURVIVABLE")


class TestCalendar(unittest.TestCase):
    def test_first_friday_rule(self):
        # 2026-09-04 is the first Friday of September 2026
        self.assertEqual(first_friday(2026, 9), 4)
        self.assertEqual(first_friday(2026, 1), 2)   # 2026-01-02

    def test_nfp_and_estimates_count(self):
        self.assertEqual(len(nfp_dates(2026)), 12)
        self.assertEqual(len(estimate_fomc(2026)), 8)
        cal = EconomicCalendar.estimate_year(2026)
        self.assertEqual(len(cal.events), 32)        # 12 NFP + 12 CPI + 8 FOMC
        self.assertTrue(all(e.estimated for e in cal.events))

    def test_blackout_window(self):
        ev = NewsEvent(ts=1700000000.0, currency="USD", title="NFP",
                       blackout_seconds=600)
        cal = EconomicCalendar(events=[ev])
        self.assertTrue(cal.is_blackout(1700000000.0))
        self.assertTrue(cal.is_blackout(1700000300.0, currency="USD"))
        self.assertFalse(cal.is_blackout(1700002000.0))
        self.assertFalse(cal.is_blackout(1700000300.0, currency="EUR"))

    def test_quote_currency_mapping(self):
        self.assertEqual(calendar_for_asset("EURUSD_otc"), "USD")
        self.assertEqual(calendar_for_asset("GBPJPY"), "JPY")

    def test_load_missing_file_estimates(self):
        cal = EconomicCalendar.load(path="/tmp/definitely-missing-cal.json",
                                    estimate_missing_year=2026)
        self.assertEqual(len(cal.events), 32)


class TestAlerts(unittest.TestCase):
    def setUp(self):
        self.fired = []
        self.center = AlertCenter(
            config=AppConfig().alerts,
            webhook=lambda a: self.fired.append(a),
        )
        self.center._config_webhook = True
        # bypass the thread hop: call _post_webhook synchronously via fire path
        self.center.config.enable_webhook = False
        self.center.config.webhook_url = ""
        self.center._webhook = self.fired.append
        self.center.attach()

    def tearDown(self):
        self.center.detach()

    def test_halt_fires_critical(self):
        default_bus.publish(Topic.RISK, {"event": "halt", "reason": "test halt"},
                            source="t")
        kinds = [a.kind for a in self.center.history()]
        self.assertIn("halt", kinds)

    def test_big_loss_threshold(self):
        default_bus.publish(Topic.SETTLE, {"pnl": -80.0, "asset": "EURUSD",
                                           "side": "put"}, source="t")
        default_bus.publish(Topic.SETTLE, {"pnl": -5.0, "asset": "EURUSD",
                                           "side": "put"}, source="t")
        losses = [a for a in self.center.history() if a.kind == "big_loss"]
        self.assertEqual(len(losses), 1)
        self.assertEqual(len(self.fired), 1)   # only the big one hit the webhook

    def test_news_blackout_alert(self):
        default_bus.publish(Topic.NEWS, {"blackout": True, "title": "NFP",
                                         "ts": 1700000000.0}, source="t")
        default_bus.publish(Topic.NEWS, {"blackout": True, "title": "NFP",
                                         "ts": 1700000000.0}, source="t")  # dedupe
        news = [a for a in self.center.history() if a.kind == "news_window"]
        self.assertEqual(len(news), 1)

    def test_recent_shape(self):
        self.center.fire(Alert(kind="x", title="T", body="B"))
        rec = self.center.recent(1)[0]
        for key in ("kind", "title", "body", "severity", "ts", "iso"):
            self.assertIn(key, rec)


class TestPlugins(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ct-plugins-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name: str, body: str) -> None:
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    def test_load_valid_plugin(self):
        self._write("myedge.py", (
            "from cybertrade.strategies.base import Strategy, StrategyContext\n"
            "class MyEdge(Strategy):\n"
            "    name = 'my_edge'\n"
            "    label = 'My Edge'\n"
            "    def decide(self, ctx):\n"
            "        return None\n"
            "STRATEGY_CLASS = MyEdge\n"
        ))
        reg = PluginRegistry(plugin_dir=self.dir)
        infos = reg.load_all()
        self.assertTrue(infos and infos[0].ok)
        self.assertIsNotNone(reg.get("my_edge"))
        self.assertEqual(len(reg.strategies()), 1)

    def test_broken_plugin_quarantined(self):
        self._write("bad.py", "raise RuntimeError('boom')\n")
        self._write("good.py", (
            "from cybertrade.strategies.base import Strategy\n"
            "class G(Strategy):\n"
            "    name = 'g_edge'\n"
            "    def decide(self, ctx): return None\n"
            "STRATEGY_CLASS = G\n"
        ))
        reg = PluginRegistry(plugin_dir=self.dir)
        infos = {i.name: i for i in reg.load_all()}
        self.assertFalse(infos["bad"].ok)
        self.assertIn("boom", infos["bad"].error)
        self.assertTrue(infos["good"].ok)
        self.assertIsNotNone(reg.get("g_edge"))

    def test_register_hook_and_duplicates(self):
        self._write("hook.py", (
            "from cybertrade.strategies.base import Strategy\n"
            "class H(Strategy):\n"
            "    name = 'g_edge'\n"
            "    def decide(self, ctx): return None\n"
            "def register():\n"
            "    return [H()]\n"
        ))
        self._write("good.py", (
            "from cybertrade.strategies.base import Strategy\n"
            "class G(Strategy):\n"
            "    name = 'g_edge'\n"
            "    def decide(self, ctx): return None\n"
            "STRATEGY_CLASS = G\n"
        ))
        reg = PluginRegistry(plugin_dir=self.dir)
        reg.load_all()
        # duplicate names are refused — exactly one 'g_edge'
        self.assertEqual(len([s for s in reg.strategies() if s.name == "g_edge"]), 1)

    def test_missing_dir_is_safe(self):
        reg = PluginRegistry(plugin_dir=os.path.join(self.dir, "nope"))
        self.assertEqual(reg.load_all(), [])


class TestVenueTape(unittest.TestCase):
    def test_tape_shape_is_stable(self):
        # the same shape/seed always yields the same path — a recorded tape
        a = venue_candles(n=40, shape="range", seed=9)
        b = venue_candles(n=40, shape="range", seed=9)
        self.assertEqual([c.close for c in a], [c.close for c in b])
        self.assertNotEqual([c.close for c in a],
                            [c.close for c in venue_candles(n=40, shape="trend_up", seed=9)])

    def test_feed_warmup_keeps_last_price(self):
        feed = VenueFeed(assets=["EURUSD"])
        feed.warmup()
        self.assertEqual(feed.last_price("EURUSD"),
                         feed.history("EURUSD", 1, 60)[-1].close)

    def test_feed_advance_appends_closed_bars(self):
        feed = VenueFeed(assets=["EURUSD"])
        last_ts = feed.history("EURUSD", 1, 60)[-1].open_ts
        feed.advance(3)
        candles = feed.history("EURUSD", 4, 60)
        self.assertEqual(len(candles), 4)
        self.assertGreater(candles[-1].open_ts, last_ts)


def _settled(i: int, won: bool):
    from cybertrade.constants import Side
    from cybertrade.data.models import Fill, Settlement

    fill = Fill(order_id=f"o{i}", asset="EURUSD_otc", side=Side.CALL,
                price=1.1, amount=10.0, payout=0.85)
    return Settlement(fill_id=fill.id, order_id=fill.order_id,
                      asset="EURUSD_otc", side=Side.CALL, strike=1.1,
                      expiry_price=1.2 if won else 1.0, stake=10.0,
                      payout=0.85 if won else 0.0, won=won)


class TestEnginePhase2Wiring(unittest.TestCase):
    def test_calendar_veto_and_cluster_plumbing(self):
        eng = make_engine(self, assets=["EURUSD"])
        eng.config.survivor.news_blackout_minutes = 5.0
        try:
            self.assertTrue(eng.alerts._subscribed)
            self.assertGreater(len(eng.calendar.events), 0)
            eng.calendar.events.append(
                NewsEvent(ts=time.time(), currency="USD", title="TEST", impact="high"))
            sig_ts = time.time()
            from cybertrade.data.models import Signal

            sig = Signal(asset="EURUSD", side=Side.CALL, confidence=0.9,
                         strategy="t", ts=sig_ts)
            ok = eng._try_execute(sig, RegimeReading())
            self.assertFalse(ok)                       # blackout vetoed
            kinds = [a["kind"] for a in eng.alerts.recent(3)]
            self.assertIn("news_window", kinds)
            self.assertEqual(eng.corr.cluster_of("EURUSD"), "USD")
        finally:
            eng.shutdown()

    def test_hub_montecarlo_payload(self):  # noqa: C901
        from cybertrade.web.server import EngineHub

        eng = make_engine(self, assets=["EURUSD"])
        try:
            hub = EngineHub(eng, eng.config)
            # no settled trades -> the lab reports the hole, never a sample
            empty = hub.montecarlo(runs=20, horizon=15)
            self.assertTrue(empty["empty"])
            self.assertEqual(empty["verdict"], "NO DATA")
            self.assertEqual(empty["n_trades"], 0)
            # with real settlements it bootstraps them
            for i in range(6):
                eng.oms.ledger.record_settlement(
                    _settled(i, won=(i % 2 == 0)), strategy="alpha")
            data = hub.montecarlo(runs=20, horizon=15)
            for key in ("verdict", "bands", "p05_terminal", "p50_terminal",
                        "risk_of_ruin", "source"):
                self.assertIn(key, data)
            self.assertEqual(data["source"], "trades")
            self.assertEqual(data["n_trades"], 6)
            state = hub.state()
            self.assertIn("equity_curve", state)
            self.assertIn("calendar", state)
            self.assertIn("alerts", state)
            self.assertIn("clusters", state)
        finally:
            eng.shutdown()


if __name__ == "__main__":
    unittest.main()
