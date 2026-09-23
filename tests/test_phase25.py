"""Phase-25 tests — the quarantine ward: decay flags now block trades.

P18 maintained `_decay_alerted` but only ALERTed: nothing isolated the
fading edge, and live journal rows were keyed by the ensemble blob
('ensemble_all_weather') so per-strategy decay never fired outside rigged
tests. P25 attributes orders to the strongest same-side voter, vetoes
quarantined names in `_try_execute`, skips them as ensemble voters, and
surfaces the ward on the HUD.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from cybertrade.bot.engine import TradingEngine
from cybertrade.config import AppConfig
from cybertrade.data.feed import SyntheticFeed
from cybertrade.data.models import Settlement, Side, Signal, TradeRecord
from cybertrade.events import Topic, default_bus
from cybertrade.execution.paper import PaperBroker
from cybertrade.regime.detector import RegimeReading
from cybertrade.strategies.registry import build_all_weather


def _rec(i: int, won: bool, strategy: str = "alpha") -> TradeRecord:
    return TradeRecord(
        settlement=Settlement(
            fill_id=f"f{i}", order_id=f"o{i}", asset="EURUSD_otc",
            side=Side.CALL, strike=1.0, expiry_price=1.01 if won else 0.99,
            stake=10.0, payout=0.85, won=won, ts=1000.0 + i, id=f"set{i}",
        ),
        strategy=strategy, regime="range",
    )


class TestAttribution(unittest.TestCase):
    def test_top_same_side_voter_wins(self):
        sig = Signal(
            asset="EURUSD_otc", side=Side.CALL, confidence=0.8,
            strategy="ensemble_all_weather",
            meta={"votes": [
                {"strategy": "weak_voter", "side": "call", "confidence": 0.5},
                {"strategy": "strong_voter", "side": "call", "confidence": 0.9},
                {"strategy": "other_side", "side": "put", "confidence": 0.99},
            ]},
        )
        self.assertEqual(TradingEngine._attributed_strategy(sig), "strong_voter")

    def test_no_votes_keeps_own_name(self):
        sig = Signal(asset="EURUSD_otc", side=Side.CALL, confidence=0.8,
                     strategy="manual", meta={})
        self.assertEqual(TradingEngine._attributed_strategy(sig), "manual")

    def test_try_execute_submits_attributed_name(self):
        import inspect
        src = inspect.getsource(TradingEngine._try_execute)
        self.assertIn("attributed = self._attributed_strategy(signal)", src)
        self.assertIn("strategy=attributed", src)


class TestQuarantineWard(unittest.TestCase):
    def _engine(self) -> TradingEngine:
        cfg = AppConfig()
        tmp = tempfile.mkdtemp()
        cfg.journal_path = os.path.join(tmp, "j.db")
        cfg.calibration_path = os.path.join(tmp, "c.json")
        return TradingEngine(
            cfg, feed=SyntheticFeed(tick_interval=60.0),
            broker=PaperBroker(starting_balance=1000.0),
        )

    def _rig_decaying(self, eng: TradingEngine) -> None:
        # window=10 needs 20 rows: 8 wins then 13 losses (phase18 pattern)
        for i in range(8):
            eng._on_settle(_rec(i, True))
        for i in range(8, 21):
            eng._on_settle(_rec(i, False))

    def test_decay_engages_ward_and_syncs_ensemble(self):
        eng = self._engine()
        eng.boot()
        try:
            self._rig_decaying(eng)
            self.assertIn("alpha", eng._decay_alerted)
            self.assertIn("alpha", eng.quarantine_report())
            self.assertIn("alpha", eng.ensemble.quarantined_votes)
        finally:
            eng.shutdown()

    def test_quarantine_vetoes_trade(self):
        eng = self._engine()
        eng.boot()
        try:
            self._rig_decaying(eng)
            before = eng.vetoes
            sig = Signal(asset="EURUSD_otc", side=Side.CALL, confidence=0.95,
                         strategy="alpha", reason="should be isolated")
            ok = eng._try_execute(sig, RegimeReading())
            self.assertFalse(ok)
            self.assertEqual(eng.vetoes, before + 1)
            self.assertTrue(any("quarantine veto" in m
                                for m in eng.health.messages), eng.health.messages)
        finally:
            eng.shutdown()

    def test_recovery_releases_ward(self):
        eng = self._engine()
        eng.boot()
        try:
            self._rig_decaying(eng)
            self.assertIn("alpha", eng._decay_alerted)
            for i in range(30, 46):   # win streak clears the flag
                eng._on_settle(_rec(i, True))
            self.assertNotIn("alpha", eng._decay_alerted)
            self.assertNotIn("alpha", eng.ensemble.quarantined_votes)
            self.assertEqual(eng.quarantine_report(), [])
        finally:
            eng.shutdown()

    def test_ward_publishes_quarantine_and_release(self):
        eng = self._engine()
        eng.boot()
        kinds = []
        try:
            with default_bus.subscribe(
                Topic.ALERT, lambda e: kinds.append(e.payload.get("kind"))
            ):
                self._rig_decaying(eng)
                for i in range(30, 46):
                    eng._on_settle(_rec(i, True))
            self.assertIn("quarantine", kinds)
            self.assertIn("release", kinds)
        finally:
            eng.shutdown()

    def test_ensemble_skips_quarantined_voters(self):
        ens = build_all_weather()
        # mirror the adaptive-quarantine rig: every member isolated -> no signal
        ens.quarantined_votes = {m.name for m in ens.members}
        from cybertrade.strategies.base import StrategyContext
        from cybertrade.regime.detector import RegimeReading
        from cybertrade.data.synthetic import generate_candles as gen
        cs = gen("bull_trend", bars=250)
        ctx = StrategyContext(
            asset="EURUSD_otc", candles=cs, regime=RegimeReading(),
            timeframe_seconds=60, expiry_seconds=60, payout=0.85, ts=1.0,
        )
        self.assertIsNone(ens.generate(ctx))
        ens.quarantined_votes = set()

    def test_web_block_exposes_quarantine(self):
        from cybertrade.web.server import EngineHub
        eng = self._engine()
        eng.boot()
        try:
            self._rig_decaying(eng)
            hub = EngineHub(eng)
            block = hub._journal_block()
            self.assertTrue(block["available"])
            self.assertEqual(block["quarantined"], ["alpha"])
        finally:
            eng.shutdown()


if __name__ == "__main__":
    unittest.main()
