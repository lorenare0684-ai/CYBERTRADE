"""Phase 32: durable governor/book recovery and bounded PAPER supervision.

Most lifecycles use fake clocks/processes; bounded subprocess probes also
exercise real crashes, OS-lock release, noisy pipes, and hang cleanup.
No venue or Tk needed.
"""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import random
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from cybertrade.bot.engine import TradingEngine
from cybertrade.cli import build_parser, cmd_run, cmd_supervise
from cybertrade.config import AppConfig, RiskConfig
from cybertrade.constants import EngineState, Side
from cybertrade.data.feed import Feed
from cybertrade.data.models import AccountSnapshot, Fill, Order, Position, Tick
from cybertrade.exceptions import ConfigError, KillSwitchEngaged
from cybertrade.execution.broker import Broker
from cybertrade.execution.paper import PaperBroker
from cybertrade.risk.manager import RiskManager
from cybertrade.statestore import (
    StateError, StateLease, heartbeat_age, load_continuity, load_operator_state,
    load_state, pack_position, read_heartbeat, save_continuity, save_state,
    unpack_position, write_heartbeat,
)
from cybertrade.utils import timex
from cybertrade.watchdog import (
    RestartBudget, SAFETY_HOLD_EXIT, Supervisor, backoff_seconds, write_crash_report,
)
from cybertrade.web.server import EngineHub, WebTerminal

ROOT = Path(__file__).resolve().parents[1]
ASSET = "EURUSD_otc"


class QuietFeed(Feed):
    """Deterministic quotes; no background threads or synthetic warmup."""
    def __init__(self):
        super().__init__([ASSET])

    def history(self, asset, bars, timeframe_seconds):
        return []

    def last_price(self, asset):
        return 1.1


class VenueStub(Broker):
    def __init__(self, balance=1000.0, user="account-a"):
        super().__init__()
        self.balance = balance
        self.api = SimpleNamespace(session=SimpleNamespace(user_id=user))
        self.submits = 0
        self._connected = False

    @property
    def name(self):
        return "QUOTEX-DEMO"

    @property
    def connected(self):
        return self._connected

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def account(self):
        return AccountSnapshot(balance=self.balance, equity=self.balance,
                               margin_used=0, open_positions=0, peak_balance=self.balance)

    def last_price(self, asset):
        return 1.1

    def payout_for(self, asset, expiry_seconds):
        return .85

    def submit(self, order):
        self.submits += 1
        raise AssertionError("recovery must never send a venue order")

    def open_positions(self):
        return []

    def settle_due(self, now=None):
        return []

    def close_position(self, position_id):
        return False


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "continuity.json")


class TestStatePrimitives(TempCase):
    def test_atomic_private_roundtrip(self):
        self.assertTrue(save_state(self.path, {"x": 1}))
        self.assertEqual(load_state(self.path), {"x": 1})
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))

    def test_bad_serialization_leaves_prior_file(self):
        save_state(self.path, {"balance": 900})
        for value in (float("nan"), float("inf"), object()):
            with self.subTest(value=repr(value)):
                self.assertFalse(save_state(self.path, {"balance": value}))
                self.assertEqual(load_state(self.path)["balance"], 900)
        self.assertFalse(list(Path(self.tmp.name).glob(".state-*")))

    def test_replace_failure_preserves_prior_and_cleans_tmp(self):
        save_state(self.path, {"x": 1})
        with mock.patch("cybertrade.statestore.os.replace", side_effect=OSError("disk")):
            self.assertFalse(save_state(self.path, {"x": 2}))
        self.assertEqual(load_state(self.path), {"x": 1})
        self.assertFalse(list(Path(self.tmp.name).glob(".state-*")))

    def test_durable_file_fsync_before_replace(self):
        events = []
        replace = os.replace
        with mock.patch("cybertrade.statestore.os.fsync", side_effect=lambda fd: events.append("sync")), \
             mock.patch("cybertrade.statestore.os.replace",
                        side_effect=lambda a, b: (events.append("replace"), replace(a, b))):
            self.assertTrue(save_state(self.path, {"x": 1}))
        self.assertEqual(events[:2], ["sync", "replace"])
        if os.name == "posix":
            self.assertEqual(events[-1], "sync")

    def test_only_missing_continuity_is_fresh(self):
        self.assertEqual(load_continuity(self.path), {})
        bad_rows = ["{broken", "[]", "null", "{}",
                    '{"version":99,"saved_ts":1}', '{"version":true,"saved_ts":1}',
                    '{"version":1,"version":1,"saved_ts":1}',
                    '{"version":1,"saved_ts":NaN}',
                    '{"version":1,"saved_ts":1e999}']
        for row in bad_rows:
            with self.subTest(row=row):
                Path(self.path).write_text(row)
                with self.assertRaises(StateError):
                    load_continuity(self.path)

    def test_operator_preferences_remain_lenient(self):
        Path(self.path).write_text("broken")
        self.assertEqual(load_operator_state(self.path), {})

    def test_lease_excludes_second_writer_and_releases(self):
        first, second = StateLease(self.path), StateLease(self.path)
        first.acquire()
        try:
            with self.assertRaises(StateError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()
        self.assertTrue(os.path.exists(self.path + ".lock"))

    def test_os_releases_lease_after_unclean_process_exit(self):
        code = ("import os,sys; from cybertrade.statestore import StateLease; "
                "lock=StateLease(sys.argv[1]); lock.acquire(); os._exit(3)")
        proc = subprocess.run([sys.executable, "-c", code, self.path], cwd=ROOT,
                              capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        with StateLease(self.path):
            pass

    def test_heartbeat_identity_and_protected_fields(self):
        self.assertTrue(write_heartbeat(self.path, ts=100.0, cycle=7, run_id="run-a",
                                        state="armed", extra={"pid": -1, "cycle": 999}))
        beat = read_heartbeat(self.path)
        self.assertEqual((beat["pid"], beat["run_id"], beat["cycle"]), (os.getpid(), "run-a", 7))
        self.assertEqual(heartbeat_age(beat, 112.5), 12.5)
        self.assertIsNone(heartbeat_age({"ts": float("nan")}, 1))
        self.assertIsNone(heartbeat_age({"ts": True}, 1))

    def test_position_codec_exact_and_strict(self):
        pos = Position(fill=Fill(order_id="order-a", asset=ASSET, side=Side.PUT,
                                 price=1.1, amount=10, payout=.85, ts=100),
                       expiry_ts=160, strategy="alpha")
        row = pack_position(pos)
        self.assertEqual(pack_position(unpack_position(row)), row)
        for field, value in (("id", ""), ("price", float("nan")), ("side", "flat"),
                             ("amount", -1), ("amount", True), ("asset", "")):
            bad = copy.deepcopy(row)
            bad["fill"][field] = value
            self.assertIsNone(unpack_position(bad), (field, value))
        del row["id"]
        self.assertIsNone(unpack_position(row))

    def test_config_paths_roundtrip_and_do_not_overlap(self):
        cfg = AppConfig()
        cfg.continuity_path, cfg.heartbeat_path = self.path, self.path + ".hb"
        restored = AppConfig.from_dict(cfg.to_dict())
        self.assertEqual(restored.continuity_path, self.path)
        self.assertEqual(restored.heartbeat_path, self.path + ".hb")
        cfg.heartbeat_path = self.path
        with self.assertRaises(ConfigError):
            cfg.validate()
        cfg.heartbeat_path = None
        with self.assertRaises(ConfigError):
            cfg.validate()


class TestRiskRecovery(unittest.TestCase):
    def risk(self):
        r = RiskManager()
        r.reset_day(1000, ts=200 * 86400 + 100)
        r.state.current_balance = 930
        r.state.peak_balance = 1200
        r.state.trades_today = 12
        r.state.trades_this_hour = 4
        r.state.consecutive_losses = 2
        r.state.cooldown_until = 202 * 86400
        r.state.locked_until = 201 * 86400
        r.state.kill, r.state.kill_reason = True, "latched"
        r.state.last_results = [True, False, False]
        r.record_strategy("alpha", False)
        r.on_open(Order(ASSET, Side.CALL, 10, meta={"cluster": "USD"}))
        return r

    def test_same_day_full_precision_governor(self):
        r = self.risk()
        data = r.export_state()
        restored = RiskManager()
        restored.restore_state(data, now=200 * 86400 + 150)
        self.assertEqual(restored.export_state(), data)
        self.assertAlmostEqual(restored.daily_loss_frac(), .07)
        self.assertTrue(restored.state.kill)

    def test_next_utc_day_rolls_only_daily_and_elapsed_hour(self):
        r = self.risk()
        restored = RiskManager()
        restored.restore_state(r.export_state(), now=201 * 86400 + 1)
        st = restored.state
        self.assertEqual((st.trades_today, st.trades_this_hour, st.locked_until), (0, 0, 0))
        self.assertEqual(st.day_start_balance, 930)
        self.assertEqual(st.peak_balance, 1200)
        self.assertTrue(st.kill)
        self.assertEqual(st.consecutive_losses, 2)
        self.assertEqual(st.cooldown_until, 202 * 86400)
        self.assertEqual((st.open_count, st.open_stake_sum), (1, 10))

    def test_backwards_clock_never_refills_rate_budget(self):
        r = self.risk()
        data = r.export_state()
        restored = RiskManager()
        restored.restore_state(data, now=200 * 86400 + 50)
        self.assertEqual(restored.state.trades_today, r.state.trades_today)
        self.assertEqual(restored.state.trades_this_hour, r.state.trades_this_hour)
        self.assertEqual(restored.state.day_start_balance, 1000)

    def test_running_clock_crosses_midnight_without_restart(self):
        r = self.risk()
        r._roll_clock(201 * 86400 + 1)
        self.assertEqual(r.state.trades_today, 0)
        self.assertEqual(r.state.day_start_balance, 930)
        self.assertTrue(r.state.kill)

    def test_settlement_can_roll_utc_day_without_extending_yesterdays_lock(self):
        r = RiskManager(RiskConfig(max_daily_loss_frac=.04))
        day = 200 * 86400
        r.reset_day(1000, ts=day + 100)
        order = Order(ASSET, Side.CALL, 50)
        r.on_open(order)
        r.update_balance(950)  # yesterday's escrow
        r.state.locked_until = day + 86400
        r.on_close(order, won=False, pnl=-50, balance=950, now=day + 86401)
        self.assertEqual(r.state.day_start_balance, 950)
        self.assertEqual(r.state.locked_until, 0)
        self.assertEqual(r.state.trades_today, 0)
        self.assertEqual(r.state.open_count, 0)
        self.assertEqual(r.state.consecutive_losses, 1)
        self.assertEqual(r.state.peak_balance, 1000)

    def test_invalid_state_does_not_partially_mutate_governor(self):
        r, target = self.risk(), RiskManager()
        before = target.export_state()
        for key, value in (("trades_today", -1), ("kill", "false"),
                           ("peak_balance", float("nan")), ("open_assets", []),
                           ("last_results", [1])):
            row = r.export_state()
            row["state"][key] = value
            with self.assertRaises(StateError):
                target.restore_state(row)
            self.assertEqual(target.export_state(), before)

    def test_zero_cash_is_not_missing_balance(self):
        r = RiskManager()
        r.update_balance(0.0)
        self.assertEqual(r.total_drawdown(), 1.0)
        self.assertFalse(r.check(asset=ASSET, side=Side.CALL, stake=1, payout=.85,
                                 confidence=.8).all_ok)


class EngineCase(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = AppConfig()
        self.cfg.strategy.universe = [ASSET]
        self.cfg.risk.win_rate_floor = 0.0
        self.cfg.journal_path = os.path.join(self.tmp.name, "journal.db")
        self.cfg.calibration_path = os.path.join(self.tmp.name, "calibration.json")
        self.cfg.operator_path = os.path.join(self.tmp.name, "operator.json")
        self.cfg.plugins_dir = os.path.join(self.tmp.name, "plugins")
        self.cfg.calendar_path = os.path.join(self.tmp.name, "calendar.json")
        self.cfg.continuity_path = self.path
        self.cfg.heartbeat_path = os.path.join(self.tmp.name, "heartbeat.json")
        self.cfg.alerts.enable_sound = False
        self.cfg.tape_enabled = False

    def engine(self, durable=True, broker=None, boot=True):
        eng = TradingEngine(self.cfg, feed=QuietFeed(), durable=durable,
                            broker=broker or PaperBroker(latency_ms=0, slippage_bps=0))
        self.addCleanup(self.close, eng)
        if boot:
            eng.boot()
        return eng

    @staticmethod
    def close(eng):
        if not getattr(eng, "_test_closed", False):
            eng.shutdown()
            if eng.journal is not None:
                eng.journal.close()
            eng._test_closed = True

    def order(self, eng, amount=10, expiry=60):
        eng.broker.on_tick(Tick(ASSET, 1.1))
        order = eng.oms.submit(ASSET, Side.CALL, amount, expiry, payout=.85,
                               confidence=.8, strategy="alpha", cluster="USD", regime="range",
                               votes=({"strategy": "alpha", "confidence": .8},))
        self.assertIsNotNone(order)
        return order

    def expire_offline(self):
        eng = self.engine()
        self.order(eng)
        self.close(eng)
        data = load_continuity(self.path)
        row = data["paper"]["positions"][0]
        row["fill"]["ts"] = time.time() - 120
        row["expiry_ts"] = row["fill"]["ts"] + 60
        data["orders"][0]["ts"] = row["fill"]["ts"]
        save_continuity(self.path, data)
        return self.engine()


class TestEngineContinuity(EngineCase):
    def test_fresh_runtime_checkpoints_disarmed(self):
        eng = self.engine()
        self.assertEqual(eng.state, EngineState.DISARMED)
        self.assertFalse(eng.continuity.restored)
        self.assertTrue(eng.snapshot()["continuity"]["enabled"])
        data = load_continuity(self.path)
        self.assertEqual(data["pending_operation"], "")
        self.assertNotIn("armed", data)
        self.assertEqual(data["ledger"]["balance"], 1000)

    def test_ephemeral_engines_never_share_runtime_files(self):
        eng = self.engine(durable=False)
        eng.cycle()
        self.close(eng)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.cfg.heartbeat_path))
        self.assertFalse(eng.snapshot()["continuity"]["enabled"])

    def test_restart_keeps_daily_loss_lock_and_trade_count(self):
        self.cfg.risk.max_daily_loss_frac = .04
        eng = self.engine()
        self.order(eng, amount=50)
        pos = eng.broker.open_positions()[0]
        eng.broker.on_tick(Tick(ASSET, 1.0, ts=pos.expiry_ts + 1))
        eng.oms.pump(now=pos.expiry_ts + 1)
        self.assertGreater(eng.risk.state.locked_until, time.time())
        self.close(eng)
        restored = self.engine()
        self.assertEqual(restored.oms.ledger.balance, 950)
        self.assertEqual(restored.broker.account().balance, 950)
        self.assertEqual(restored.risk.state.trades_today, 1)
        self.assertAlmostEqual(restored.risk.daily_loss_frac(), .05)
        self.assertEqual(restored.risk.state.consecutive_losses, 1)
        self.assertIsNone(restored.oms.submit(ASSET, Side.CALL, 5, 60, payout=.85))

    def test_open_contract_links_and_exposure_settle_once(self):
        eng = self.engine()
        order = self.order(eng)
        original = eng.broker.open_positions()[0]
        self.close(eng)
        restored = self.engine()
        pos = restored.broker.open_positions()[0]
        self.assertEqual(pack_position(pos), pack_position(original))
        self.assertEqual(restored.oms.orders[order.id].meta["cluster"], "USD")
        self.assertEqual(restored.risk.state.open_clusters, {"USD": 1})
        self.assertEqual(restored.risk.state.trades_today, 1)  # not counted twice
        restored.broker.on_tick(Tick(ASSET, 1.2, ts=pos.expiry_ts + 1))
        records = restored.oms.pump(now=pos.expiry_ts + 1)
        self.assertEqual((records[0].strategy, records[0].regime), ("alpha", "range"))
        self.assertEqual(restored.oms.ledger.balance, 1008.5)
        self.assertEqual(restored.risk.state.open_count, 0)
        self.assertEqual(restored.risk.state.open_stake_sum, 0)
        self.assertEqual(restored.oms.pump(now=pos.expiry_ts + 2), [])
        self.close(restored)
        again = self.engine()
        self.assertEqual(again.oms.ledger.balance, 1008.5)
        self.assertEqual(again.broker.open_positions(), [])

    def test_pending_salvage_cash_not_credited_twice(self):
        eng = self.engine()
        self.order(eng)
        pos = eng.broker.open_positions()[0]
        eng.broker.on_tick(Tick(ASSET, 1.0))
        self.assertTrue(eng.broker.close_position(pos.id))
        self.assertEqual(eng.broker.account().balance, 992.5)
        self.assertEqual(eng.oms.ledger.balance, 990)
        eng.continuity.commit()
        self.close(eng)
        restored = self.engine()
        self.assertEqual(len(restored.oms.pump()), 1)
        self.assertEqual(restored.oms.ledger.balance, 992.5)
        self.assertEqual(restored.risk.state.open_count, 0)
        self.assertEqual(restored.oms.pump(), [])

    def test_kill_and_explicit_clear_are_both_durable(self):
        eng = self.engine()
        eng.kill("operator test")
        self.close(eng)
        restored = self.engine()
        self.assertEqual(restored.state, EngineState.KILL)
        with self.assertRaises(KillSwitchEngaged):
            restored.arm()
        restored.clear_kill()
        self.close(restored)
        again = self.engine()
        self.assertEqual(again.state, EngineState.DISARMED)
        self.assertFalse(again.risk.state.kill)

    def test_interrupted_operation_is_held_not_replayed(self):
        eng = self.engine()
        eng.continuity.begin("submit")
        before = Path(self.path).read_bytes()
        eng.continuity.release()  # simulate loss of the process, no AFTER commit
        self.close(eng)
        restored = self.engine()
        self.assertTrue(restored.continuity.status()["blocked"])
        self.assertIn("interrupted submit", restored.continuity.fault)
        self.assertEqual(restored.broker.open_positions(), [])
        with self.assertRaises(StateError):
            restored.clear_kill()
        self.close(restored)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_corrupt_checkpoint_is_not_replaced_by_fresh_balance(self):
        Path(self.path).write_bytes(b"{broken checkpoint")
        eng = self.engine()
        self.assertEqual(eng.state, EngineState.KILL)
        with self.assertRaises(KillSwitchEngaged):
            eng.arm()
        self.close(eng)
        self.assertEqual(Path(self.path).read_bytes(), b"{broken checkpoint")

    def test_partial_position_corruption_refuses_entire_book(self):
        eng = self.engine()
        self.order(eng)
        self.close(eng)
        data = load_continuity(self.path)
        del data["paper"]["positions"][0]["fill"]["id"]
        save_continuity(self.path, data)
        restored = self.engine()
        self.assertTrue(restored.continuity.fault)
        self.assertEqual(restored.broker.open_positions(), [])
        self.assertTrue(restored.risk.state.kill)

    def test_cross_book_cash_or_exposure_mismatch_is_rejected(self):
        eng = self.engine()
        self.close(eng)
        data = load_continuity(self.path)
        data["paper"]["balance"] = 950
        save_continuity(self.path, data)
        restored = self.engine()
        self.assertIn("cash mismatch", restored.continuity.fault)

    def test_failed_before_write_never_reaches_broker(self):
        eng = self.engine()
        before = Path(self.path).read_bytes()
        with mock.patch("cybertrade.continuity.save_continuity", return_value=False), \
             mock.patch.object(eng.broker, "submit") as submit:
            with self.assertRaises(StateError):
                eng.oms.submit(ASSET, Side.CALL, 10, 60, payout=.85)
        submit.assert_not_called()
        self.assertTrue(eng.risk.state.kill)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_failed_after_write_leaves_uncertainty_marker(self):
        eng = self.engine()
        eng.broker.on_tick(Tick(ASSET, 1.1))
        def fail_commit(path, payload):
            return save_continuity(path, payload) if payload["pending_operation"] else False
        with mock.patch("cybertrade.continuity.save_continuity", side_effect=fail_commit):
            with self.assertRaises(StateError):
                eng.oms.submit(ASSET, Side.CALL, 10, 60, payout=.85)
        self.assertEqual(load_continuity(self.path)["pending_operation"], "submit")
        self.assertTrue(eng.risk.state.kill)
        self.close(eng)
        restored = self.engine()
        self.assertIn("interrupted submit", restored.continuity.fault)

    def test_second_runtime_cannot_boot_on_same_book(self):
        first = self.engine()
        second = self.engine(boot=False)
        with self.assertRaises(StateError):
            second.boot()
        self.assertTrue(first.broker.connected)
        self.assertTrue(first.continuity.active)
        self.close(second)
        self.close(first)
        self.assertTrue(self.engine().continuity.restored)

    def test_completed_cycle_heartbeat_uses_wall_not_simulated_time(self):
        eng = self.engine()
        eng.cycle(now=1_000_000.0)
        beat = read_heartbeat(self.cfg.heartbeat_path)
        self.assertEqual(beat["cycle"], 1)
        self.assertAlmostEqual(beat["ts"], time.time(), delta=2)
        self.assertEqual(beat["pid"], os.getpid())

    def test_failed_cycle_does_not_claim_progress(self):
        eng = self.engine()
        before = read_heartbeat(self.cfg.heartbeat_path)
        with mock.patch.object(eng, "_sync_quote", side_effect=RuntimeError("stuck stage")):
            with self.assertRaises(RuntimeError):
                eng.cycle()
        self.assertEqual(read_heartbeat(self.cfg.heartbeat_path), before)

    def test_offline_expiry_does_not_become_a_refund_or_current_mark_win(self):
        eng = self.expire_offline()
        pos = eng.broker.open_positions()[0]
        eng.broker.on_tick(Tick(ASSET, 1.5))
        self.assertEqual(eng.oms.pump(), [])
        self.assertEqual(eng.oms.ledger.balance, 990)
        self.assertFalse(eng.oms.close_position(pos.id))
        with self.assertRaises(KillSwitchEngaged):
            eng.arm()
        self.assertEqual(eng.continuity.status()["held_positions"], [pos.id])

    def test_restored_future_contract_waits_for_fresh_expiry_quote(self):
        eng = self.engine()
        self.order(eng)
        self.close(eng)
        restored = self.engine()
        pos = restored.broker.open_positions()[0]
        self.assertEqual(restored.oms.pump(now=pos.expiry_ts + 1), [])
        self.assertEqual(restored.oms.ledger.balance, 990)
        restored.broker.on_tick(Tick(ASSET, 1.2, ts=pos.expiry_ts - 1))
        self.assertEqual(restored.oms.pump(now=pos.expiry_ts + 1), [])

    def test_explicit_paper_resolution_records_once_and_releases_exposure(self):
        eng = self.expire_offline()
        pos = eng.broker.open_positions()[0]
        self.assertTrue(eng.oms.resolve_recovery(pos.id, 1.0))
        self.assertFalse(eng.oms.resolve_recovery(pos.id, 1.0))
        self.assertEqual(eng.oms.ledger.balance, 990)
        self.assertEqual(eng.risk.state.open_count, 0)
        self.assertFalse(eng.continuity.status()["blocked"])
        self.assertEqual(len(eng.oms.trades), 1)

    def test_invalid_resolution_input_does_not_poison_book(self):
        eng = self.expire_offline()
        pos = eng.broker.open_positions()[0]
        for value in (float("nan"), -1, True, "1.2"):
            with self.assertRaises(StateError):
                eng.oms.resolve_recovery(pos.id, value)
        self.assertFalse(eng.continuity.fault)
        self.assertEqual(len(eng.broker.open_positions()), 1)

    def test_web_recovery_status_and_resolution_endpoint(self):
        eng = self.expire_offline()
        hub = EngineHub(eng, self.cfg)
        web = WebTerminal(hub, port=0)
        pos = eng.broker.open_positions()[0]
        self.assertTrue(hub.state()["snapshot"]["continuity"]["blocked"])
        self.assertTrue(hub.state()["positions"][0]["recovery_hold"])
        result = web.command({"cmd": "resolve_paper", "position": pos.id, "expiry_price": 1.2})
        self.assertTrue(result["ok"], result)
        self.assertEqual(eng.oms.ledger.balance, 1008.5)
        self.assertEqual(hub.state()["positions"], [])

    def test_web_close_commits_salvage_with_ledger(self):
        eng = self.engine()
        self.order(eng)
        pos = eng.broker.open_positions()[0]
        eng.broker.on_tick(Tick(ASSET, 1.0))
        result = WebTerminal(EngineHub(eng, self.cfg), port=0).command(
            {"cmd": "close", "position": pos.id})
        self.assertTrue(result["ok"])
        data = load_continuity(self.path)
        self.assertEqual(data["paper"]["pending"], [])
        self.assertEqual(data["ledger"]["balance"], 992.5)
        self.assertEqual(data["risk"]["state"]["open_count"], 0)

    def test_new_account_or_mode_does_not_load_another_book(self):
        eng = self.engine()
        self.close(eng)
        self.cfg.broker.mode = "dryrun"
        restored = self.engine()
        self.assertIn("different mode/account", restored.continuity.fault)

    def _hard_crash_child(self, during_submit=False):
        paths = {name: getattr(self.cfg, name) for name in
                 ("continuity_path", "heartbeat_path", "journal_path", "operator_path",
                  "calibration_path", "plugins_dir", "calendar_path")}
        script = """
import json, os, sys
from tests.test_phase32 import QuietFeed
from cybertrade.config import AppConfig
from cybertrade.bot.engine import TradingEngine
from cybertrade.execution.paper import PaperBroker
from cybertrade.data.models import Tick
from cybertrade.constants import Side
cfg = AppConfig()
for key, value in json.loads(sys.argv[1]).items():
    setattr(cfg, key, value)
cfg.tape_enabled = False
cfg.alerts.enable_sound = False
cfg.strategy.universe = ['EURUSD_otc']
class CrashBroker(PaperBroker):
    def submit(self, order):
        fill = super().submit(order)
        if sys.argv[2] == 'during':
            os._exit(9)
        return fill
eng = TradingEngine(cfg, feed=QuietFeed(), durable=True,
                    broker=CrashBroker(latency_ms=0, slippage_bps=0))
eng.boot()
eng.broker.on_tick(Tick('EURUSD_otc', 1.1))
order = eng.oms.submit('EURUSD_otc', Side.CALL, 10, 60, payout=.85)
assert order is not None
os._exit(9)
"""
        proc = subprocess.run([sys.executable, "-c", script, json.dumps(paths),
                               "during" if during_submit else "after"], cwd=ROOT,
                              capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 9, proc.stderr)

    def test_real_process_crash_restores_acknowledged_contract(self):
        self._hard_crash_child()
        restored = self.engine()
        self.assertTrue(restored.continuity.restored)
        self.assertFalse(restored.continuity.status()["blocked"])
        self.assertEqual(len(restored.broker.open_positions()), 1)
        self.assertEqual(restored.oms.ledger.balance, 990)
        self.assertEqual(restored.risk.state.trades_today, 1)

    def test_real_crash_inside_submit_leaves_hold_not_replay(self):
        self._hard_crash_child(during_submit=True)
        restored = self.engine()
        self.assertIn("interrupted submit", restored.continuity.fault)
        self.assertTrue(restored.risk.state.kill)
        self.assertEqual(restored.broker.open_positions(), [])
        self.assertEqual(load_continuity(self.path)["pending_operation"], "submit")

    @unittest.skipUnless(os.name == "posix", "symlink probe requires POSIX")
    def test_symlink_alias_keeps_same_book_and_lock_inode(self):
        first = self.engine()
        self.close(first)
        alias = Path(self.tmp.name) / "alias.json"
        alias.symlink_to(self.path)
        self.cfg.continuity_path = str(alias)
        restored = self.engine()
        self.assertEqual(restored.continuity.path, self.path)
        self.assertTrue(alias.is_symlink())  # atomic write replaced target, not alias
        self.cfg.continuity_path = self.path
        other = self.engine(boot=False)
        with self.assertRaises(StateError):
            other.boot()

    def test_shutdown_wakes_long_timeframe_loop(self):
        self.cfg.strategy.timeframe = "1h"
        eng = self.engine()
        finished = threading.Event()
        with mock.patch.object(eng, "cycle", side_effect=lambda: finished.set()), \
             mock.patch.object(eng.watchdog, "start"), mock.patch.object(eng.watchdog, "stop"):
            eng.arm()
            self.assertTrue(finished.wait(2))
            thread = eng._thread
            eng.arm()
            self.assertIs(eng._thread, thread)
            self.close(eng)
            self.assertFalse(thread.is_alive())
        self.assertTrue(self.engine().continuity.restored)


class TestVenueRecovery(EngineCase):
    def setUp(self):
        super().setUp()
        self.cfg.broker.mode = "quotex"

    def test_live_recovery_never_replays_paper_cash_or_orders(self):
        first = self.engine(broker=VenueStub(balance=1000))
        self.close(first)
        venue = VenueStub(balance=975)
        restored = self.engine(broker=venue)
        self.assertTrue(restored.continuity.restored)
        self.assertEqual(restored.oms.ledger.balance, 975)
        self.assertEqual(restored.risk.state.day_start_balance, 1000)
        self.assertEqual(venue.submits, 0)
        self.assertFalse(restored.oms.resolve_recovery("anything", 1.1))
        with self.assertRaises(KillSwitchEngaged):
            restored.arm(live=False)

    def test_live_exposure_is_held_for_venue_reconciliation(self):
        first = self.engine(broker=VenueStub())
        with first.oms.transaction("test-live-exposure"):
            first.risk.on_open(Order(ASSET, Side.CALL, 10))
        self.close(first)
        restored = self.engine(broker=VenueStub())
        self.assertIn("venue reconciliation", restored.continuity.fault)
        self.assertEqual(restored.broker.submits, 0)
        self.assertTrue(restored.risk.state.kill)

    def test_authenticated_account_identity_must_match(self):
        first = self.engine(broker=VenueStub(user="account-a"))
        self.close(first)
        restored = self.engine(broker=VenueStub(user="account-b"))
        self.assertIn("different mode/account", restored.continuity.fault)

    def test_live_account_id_can_come_from_venue_balance_payload(self):
        from cybertrade.brokers.quotex.models import QXBalance
        venue = VenueStub(user="")
        venue.api.balance = QXBalance.from_payload({"userId": "from-venue", "balance": 1000})
        eng = self.engine(broker=venue)
        self.assertFalse(eng.continuity.fault)
        self.assertEqual(load_continuity(self.path)["scope"]["account"], "from-venue")

    def test_runtime_account_switch_is_blocked_before_order_submission(self):
        venue = VenueStub()
        eng = self.engine(broker=venue)
        before = Path(self.path).read_bytes()
        venue.api.session.user_id = "different-account"
        with self.assertRaises(StateError):
            eng.oms.submit(ASSET, Side.CALL, 10, 60, payout=.85)
        self.assertEqual(venue.submits, 0)
        self.assertIn("runtime mode/account changed", eng.continuity.fault)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_conflicting_venue_identity_sources_fail_closed(self):
        venue = VenueStub(user="one")
        venue.api.balance = SimpleNamespace(user_id="other")
        eng = self.engine(broker=venue)
        self.assertIn("identities disagree", eng.continuity.fault)

    def test_unknown_live_identity_fails_closed(self):
        eng = self.engine(broker=VenueStub(user=""))
        self.assertIn("user ID", eng.continuity.fault)
        self.assertEqual(eng.broker.submits, 0)
        self.assertFalse(os.path.exists(self.path))


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeProcess:
    def __init__(self, code=None, output=b"", clock=None, exit_at=None, ignore_term=False):
        self.pid = 424242
        self.returncode = code
        self.stdout = io.BytesIO(output)
        self.clock, self.exit_at = clock, exit_at
        self.terminated = self.killed = False
        self.ignore_term = ignore_term

    def poll(self):
        if self.clock is not None and self.exit_at is not None and self.clock() >= self.exit_at:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.ignore_term:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=0):
        if self.poll() is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class TestProcessSupervisor(TempCase):
    def setup_supervisor(self, proc=None, beats=None, max_restarts=0, **kwargs):
        self.clock = Clock()
        self.proc = proc or FakeProcess()
        self.spawn_kw = {}
        def spawn(cmd, **kw):
            self.spawn_kw.update(kw)
            return self.proc
        def reader(path):
            return beats(self) if beats else {}
        defaults = dict(heartbeat_path=self.path, crash_dir=self.tmp.name,
                        budget=RestartBudget(max_restarts, 60, clock=self.clock),
                        max_stale=2, startup_grace=4, watch_slice=1,
                        clock=self.clock, wall=lambda: 1_700_000_000 + self.clock(),
                        sleep=self.clock.sleep, spawn=spawn, read_beat=reader,
                        backoff=lambda n: 2 ** n)
        defaults.update(kwargs)
        return Supervisor(["python", "paper-child"], **defaults)

    def beat(self, seq=0, **changes):
        return {"pid": self.proc.pid, "run_id": self.spawn_kw["env"]["CYBERTRADE_RUN_ID"],
                "cycle": seq, "ts": 1e99, "state": "armed", **changes}

    def test_budget_rolls_and_allows_zero_restarts(self):
        clk = Clock()
        b = RestartBudget(2, 10, clock=clk)
        self.assertTrue(b.allow())
        self.assertTrue(b.allow())
        self.assertFalse(b.allow())
        clk.sleep(10)
        self.assertTrue(b.allow())
        self.assertFalse(RestartBudget(0).allow())
        with self.assertRaises(ValueError):
            RestartBudget(-1)

    def test_backoff_jitter_caps_and_large_attempts(self):
        for seed in range(10):
            first = backoff_seconds(1, rnd=random.Random(seed))
            self.assertGreaterEqual(first, 1.6)
            self.assertLessEqual(first, 2.4)
            self.assertLessEqual(backoff_seconds(100_000, rnd=random.Random(seed)), 300)
        self.assertNotEqual(backoff_seconds(3, rnd=random.Random(1)),
                            backoff_seconds(3, rnd=random.Random(2)))

    def test_report_unique_bounded_private_redacted_and_rotated(self):
        paths = []
        for _ in range(3):
            paths.append(write_crash_report(self.tmp.name, cmd=["x", "--ssid", "SECRET1"],
                                            exit_code=2, ts=100, keep=2, max_output=80,
                                            output="a" * 200 + '\npassword=SECRET2\n{"ssid":"SECRET3"}\nboom'))
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual(len(list(Path(self.tmp.name).glob("crash-*.txt"))), 2)
        body = Path(paths[-1]).read_text()
        self.assertNotIn("SECRET", body)
        self.assertIn("boom", body)
        self.assertLess(len(body), 350)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(paths[-1]).st_mode), 0o600)

    def test_clean_exit_is_not_respawned(self):
        sup = self.setup_supervisor(FakeProcess(code=0))
        self.assertEqual(sup.run(), 0)
        self.assertEqual(sup.restarts, 0)
        self.assertIsNone(sup.last_report)

    def test_safety_hold_exit_is_not_respawned(self):
        sup = self.setup_supervisor(FakeProcess(code=SAFETY_HOLD_EXIT), max_restarts=5)
        self.assertEqual(sup.run(), SAFETY_HOLD_EXIT)
        self.assertEqual(sup.restarts, 0)
        self.assertIn("safety-hold", Path(sup.last_report).read_text())

    def test_crash_budget_exhausts_after_bounded_restarts(self):
        procs = []
        def spawn(*args, **kwargs):
            proc = FakeProcess(code=2, output=b"traceback: boom")
            procs.append(proc)
            return proc
        sup = self.setup_supervisor(max_restarts=2, spawn=spawn)
        self.assertEqual(sup.run(), 1)
        self.assertEqual((sup.restarts, len(procs)), (2, 3))
        self.assertEqual(self.clock.sleeps, [2, 4])
        self.assertIn("boom", Path(sup.last_report).read_text())

    def test_missing_heartbeat_uses_startup_grace_then_terminates(self):
        sup = self.setup_supervisor()
        self.assertEqual(sup.run(), 1)
        self.assertEqual(self.clock(), 4)
        self.assertEqual(sup.hangs, 1)
        self.assertTrue(self.proc.terminated)

    def test_unchanged_sequence_cannot_keep_hung_child_alive(self):
        sup = self.setup_supervisor(beats=lambda me: me.beat(seq=0))
        self.assertEqual(sup.run(), 1)
        self.assertEqual(self.clock(), 2)
        self.assertEqual(sup.hangs, 1)

    def test_wrong_pid_or_session_or_boolean_sequence_is_ignored(self):
        for change in ({"pid": -1}, {"run_id": "previous-process"}, {"cycle": True}):
            with self.subTest(change=change):
                sup = self.setup_supervisor(beats=lambda me: me.beat(**change))
                self.assertEqual(sup.run(), 1)
                self.assertEqual(self.clock(), 4)

    def test_advancing_sequence_survives_wall_clock_jumps(self):
        sup = self.setup_supervisor(beats=lambda me: me.beat(seq=int(me.clock())),
                                    wall=lambda: -1e9)
        self.proc.clock, self.proc.exit_at = self.clock, 10
        self.assertEqual(sup.run(), 0)
        self.assertEqual(sup.hangs, 0)
        self.assertEqual(self.clock(), 10)

    def test_kill_heartbeat_stops_without_restart(self):
        sup = self.setup_supervisor(beats=lambda me: me.beat(state="kill"), max_restarts=5)
        self.assertEqual(sup.run(), SAFETY_HOLD_EXIT)
        self.assertTrue(self.proc.terminated)
        self.assertEqual(sup.restarts, 0)

    def test_keyboard_interrupt_reaps_child(self):
        def interrupt(seconds):
            raise KeyboardInterrupt
        sup = self.setup_supervisor(sleep=interrupt)
        self.assertEqual(sup.run(), 0)
        self.assertTrue(self.proc.terminated)
        self.assertEqual(sup.restarts, 0)

    def test_child_ignoring_term_is_killed(self):
        sup = self.setup_supervisor(FakeProcess(ignore_term=True))
        self.assertEqual(sup.run(), 1)
        self.assertTrue(self.proc.terminated)
        self.assertTrue(self.proc.killed)

    def test_spawn_failure_spends_budget_not_unbounded_loop(self):
        spawn = mock.Mock(side_effect=FileNotFoundError("missing executable"))
        sup = self.setup_supervisor(spawn=spawn, max_restarts=1)
        self.assertEqual(sup.run(), 1)
        self.assertEqual(spawn.call_count, 2)
        self.assertIn("missing executable", Path(sup.last_report).read_text())

    def test_real_noisy_child_pipe_is_drained_and_tail_bounded(self):
        script = "import sys; sys.stdout.write('x'*1000000+'TAIL-END'); sys.stdout.flush(); sys.exit(3)"
        sup = Supervisor([sys.executable, "-u", "-c", script], heartbeat_path=self.path,
                         crash_dir=self.tmp.name, budget=RestartBudget(0),
                         startup_grace=5, watch_slice=.05)
        self.assertEqual(sup.run(), 1)
        self.assertEqual(sup.hangs, 0)
        body = Path(sup.last_report).read_text()
        self.assertIn("TAIL-END", body)
        self.assertLess(len(body), 65_000)

    def test_real_missing_heartbeat_child_is_reaped(self):
        captured = []
        def spawn(cmd, **kwargs):
            proc = subprocess.Popen(cmd, **kwargs)
            captured.append(proc)
            return proc
        sup = Supervisor([sys.executable, "-c", "import time; time.sleep(60)"],
                         heartbeat_path=self.path, crash_dir=self.tmp.name,
                         budget=RestartBudget(0), startup_grace=.2, watch_slice=.05,
                         spawn=spawn)
        try:
            self.assertEqual(sup.run(), 1)
            self.assertEqual(sup.hangs, 1)
            self.assertIsNotNone(captured[0].poll())
        finally:
            for proc in captured:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=2)


class TestRuntimeWiring(TempCase):
    def test_supervise_cli_forwards_config_without_live_flags(self):
        args = build_parser().parse_args(["--config", "custom.json", "supervise",
                                          "--scenario", "range_chop", "--max-restarts", "2"])
        with mock.patch("cybertrade.cli._load_config", return_value=AppConfig()), \
             mock.patch("cybertrade.watchdog.Supervisor") as sup:
            sup.return_value.run.return_value = 0
            self.assertEqual(cmd_supervise(args), 0)
        cmd = sup.call_args.args[0]
        self.assertLess(cmd.index("--config"), cmd.index("run"))
        self.assertIn("--supervised", cmd)
        self.assertIn("range_chop", cmd)
        for flag in ("--live", "--yes", "--dry-run"):
            self.assertNotIn(flag, cmd)

    def test_supervise_refuses_nonpaper_and_disabled_persistence(self):
        args = build_parser().parse_args(["supervise"])
        for mode in ("quotex", "dryrun"):
            cfg = AppConfig()
            cfg.broker.mode = mode
            with mock.patch("cybertrade.cli._load_config", return_value=cfg), \
                 mock.patch("cybertrade.watchdog.Supervisor") as sup:
                self.assertEqual(cmd_supervise(args), 2)
                sup.assert_not_called()
        cfg = AppConfig()
        cfg.continuity_path = ""
        with mock.patch("cybertrade.cli._load_config", return_value=cfg):
            self.assertEqual(cmd_supervise(args), 2)

    def test_supervised_child_cannot_bypass_parent_live_rail(self):
        args = build_parser().parse_args(["run", "--supervised", "--live", "--yes"])
        with mock.patch("cybertrade.cli._load_config", return_value=AppConfig()), \
             mock.patch("cybertrade.cli._build_engine") as build:
            self.assertEqual(cmd_run(args), SAFETY_HOLD_EXIT)
            build.assert_not_called()

    def test_saved_live_permission_is_reset_without_live_request(self):
        args = build_parser().parse_args(["run"])
        cfg = AppConfig()
        cfg.risk.allow_live = True
        with mock.patch("cybertrade.cli._load_config", return_value=cfg), \
             mock.patch("cybertrade.cli._build_engine") as build:
            build.return_value.arm.side_effect = KillSwitchEngaged("test stop")
            self.assertEqual(cmd_run(args), SAFETY_HOLD_EXIT)
            self.assertFalse(cfg.risk.allow_live)
            self.assertTrue(build.call_args.kwargs["durable"])
            build.return_value.shutdown.assert_called_once()

    def test_timeout_default_scales_with_long_timeframe(self):
        cfg = AppConfig()
        cfg.strategy.timeframe = "1h"
        args = build_parser().parse_args(["supervise"])
        with mock.patch("cybertrade.cli._load_config", return_value=cfg), \
             mock.patch("cybertrade.watchdog.Supervisor") as sup:
            sup.return_value.run.return_value = 0
            cmd_supervise(args)
            self.assertGreater(sup.call_args.kwargs["max_stale"], 600)
        args.stale_seconds = 10
        with mock.patch("cybertrade.cli._load_config", return_value=cfg):
            self.assertEqual(cmd_supervise(args), 2)

    def test_both_huds_surface_hold_and_paper_resolution_is_explicit(self):
        html = (ROOT / "cybertrade/web/static/index.html").read_text()
        js = (ROOT / "cybertrade/web/static/js/app.js").read_text()
        desktop = (ROOT / "cybertrade/gui/app.py").read_text()
        self.assertIn('id="recovery-status"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("renderRecovery(snap.continuity", js)
        self.assertIn("button.disabled = blocked", js)
        self.assertIn("window.confirm", js)
        self.assertIn('cmd: "resolve_paper"', js)
        self.assertIn("self.recovery_led.pack(fill=", desktop)
        self.assertIn('state.get("continuity"', desktop)


if __name__ == "__main__":
    unittest.main()
