"""Phase 32: durable governor recovery and live venue reconciliation.

Every runtime is a live venue runtime now: the checkpoint carries the risk
governor and ledger, positions belong to the venue and are re-adopted by
reconciliation, and the supervisor/backtest/dry-run harnesses are gone.
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
from cybertrade.cli import build_parser, cmd_run
from cybertrade.config import AppConfig, RiskConfig
from cybertrade.constants import EngineState, Side
from cybertrade.data.feed import Feed
from cybertrade.data.models import AccountSnapshot, Fill, Order, Position, Tick
from cybertrade.exceptions import ConfigError, KillSwitchEngaged
from cybertrade.execution.broker import Broker
from cybertrade.risk.manager import RiskManager
from cybertrade.statestore import (
    StateError, StateLease, heartbeat_age, load_continuity, load_operator_state,
    load_state, pack_position, read_heartbeat, save_continuity, save_state,
    unpack_position, write_heartbeat,
)
from cybertrade.utils import timex
from cybertrade.shutdown import SAFETY_HOLD_EXIT, stop_on_sigterm
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


class RecordingVenue(Broker):
    """A venue that fills orders locally but never leaves the process.

    Recovery tests need a broker that *has* positions to reconcile, so the
    fill happens here and the balance is escrowed exactly like the venue.
    """
    def __init__(self, balance=1000.0, user="account-a"):
        super().__init__()
        self.balance = balance
        self.api = SimpleNamespace(session=SimpleNamespace(user_id=user))
        self.submits = 0
        self._connected = False
        self._positions = {}

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
        return getattr(self, "_px", 1.1)

    def on_tick(self, tick):
        self._px = tick.price

    def payout_for(self, asset, expiry_seconds):
        return .85

    def submit(self, order):
        self.submits += 1
        fill = Fill(
            order_id=order.id, asset=order.asset, side=order.side,
            price=order.limit_price or self.last_price(order.asset),
            amount=order.amount, payout=order.payout or 0.85, ts=order.ts,
        )
        self.balance -= order.amount
        pos = Position(fill=fill, expiry_ts=order.ts + order.expiry_seconds,
                       strategy=order.meta.get("strategy") or "")
        self._positions[pos.id] = pos
        return fill

    def open_positions(self):
        return list(self._positions.values())

    def settle_due(self, now=None):
        return []

    def close_position(self, position_id):
        return self._positions.pop(position_id, None) is not None


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
                            broker=broker or RecordingVenue())
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

    def test_new_account_or_mode_does_not_load_another_book(self):
        eng = self.engine()
        self.close(eng)
        self.cfg.broker.mode = "quotex"
        restored = self.engine(broker=RecordingVenue(user="account-b"))
        self.assertIn("different mode/account", restored.continuity.fault)


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
        first = self.engine(broker=RecordingVenue(balance=1000))
        self.close(first)
        venue = RecordingVenue(balance=975)
        restored = self.engine(broker=venue)
        self.assertTrue(restored.continuity.restored)
        self.assertFalse(restored.continuity.fault)
        # the venue balance is the only cash there is; nothing local is credited
        self.assertEqual(restored.oms.ledger.balance, 975)
        self.assertEqual(restored.risk.state.day_start_balance, 1000)
        self.assertEqual(venue.submits, 0)
        # there is no local paper resolution any more — the venue decides
        self.assertFalse(restored.oms.resolve_recovery("anything", 1.1))

    def test_open_exposure_is_held_for_venue_reconciliation(self):
        first = self.engine(broker=RecordingVenue(balance=1000))
        self.order(first)
        self.close(first)
        # a venue that no longer holds the contract: the exposure is held,
        # never settled from local cash and never replayed as an order
        venue = RecordingVenue(balance=990)
        restored = self.engine(broker=venue)
        self.assertIn("venue reconciliation", restored.continuity.fault)
        self.assertTrue(restored.risk.state.kill)
        self.assertEqual(venue.submits, 0)
        with self.assertRaises(KillSwitchEngaged):
            restored.arm()

    def test_exposure_without_a_venue_order_cannot_be_checkpointed(self):
        eng = self.engine(broker=RecordingVenue())
        with self.assertRaises(StateError) as ctx:
            with eng.oms.transaction("test-live-exposure"):
                eng.risk.on_open(Order(ASSET, Side.CALL, 10))
        self.assertIn("does not match the live order registry", str(ctx.exception))

    def test_authenticated_account_identity_must_match(self):
        first = self.engine(broker=RecordingVenue(user="account-a"))
        self.close(first)
        restored = self.engine(broker=RecordingVenue(user="account-b"))
        self.assertIn("different mode/account", restored.continuity.fault)

    def test_live_account_id_can_come_from_venue_balance_payload(self):
        from cybertrade.brokers.quotex.models import QXBalance
        venue = RecordingVenue(user="")
        venue.api.balance = QXBalance.from_payload({"userId": "from-venue", "balance": 1000})
        eng = self.engine(broker=venue)
        self.assertFalse(eng.continuity.fault)
        self.assertEqual(load_continuity(self.path)["scope"]["account"], "from-venue")

    def test_runtime_account_switch_is_blocked_before_order_submission(self):
        venue = RecordingVenue()
        eng = self.engine(broker=venue)
        before = Path(self.path).read_bytes()
        venue.api.session.user_id = "different-account"
        with self.assertRaises(StateError):
            eng.oms.submit(ASSET, Side.CALL, 10, 60, payout=.85)
        self.assertEqual(venue.submits, 0)
        self.assertIn("runtime mode/account changed", eng.continuity.fault)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_conflicting_venue_identity_sources_fail_closed(self):
        venue = RecordingVenue(user="one")
        venue.api.balance = SimpleNamespace(user_id="other")
        eng = self.engine(broker=venue)
        self.assertIn("identities disagree", eng.continuity.fault)

    def test_unknown_live_identity_fails_closed(self):
        eng = self.engine(broker=RecordingVenue(user=""))
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


class TestShutdownContract(TempCase):
    """The supervisor/watchdog harness is gone; what survives is the contract
    a live runtime still owes the operator: bounded exit codes and a signal
    handler that disarms instead of dying silently."""

    def test_safety_hold_exit_code_is_bounded_and_distinct(self):
        self.assertEqual(SAFETY_HOLD_EXIT, 78)
        self.assertNotEqual(SAFETY_HOLD_EXIT, 0)
        self.assertNotEqual(SAFETY_HOLD_EXIT, 2)

    def test_sigterm_becomes_keyboard_interrupt(self):
        seen = []
        with mock.patch("signal.signal") as sig:
            with stop_on_sigterm():
                self.assertEqual(sig.call_count, 1)     # SIGTERM is the wire
                handler = sig.call_args.args[1]
            self.assertEqual(sig.call_count, 2)         # restored on exit
        with self.assertRaises(KeyboardInterrupt):
            handler(15, None)
        self.assertEqual(seen, [])

    def test_sigterm_off_the_main_thread_is_a_noop(self):
        done = []
        thread = threading.Thread(
            target=lambda: stop_on_sigterm().__enter__() or done.append(True))
        thread.start()
        thread.join(2)
        self.assertEqual(done, [True])


    def test_parser_rejects_deleted_commands(self):
        parser = build_parser()
        for command in ("supervise", "backtest", "optimize", "drill", "scenarios"):
            with self.assertRaises(SystemExit):
                with mock.patch("sys.stderr", new=io.StringIO()):
                    parser.parse_args([command])

    def test_live_commands_survive(self):
        parser = build_parser()
        for command in (("run",), ("web",), ("gui",), ("quotex", "status"),
                        ("journal",), ("strategies",), ("calendar",), ("edge",),
                        ("montecarlo",), ("calibrate",), ("doctor",)):
            self.assertIsNotNone(parser.parse_args(list(command)), command)


class TestRuntimeWiring(TempCase):
    def test_run_holds_on_a_kill_switch_and_clears_the_saved_permission(self):
        args = build_parser().parse_args(["run", "--demo"])
        cfg = AppConfig()
        cfg.risk.allow_live = True
        with mock.patch("cybertrade.cli._load_config", return_value=cfg), \
             mock.patch("cybertrade.cli._resolve_purse"), \
             mock.patch("cybertrade.cli._confirm_live", lambda a, c: True), \
             mock.patch("cybertrade.cli._build_engine") as build:
            build.return_value.arm.side_effect = KillSwitchEngaged("test stop")
            self.assertEqual(cmd_run(args), SAFETY_HOLD_EXIT)
            self.assertTrue(build.call_args.kwargs["durable"])
            build.return_value.shutdown.assert_called_once()

    def test_run_aborts_before_the_venue_when_the_gate_refuses(self):
        args = build_parser().parse_args(["run", "--demo"])
        with mock.patch("cybertrade.cli._load_config", return_value=AppConfig()), \
             mock.patch("cybertrade.cli._resolve_purse"), \
             mock.patch("cybertrade.cli._confirm_live", lambda a, c: False), \
             mock.patch("cybertrade.cli._build_engine") as build:
            self.assertEqual(cmd_run(args), 1)
            build.assert_not_called()

    def test_run_needs_a_purse_before_anything_live(self):
        args = build_parser().parse_args(["run"])
        with mock.patch("cybertrade.cli._load_config", return_value=AppConfig()), \
             mock.patch("cybertrade.cli._resolve_purse",
                        side_effect=ConfigError("no purse chosen")), \
             mock.patch("cybertrade.cli._build_engine") as build:
            self.assertEqual(cmd_run(args), SAFETY_HOLD_EXIT)
            build.assert_not_called()

    def test_both_huds_surface_the_hold(self):
        html = (ROOT / "cybertrade/web/static/index.html").read_text()
        js = (ROOT / "cybertrade/web/static/js/app.js").read_text()
        desktop = (ROOT / "cybertrade/gui/app.py").read_text()
        self.assertIn('id="recovery-status"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("renderRecovery(snap.continuity", js)
        self.assertIn("button.disabled = blocked", js)
        self.assertIn("window.confirm", js)
        self.assertIn("self.recovery_led.pack(fill=", desktop)


if __name__ == "__main__":
    unittest.main()
