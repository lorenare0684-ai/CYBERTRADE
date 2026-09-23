"""Phase-21 tests — the cockpit: live drills + console controls.

The DRILL buttons crash the running terminal's market through the feed's
price filter (one shock point — tick, book, and broker all see the same
storm), and the console LOCKDOWN button launches the P19 lifeboat through
the real survivor chain.  Everything the operator can press is tested
through the same `terminal.command` surface the HTTP API uses.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from cybertrade.constants import Side
from cybertrade.data.models import Order, Tick
from cybertrade.exceptions import DataError


def _engine(tmp):
    from cybertrade.bot.engine import TradingEngine
    from cybertrade.config import AppConfig
    from cybertrade.data.feed import SyntheticFeed
    from cybertrade.execution.paper import PaperBroker

    cfg = AppConfig()
    cfg.journal_path = os.path.join(tmp, "j.db")
    cfg.calibration_path = os.path.join(tmp, "c.json")
    eng = TradingEngine(cfg, feed=SyntheticFeed(tick_interval=60.0),
                        broker=PaperBroker(starting_balance=1000.0))
    eng.boot()
    return eng


def _terminal(eng):
    from cybertrade.web.server import EngineHub, WebTerminal

    return WebTerminal(EngineHub(eng), host="127.0.0.1", port=0)


class TestFeedFilter(unittest.TestCase):
    def test_wired_and_scale_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(tmp)
            filt = eng.feed.price_filter
            self.assertIsNotNone(filt)             # wired at boot
            self.assertAlmostEqual(filt("EURUSD_otc", 2.0), 2.0)  # idle = identity
            eng.drill.arm("flash_crash", assets=["EURUSD_otc"], ticks=5)
            self.assertNotEqual(filt("EURUSD_otc", 2.0), 2.0)    # armed = shock
            eng.shutdown()

    def test_disarm_stops_the_storm(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(tmp)
            eng.drill.arm("gap_open", assets=["EURUSD_otc"], ticks=5)
            self.assertTrue(eng.drill.active())
            eng.drill.disarm()
            self.assertFalse(eng.drill.active())
            self.assertAlmostEqual(eng.feed.price_filter("EURUSD_otc", 3.0), 3.0)
            self.assertIsNotNone(eng.drill_report()["finished_ts"])
            eng.shutdown()


class TestWebDrillCommands(unittest.TestCase):
    def test_arm_report_stop_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(tmp)
            term = _terminal(eng)
            self.assertIsNone(term.hub.state()["drill"])          # idle
            out = term.command({"cmd": "drill", "scenario": "flash_crash", "ticks": 8})
            self.assertTrue(out["ok"])
            self.assertEqual(out["drill"]["name"], "flash_crash")
            self.assertEqual(term.hub.state()["drill"]["name"], "flash_crash")
            bad = term.command({"cmd": "drill", "scenario": "meteor_strike"})
            self.assertFalse(bad["ok"])
            stop = term.command({"cmd": "drill", "scenario": "stop"})
            self.assertTrue(stop["ok"])
            self.assertFalse(eng.drill.active())
            eng.shutdown()

    def test_console_lockdown_launches_lifeboat(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(tmp)
            term = _terminal(eng)
            asset = eng.feed.assets[0]
            eng.broker.on_tick(Tick(asset=asset, price=1.10, ts=1.0))
            eng.broker.submit(Order(asset=asset, side=Side.CALL, amount=10.0,
                                    expiry_seconds=30, payout=0.85, strategy="s", tag=""))
            self.assertEqual(len(eng.broker.open_positions()), 1)
            self.assertTrue(term.command({"cmd": "lockdown"})["ok"])
            eng.cycle(now=2.0)          # posture_for honours manual lockdown
            self.assertEqual(len(eng.broker.open_positions()), 0)  # lifeboat sailed
            self.assertTrue(term.command({"cmd": "unlock"})["ok"])
            eng.shutdown()

    def test_all_five_drills_accepted(self):
        from cybertrade.bot.drills import CRISIS_SCENARIOS

        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(tmp)
            term = _terminal(eng)
            for name in CRISIS_SCENARIOS:
                out = term.command({"cmd": "drill", "scenario": name, "ticks": 2})
                self.assertTrue(out["ok"], name)
            eng.drill.disarm()
            eng.shutdown()


class TestSingleShockRule(unittest.TestCase):
    def test_ingress_does_not_double_shock(self):
        """The feed filter is the only shock point — _on_tick must not re-apply."""
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(tmp)
            asset = eng.feed.assets[0]
            eng.drill.arm("flash_crash", assets=[asset], ticks=10)
            price = eng.feed.price_filter(asset, 1.10)   # feed applies once
            eng._on_tick(Tick(asset=asset, price=price, ts=1.0))
            self.assertAlmostEqual(eng.broker.last_price(asset), price)
            self.assertEqual(eng.drill.stats.ticks, 1)   # exactly one step consumed
            eng.shutdown()

    def test_gauntlet_rows_still_score(self):
        from cybertrade.bot.drills import run_gauntlet

        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(tmp)
            rows = run_gauntlet(eng, scenarios=("flash_crash",), ticks=40)
            eng.shutdown()
            row = rows[0]
            self.assertEqual(row["ticks"], 40)   # deck caps at arm(ticks=40)
            self.assertTrue(row["verdict"].startswith("SURVIVED") or row["verdict"] == "KILLED")


if __name__ == "__main__":
    unittest.main()
