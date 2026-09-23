"""Phase-24 tests — fill realism: storms widen entries, the limit is real.

`expected_slippage_bps` estimates adverse entry slip once per candidate
(stress × session liquidity × drill); the survivor vetoes anything above
`max_slippage_bps` (previously declared, never enforced), and the paper
broker applies the surviving value as an adverse strike offset so storm
wins must clear the handicap. Calm thick-tape fills keep the configured
base — zero drift outside adverse conditions.
"""

from __future__ import annotations

import inspect
import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.bot.survivor import Survivor
from cybertrade.config import AppConfig
from cybertrade.constants import Side
from cybertrade.data.feed import SyntheticFeed
from cybertrade.data.models import Order, Signal, Tick
from cybertrade.execution.paper import PaperBroker
from cybertrade.regime.detector import RegimeReading

from cybertrade.risk.slippage import CAP_BPS, expected_slippage_bps


class TestExpectedSlippage(unittest.TestCase):
    def test_calm_thick_stays_at_base(self):
        self.assertEqual(
            expected_slippage_bps(stress=0.0, session_liquidity="thick", base=0.5),
            0.5,
        )
        self.assertEqual(expected_slippage_bps(base=0.0, stress=0.0), 0.0)

    def test_adverse_conditions_widen_monotonically(self):
        calm = expected_slippage_bps(base=0.5)
        thin = expected_slippage_bps(base=0.5, session_liquidity="thin")
        drill = expected_slippage_bps(base=0.5, drill=True)
        storm = expected_slippage_bps(base=0.5, stress=1.0)
        self.assertGreater(thin, calm)
        self.assertGreater(drill, thin)
        self.assertGreater(storm, drill)
        self.assertGreaterEqual(storm, 40.0)
        self.assertLessEqual(
            expected_slippage_bps(stress=1.0, drill=True,
                                  session_liquidity="thin", base=0.5),
            CAP_BPS,
        )


class TestSurvivorSlipVeto(unittest.TestCase):
    def _sig(self):
        return Signal(asset="EURUSD_otc", side=Side.CALL, confidence=0.9,
                      strategy="t", expiry_seconds=60)

    def test_veto_when_slip_above_limit(self):
        s = Survivor()  # max_slippage_bps = 8.0
        d = s.evaluate(self._sig(), RegimeReading(), expected_slippage_bps=9.0,
                       is_otc=True, now=1e9)
        self.assertFalse(d.allow)
        self.assertTrue(any("slippage" in r for r in d.reasons), d.reasons)
        self.assertEqual(d.to_dict()["slippage_bps"], 9.0)

    def test_passes_within_limit(self):
        s = Survivor()
        d = s.evaluate(self._sig(), RegimeReading(), expected_slippage_bps=5.0,
                       is_otc=True, now=1e9)
        self.assertTrue(d.allow, d.reasons)
        self.assertEqual(d.slippage_bps, 5.0)

    def test_engine_estimates_and_forwards_slip(self):
        src = inspect.getsource(TradingEngine._try_execute)
        self.assertIn("expected_slippage_bps(", src)
        self.assertIn("expected_slippage_bps=slip_bps", src)
        self.assertIn("slippage_bps=decision.slippage_bps", src)


class TestPaperPerOrderSlip(unittest.TestCase):
    def _broker(self, default_bps=0.0):
        b = PaperBroker(1000.0, latency_ms=0, slippage_bps=default_bps, seed=1)
        b.connect()
        return b

    def test_storm_meta_displaces_strike_adversely(self):
        b = self._broker(default_bps=0.0)
        b.on_tick(Tick(asset="EURUSD_otc", price=1.1000, ts=1.0))
        o = Order(asset="EURUSD_otc", side=Side.CALL, amount=5.0,
                  expiry_seconds=60, payout=0.85, meta={"slippage_bps": 50.0})
        fill = b.submit(o)
        self.assertGreater(fill.price, 1.1000)        # adverse for CALL
        self.assertGreater(abs(fill.slippage), 0.0)   # recorded on the fill

    def test_falls_back_to_broker_default_without_meta(self):
        b = self._broker(default_bps=0.5)
        b.on_tick(Tick(asset="EURUSD_otc", price=1.1000, ts=2.0))
        o = Order(asset="EURUSD_otc", side=Side.CALL, amount=5.0,
                  expiry_seconds=60, payout=0.85, meta={})
        fill = b.submit(o)
        # 0.5bps ± 25% jitter on 1.10 ≈ 0.000055 — well under a pip
        self.assertLess(abs(fill.price - 1.1000), 1e-4)

    def test_oms_forwards_slippage_meta(self):
        from cybertrade.execution.oms import OrderManager

        class _B:
            name = "T"
            def payout_for(self, a, e): return 0.85
            def submit(self, order):
                from cybertrade.data.models import Fill
                return Fill(order_id=order.id, asset=order.asset, side=order.side,
                            price=1.0, amount=order.amount, payout=0.85, ts=0.0)
            def settle_due(self, now): return []
            def open_positions(self): return []

        class _R:
            def authorize(self, **kw): return None
            def on_open(self, order): return None
            def update_balance(self, balance): self.balance = balance

        class _L:
            balance = 1000.0
            def stake(self, amount, **kw): self.balance -= amount

        oms = OrderManager(broker=_B(), risk=_R(), ledger=_L())
        o = oms.submit("EURUSD_otc", Side.CALL, 5.0, 60, slippage_bps=12.5)
        self.assertIsNotNone(o)
        self.assertEqual(o.meta.get("slippage_bps"), 12.5)
        self.assertEqual(oms.risk.balance, 995.0)  # P32: escrow immediately reaches risk


if __name__ == "__main__":
    unittest.main()
