"""Phase-19 tests — the lifeboat: crisis salvage of open positions.

Binaries cannot stop out — they ride to expiry. When Survivor screams
LOCKDOWN the lifeboat sells every open contract back at the salvage mark:
losing contracts recover `salvage_rate` of stake (the venue's sell-back
quote), and the settlement flows through settle_due like an expiry — no
special paths downstream.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

from cybertrade.constants import Side
from cybertrade.data.models import Order, Settlement, Tick


class TestSettlementSalvage(unittest.TestCase):
    def _set(self, won: bool, refunded: bool = False, salvage: float = 0.0) -> Settlement:
        return Settlement(
            fill_id="f", order_id="o", asset="EURUSD_otc", side=Side.CALL,
            strike=1.0, expiry_price=1.1, stake=10.0, payout=0.85,
            won=won, refunded=refunded, salvage=salvage,
        )

    def test_salvage_softens_the_loss(self):
        s = self._set(won=False, salvage=2.5)
        self.assertAlmostEqual(s.pnl, -7.5)        # -stake + salvage
        self.assertAlmostEqual(s.returned, 2.5)    # cash in = stake + pnl

    def test_unsalvaged_cases_unchanged(self):
        self.assertAlmostEqual(self._set(won=True).returned, 18.5)
        self.assertAlmostEqual(self._set(won=False).pnl, -10.0)
        self.assertAlmostEqual(self._set(won=False, refunded=True).returned, 10.0)


class TestPaperSalvage(unittest.TestCase):
    def setUp(self):
        from cybertrade.execution.paper import PaperBroker

        self.b = PaperBroker(starting_balance=1000.0, salvage_rate=0.25)
        self.b.connect()
        self.b.on_tick(Tick(asset="EURUSD_otc", price=1.0))

    def _order(self, amount=10.0):
        return Order(asset="EURUSD_otc", side=Side.CALL, amount=amount,
                     expiry_seconds=60, payout=0.85, strategy="s", tag="")

    def test_close_loses_salvages_and_drains(self):
        self.b.submit(self._order())
        self.b.on_tick(Tick(asset="EURUSD_otc", price=0.5))  # mark against us
        self.assertEqual(len(self.b.open_positions()), 1)
        self.assertTrue(self.b.close_position(self.b.open_positions()[0].id))
        self.assertEqual(len(self.b.open_positions()), 0)
        out = self.b.settle_due()
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].won)
        self.assertAlmostEqual(out[0].salvage, 2.5)      # 25% of 10
        self.assertAlmostEqual(out[0].pnl, -7.5)

    def test_winner_banks_full_modeled_payout(self):
        self.b.submit(self._order())
        self.b.on_tick(Tick(asset="EURUSD_otc", price=2.0))  # mark for us
        self.assertTrue(self.b.close_position(self.b.open_positions()[0].id))
        out = self.b.settle_due()
        self.assertTrue(out[0].won)
        self.assertAlmostEqual(out[0].salvage, 0.0)
        self.assertAlmostEqual(out[0].pnl, 8.5)

    def test_unknown_position_is_false(self):
        self.assertFalse(self.b.close_position("nope"))
        self.assertEqual(self.b.settle_due(), [])


class TestVenueSalvage(unittest.TestCase):
    def test_sell_option_wire_plus_local_mark(self):
        from cybertrade.brokers.quotex.adapter import QuotexBroker

        sold = []

        class Api:
            connected = True
            demo = True
            session = SimpleNamespace(ssid="QX.s", host="h", demo=True)

            def add_listener(self, fn):
                pass

            def last_price(self, asset):
                return 0.5

            def sell_option(self, order_id):
                sold.append(order_id)

        b = QuotexBroker(Api(), allow_orders=True, salvage_rate=0.25)
        from cybertrade.data.models import Fill, Position

        fill = Fill(order_id="o1", asset="EURUSD_otc", side=Side.CALL,
                    price=1.0, amount=10.0, payout=0.85, ts=0.0, broker_id="req9")
        pos = Position(fill=fill, expiry_ts=1e12, strategy="s")
        b._positions[pos.id] = pos
        self.assertTrue(b.close_position(pos.id))
        self.assertEqual(sold, ["req9"])                 # venue wire fired
        out = b.settle_due()
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].salvage, 2.5)
        self.assertFalse(b.close_position(pos.id))       # gone


class TestLifeboat(unittest.TestCase):
    def test_lockdown_salvages_all(self):
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.config import AppConfig
        from cybertrade.data.feed import SyntheticFeed
        from cybertrade.execution.paper import PaperBroker

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.journal_path = os.path.join(tmp, "journal.db")
            cfg.calibration_path = os.path.join(tmp, "cal.json")
            b = PaperBroker(starting_balance=1000.0)
            eng = TradingEngine(cfg, feed=SyntheticFeed(tick_interval=60.0), broker=b)
            eng.boot()
            asset = eng.feed.assets[0]
            eng.broker.on_tick(Tick(asset=asset, price=1.0))

            def make_order():
                return Order(asset=asset, side=Side.CALL, amount=10.0,
                             expiry_seconds=60, payout=0.85, strategy="s", tag="")

            eng.broker.submit(make_order())  # distinct ids — position.id keys off order.id
            eng.broker.submit(make_order())
            self.assertEqual(len(eng.broker.open_positions()), 2)

            # no lockdown -> nothing salvaged
            eng.regime_of = {asset: SimpleNamespace()}
            eng.survivor = SimpleNamespace(posture_for=lambda *a, **k: "NORMAL")
            eng.cycle()
            self.assertEqual(len(eng.broker.open_positions()), 2)

            # LOCKDOWN -> the lifeboat launches
            eng.survivor = SimpleNamespace(posture_for=lambda *a, **k: "LOCKDOWN")
            eng.cycle()
            self.assertEqual(len(eng.broker.open_positions()), 0)
            eng.shutdown()

    def test_salvage_disabled_by_config(self):
        from cybertrade.bot.engine import TradingEngine
        from cybertrade.config import AppConfig
        from cybertrade.data.feed import SyntheticFeed
        from cybertrade.execution.paper import PaperBroker

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.risk.crisis_salvage = False
            cfg.journal_path = os.path.join(tmp, "journal.db")
            cfg.calibration_path = os.path.join(tmp, "cal.json")
            b = PaperBroker(starting_balance=1000.0)
            eng = TradingEngine(cfg, feed=SyntheticFeed(tick_interval=60.0), broker=b)
            eng.boot()
            asset = eng.feed.assets[0]
            eng.broker.on_tick(Tick(asset=asset, price=1.0))
            eng.broker.submit(Order(asset=asset, side=Side.CALL, amount=10.0,
                                    expiry_seconds=60, payout=0.85, strategy="s", tag=""))
            eng.regime_of = {asset: SimpleNamespace()}
            eng.survivor = SimpleNamespace(posture_for=lambda *a, **k: "LOCKDOWN")
            eng.cycle()
            self.assertEqual(len(eng.broker.open_positions()), 1)  # respected the switch
            eng.shutdown()


if __name__ == "__main__":
    unittest.main()
