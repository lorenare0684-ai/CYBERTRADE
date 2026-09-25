"""Phase-28 tests — the memory: operator decisions survive restart.

A crisis lockdown and deck toggles are operator decisions, not process
memory. `statestore` writes atomically; engine `boot()` restores both;
corrupt/missing files load as empty (never stop boot). Server commands
(lockdown/unlock/strategy) persist on every change, and `clear_kill`'s
programmatic unlock updates the disk too.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.statestore import load_operator_state, save_operator_state
from cybertrade.web.server import EngineHub, WebTerminal
from tests.venue_stubs import VenueFeed, VenueStub


def _engine(tmp: str, tag: str) -> TradingEngine:
    cfg = AppConfig()
    cfg.journal_path = os.path.join(tmp, f"{tag}-j.db")
    cfg.calibration_path = os.path.join(tmp, f"{tag}-c.json")
    cfg.operator_path = os.path.join(tmp, f"{tag}-op.json")
    return TradingEngine(
        cfg, feed=VenueFeed(),
        broker=VenueStub(balance=1000.0),
    )


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "op.json")

    def test_roundtrip_and_version(self):
        self.assertEqual(load_operator_state(self.path), {})
        self.assertTrue(save_operator_state(
            self.path, {"manual_lockdown": True, "lockdown_reason": "x"}))
        data = load_operator_state(self.path)
        self.assertTrue(data["manual_lockdown"])
        self.assertEqual(data["version"], 1)

    def test_corrupt_and_missing_are_safe(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(load_operator_state(self.path), {})
        self.assertEqual(load_operator_state(self.path + ".nope"), {})
        self.assertFalse(save_operator_state("", {}))


class TestEngineRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_lockdown_and_deck_survive_restart(self):
        e1 = _engine(self.tmp, "a")
        e1.boot()
        e1.survivor.engage_lockdown("test crisis")
        name = e1.ensemble.members[0].name
        e1.ensemble.members[0].enabled = False
        self.assertTrue(e1.save_operator_state())
        e1.shutdown()

        e2 = _engine(self.tmp, "a")   # same paths — simulates restart
        e2.boot()
        try:
            self.assertTrue(e2.survivor._manual_lockdown)
            self.assertEqual(e2.survivor.lockdown_reason, "test crisis")
            self.assertFalse(e2.ensemble.members[0].enabled)
            self.assertTrue(any("LOCKDOWN" in m and "restored" in m
                                for m in e2.health.messages), e2.health.messages)
            self.assertEqual(name, e2.ensemble.members[0].name)
        finally:
            e2.shutdown()

    def test_fresh_boot_with_no_file_is_clean(self):
        e = _engine(self.tmp, "b")
        e.boot()
        try:
            self.assertFalse(e.survivor._manual_lockdown)
            self.assertTrue(all(m.enabled for m in e.ensemble.members))
            self.assertFalse(os.path.exists(
                os.path.join(self.tmp, "b-op.json")))  # load never writes
        finally:
            e.shutdown()

    def test_unlock_persists_to_disk(self):
        e = _engine(self.tmp, "c")
        e.boot()
        e.survivor.engage_lockdown("x")
        e.save_operator_state()
        e.survivor.clear_lockdown()
        e.save_operator_state()
        path = os.path.join(self.tmp, "c-op.json")
        data = load_operator_state(path)
        self.assertFalse(data["manual_lockdown"])
        e.shutdown()


class TestCommandPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.eng = _engine(self.tmp, "d")
        self.eng.boot()
        self.term = WebTerminal(EngineHub(self.eng), host="127.0.0.1",
                                port=0)
        self.path = os.path.join(self.tmp, "d-op.json")

    def tearDown(self):
        self.eng.shutdown()

    def test_lockdown_and_unlock_commands_persist(self):
        r = self.term.command({"cmd": "lockdown"})
        self.assertTrue(r["ok"])
        self.assertTrue(load_operator_state(self.path)["manual_lockdown"])
        r = self.term.command({"cmd": "unlock"})
        self.assertTrue(r["ok"])
        self.assertFalse(load_operator_state(self.path)["manual_lockdown"])

    def test_strategy_toggle_persists(self):
        name = self.eng.ensemble.members[3].name
        r = self.term.command({"cmd": "strategy", "name": name,
                               "enabled": False})
        self.assertTrue(r["ok"])
        self.assertIn(name, load_operator_state(self.path)["disabled_strategies"])
        r = self.term.command({"cmd": "strategy", "name": name,
                               "enabled": True})
        self.assertTrue(r["ok"])
        self.assertNotIn(name,
                         load_operator_state(self.path)["disabled_strategies"])


if __name__ == "__main__":
    unittest.main()
