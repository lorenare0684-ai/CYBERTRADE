"""Risk manager, sizing, paper broker, OMS, ledger, watchdog tests."""

from __future__ import annotations

import time
import unittest

from cybertrade.config import RiskConfig
from cybertrade.constants import Side
from cybertrade.data.models import Order, Tick
from cybertrade.exceptions import OrderRejected, RiskRejection
from cybertrade.execution.ledger import Ledger
from cybertrade.execution.oms import OrderManager
from cybertrade.risk.manager import RiskManager
from cybertrade.risk.sizing import (
    confidence_scaled,
    drawdown_scaled,
    fixed_fraction,
    kelly_scaled,
    stake_round,
    vol_scaled,
)
from cybertrade.bot.watchdog import Anomaly, Watchdog
from tests.venue_stubs import VenueStub


class TestSizing(unittest.TestCase):
    def test_fixed_fraction(self):
        d = fixed_fraction(1000, 0.01, 1, 50)
        self.assertAlmostEqual(d.stake, 10.0)

    def test_vol_scaled_shrinks_in_stress(self):
        calm = vol_scaled(1000, 0.01, 0.0004, 0.0006, 1, 50)
        hot = vol_scaled(1000, 0.01, 0.004, 0.0006, 1, 50)
        self.assertLess(hot.stake, calm.stake)

    def test_kelly(self):
        d = kelly_scaled(1000, 0.6, 0.85, 1, 50, kelly_mult=0.25)
        self.assertGreater(d.stake, 0)

    def test_drawdown_scale(self):
        self.assertLess(drawdown_scaled(20, 0.3, 0.35), 20.0)

    def test_confidence_scale(self):
        self.assertGreater(confidence_scaled(10, 0.9), confidence_scaled(10, 0.4))

    def test_round(self):
        self.assertEqual(stake_round(10.7, 1.0), 10.0)


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager(RiskConfig())
        self.rm.reset_day(1000.0, ts=1_000_000.0)

    def _check(self, **kw):
        base = dict(asset="EURUSD_otc", side=Side.CALL, stake=10, payout=0.85,
                    confidence=0.7, strategy="s", cluster="USD", now=1_000_100.0)
        base.update(kw)
        return self.rm.check(**base)

    def test_passes_sane_trade(self):
        self.assertTrue(self._check().all_ok)

    def test_kill_switch_blocks(self):
        self.rm.engage_kill("test")
        self.assertFalse(self._check().all_ok)

    def test_payout_floor(self):
        self.assertFalse(self._check(payout=0.1).all_ok)

    def test_stake_band(self):
        self.assertFalse(self._check(stake=10_000).all_ok)

    def test_concurrent_cap(self):
        order = Order("EURUSD_otc", Side.CALL, 10.0)
        for _ in range(self.rm.config.max_concurrent):
            self.rm.on_open(order)
        result = self._check()
        self.assertFalse(result.all_ok)
        self.assertIn(result.first_failure().name, ("open_count", "per_asset"))

    def test_authorize_raises(self):
        with self.assertRaises(RiskRejection):
            self.rm.authorize("EURUSD_otc", Side.CALL, 10, 0.1, 0.7, strategy="s")

    def test_cooldown_after_losses(self):
        order = Order("EURUSD_otc", Side.CALL, 10.0)
        for _ in range(3):
            self.rm.on_open(order)
            self.rm.on_close(order, won=False, pnl=-10, balance=900)
        result = self._check(now=1_000_200.0)
        self.assertFalse(result.all_ok)
        names = [c.name for c in result.failures]
        self.assertIn("cooldown", names)

    def test_rate_limits_roll(self):
        rm = RiskManager(RiskConfig(max_trades_per_hour=2))
        rm.reset_day(1000, ts=100.0)
        order = Order("A", Side.CALL, 1.0)
        for _ in range(2):
            rm.on_open(order)
        blocked = rm.check(asset="B", side=Side.PUT, stake=1, payout=0.85,
                           confidence=0.7, strategy="s", cluster="X", now=200.0)
        self.assertFalse(blocked.all_ok)
        allowed = rm.check(asset="B", side=Side.PUT, stake=1, payout=0.85,
                           confidence=0.7, strategy="s", cluster="X", now=200.0 + 4000.0)
        self.assertTrue(allowed.all_ok)

    def test_drawdown_lock(self):
        rm = RiskManager(RiskConfig(starting_balance=1000, max_daily_loss_frac=0.05,
                                    max_total_drawdown_frac=0.5))
        rm.reset_day(1000, ts=0.0)
        order = Order("A", Side.CALL, 1.0)
        rm.on_open(order)
        rm.on_close(order, won=False, pnl=-80, balance=920, )
        result = rm.check(asset="B", side=Side.PUT, stake=1, payout=0.85,
                          confidence=0.7, strategy="s", cluster="X", now=10.0)
        self.assertFalse(result.all_ok)

    def test_simple_trackers(self):
        rm = RiskManager(RiskConfig())
        rm.reset_day(100, ts=0.0)
        rm.on_open_simple(5, "A")
        rm.on_close_simple(True, 5, 105, asset="A", now=1.0)
        self.assertEqual(rm.state.open_count, 0)

    def test_size_stake_never_zero(self):
        d = self.rm.size_stake(1000, 0.85, 0.6, 0.5, 0.0)
        self.assertGreater(d.stake, 0)


class TestVenueBroker(unittest.TestCase):
    def setUp(self):
        self.b = VenueStub(balance=1000.0)
        self.b.connect()
        self.b.on_tick(Tick("EURUSD_otc", 1.1, bid=1.1, ask=1.1))

    def test_fill_and_settle_win(self):
        order = Order("EURUSD_otc", Side.CALL, 10.0, expiry_seconds=60, payout=0.85)
        fill = self.b.submit(order)
        self.assertEqual(self.b.account().open_positions, 1)
        self.b.on_tick(Tick("EURUSD_otc", 1.2))
        sets = self.b.settle_due(now=fill.ts + 61)
        self.assertEqual(len(sets), 1)
        self.assertTrue(sets[0].won)
        self.assertAlmostEqual(self.b.account().balance, 1000 + 8.5)

    def test_insufficient_funds(self):
        order = Order("EURUSD_otc", Side.CALL, 5000.0, payout=0.85)
        with self.assertRaises(OrderRejected):
            self.b.submit(order)

    def test_payout_for_asset(self):
        p = self.b.payout_for("EURUSD_otc", 60)
        self.assertGreater(p, 0.5)

    def test_put_wins_when_price_falls(self):
        order = Order("EURUSD_otc", Side.PUT, 10.0, expiry_seconds=60, payout=0.85)
        fill = self.b.submit(order)
        self.b.on_tick(Tick("EURUSD_otc", 1.0))
        sets = self.b.settle_due(now=fill.ts + 61)
        self.assertTrue(sets[0].won)
        self.assertAlmostEqual(self.b.account().balance, 1000 + 8.5)

    def test_atm_refunds_the_stake(self):
        order = Order("EURUSD_otc", Side.CALL, 10.0, expiry_seconds=60, payout=0.85)
        fill = self.b.submit(order)
        self.b.on_tick(Tick("EURUSD_otc", 1.1))
        sets = self.b.settle_due(now=fill.ts + 61)
        self.assertTrue(sets[0].refunded)
        self.assertEqual(sets[0].pnl, 0.0)
        self.assertAlmostEqual(self.b.account().balance, 1000.0)


class TestOMS(unittest.TestCase):
    def test_full_lifecycle(self):
        rm = RiskManager(RiskConfig())
        b = VenueStub(balance=1000.0)
        b.connect()
        b.on_tick(Tick("EURUSD_otc", 1.1))
        oms = OrderManager(b, rm)
        order = oms.submit("EURUSD_otc", Side.CALL, 10, 60, payout=0.85,
                           confidence=0.8, strategy="test", cluster="USD")
        self.assertIsNotNone(order)
        records = oms.pump(now=order.to_expiry + 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(oms.trades), 1)

    def test_risk_reject_returns_none(self):
        rm = RiskManager(RiskConfig())
        b = VenueStub(balance=1000.0)
        b.connect()
        b.on_tick(Tick("EURUSD_otc", 1.1))
        oms = OrderManager(b, rm)
        order = oms.submit("EURUSD_otc", Side.CALL, 10, 60, payout=0.05,
                           confidence=0.8, strategy="test")
        self.assertIsNone(order)


class TestLedger(unittest.TestCase):
    def test_bookkeeping(self):
        led = Ledger(1000)
        led.stake(10, ref="x")
        self.assertEqual(led.balance, 990)
        led.deposit(18.5, ref="x")
        self.assertEqual(led.balance, 1008.5)
        self.assertEqual(led.net_pnl(), 8.5)


class TestWatchdog(unittest.TestCase):
    def test_stale_heartbeat(self):
        wd = Watchdog(max_stale_seconds=0.01)
        wd.beat("engine")
        time.sleep(0.02)
        found = wd.sweep()
        self.assertTrue(any(a.kind == "stale_heartbeat" for a in found))

    def test_price_jump_detector(self):
        kills = []
        wd = Watchdog(kill_callback=kills.append)
        a = wd.check_price_jump("A", 0.5, 1.0, 0.0001)
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "price_jump")


if __name__ == "__main__":
    unittest.main()
