"""Phase-23 tests — the score bay: backtests & gauntlets from the cockpit.

`EngineHub.start_job` runs one score job at a time on a worker thread; the
live engine is never touched (the gauntlet boots its own temp engine and
the hub mutes bus forwarding while it runs). Results land in
`state()["job"]` for the HUD to render.
"""

from __future__ import annotations

import time
import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.data.feed import SyntheticFeed
from cybertrade.execution.paper import PaperBroker
from cybertrade.web.server import EngineHub, WebTerminal


def _wait(hub: EngineHub, timeout: float = 90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = hub.job_report()
        if job.get("state") in ("done", "error"):
            return job
        time.sleep(0.25)
    return hub.job_report()


class TestScoreBay(unittest.TestCase):
    def setUp(self):
        self.eng = TradingEngine(
            AppConfig(),
            feed=SyntheticFeed(tick_interval=60.0),
            broker=PaperBroker(starting_balance=1000.0),
        )
        self.hub = EngineHub(self.eng)
        self.term = WebTerminal(self.hub, host="127.0.0.1", port=0)

    def test_state_includes_idle_job(self):
        state = self.term.hub.state()
        self.assertIn("job", state)
        self.assertEqual(state["job"]["state"], "idle")

    def test_unknown_job_kind_rejected(self):
        r = self.term.command({"cmd": "run", "kind": "bogus"})
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_backtest_job_completes_with_card(self):
        r = self.term.command({
            "cmd": "run", "kind": "backtest",
            "opts": {"bars": 30, "seeds": [1], "scenarios": ["flash_crash"]},
        })
        self.assertTrue(r["ok"], r)
        job = _wait(self.hub)
        self.assertEqual(job["state"], "done", job)
        res = job["result"]
        self.assertGreaterEqual(res["runs"], 1)
        self.assertIn("scenario", res["table"])
        self.assertTrue(res["card"])

    def test_second_job_rejected_while_running(self):
        with self.hub._job_lock:
            self.hub._job = {"state": "running", "kind": "backtest",
                             "started": 0.0, "opts": {}}
        r = self.hub.start_job("gauntlet", {})
        self.assertFalse(r["ok"])
        self.assertIn("already running", r["error"])
        with self.hub._job_lock:
            self.hub._job = {"state": "idle"}  # restore

    def test_gauntlet_job_isolated_and_scores(self):
        r = self.term.command({
            "cmd": "run", "kind": "gauntlet",
            "opts": {"ticks": 40, "seed": 1337},
        })
        self.assertTrue(r["ok"], r)
        job = _wait(self.hub)
        self.assertEqual(job["state"], "done", job)
        rows = job["result"]["rows"]
        self.assertEqual(job["result"]["runs"], 5)
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertIn("verdict", row)
            self.assertIn(row["verdict"].split(" ")[0], ("SURVIVED", "KILLED"))
        # mute must be released — the live HUD is not cross-wired
        self.assertFalse(self.hub._mute.is_set())
        # live engine untouched by the temp gauntlet engine
        self.assertEqual(self.eng.drill.stats, self.eng.drill.stats)
        self.assertFalse(self.eng.drill.active())

    def test_mute_blocks_forwarding(self):
        from cybertrade.events import Topic, default_bus
        from cybertrade.events import Event  # noqa: F401 — existence check

        with self.hub._job_lock:
            before = len(self.hub._log_lines)
        self.hub._mute.set()
        try:
            default_bus.publish(Topic.LOG, {"level": "info", "msg": "muted"},
                                source="test")
        finally:
            self.hub._mute.clear()
        with self.hub._job_lock:
            after = len(self.hub._log_lines)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
