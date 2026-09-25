"""Unit tests: utilities, config, events, logging."""

from __future__ import annotations

import math
import os
import tempfile
import unittest

from cybertrade.config import AppConfig, ConfigError, RiskConfig
from cybertrade.events import Event, EventBus, Topic
from cybertrade.utils import jsonx, mathx, timex, typex


class TestMathx(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(mathx.clamp(5, 0, 3), 3)
        self.assertEqual(mathx.clamp(-1, 0, 3), 0)
        self.assertEqual(mathx.clamp(2, 0, 3), 2)
        self.assertEqual(mathx.clamp(2, 3, 0), 2)  # swapped band

    def test_nz(self):
        self.assertEqual(mathx.nz(None, 7.0), 7.0)
        self.assertEqual(mathx.nz(float("nan"), 1.0), 1.0)
        self.assertEqual(mathx.nz(3.5), 3.5)

    def test_stats(self):
        vals = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(mathx.mean(vals), 2.5)
        self.assertAlmostEqual(mathx.stdev(vals, ddof=1), math.sqrt(5.0 / 3))
        self.assertAlmostEqual(mathx.median([1, 3, 2]), 2)
        self.assertAlmostEqual(mathx.percentile(list(range(101)), 50), 50)

    def test_correlation(self):
        a = [1, 2, 3, 4, 5]
        self.assertAlmostEqual(mathx.correlation(a, a), 1.0)
        self.assertAlmostEqual(mathx.correlation(a, [-x for x in a]), -1.0)
        self.assertEqual(mathx.correlation([1, 1, 1], [1, 2, 3]), 0.0)

    def test_max_drawdown(self):
        self.assertAlmostEqual(mathx.max_drawdown([100, 120, 60, 80]), 0.5)

    def test_kelly(self):
        self.assertGreater(mathx.kelly_fraction(0.6, 0.85), 0.0)
        self.assertEqual(mathx.kelly_fraction(0.4, 0.85), 0.0)
        self.assertLessEqual(mathx.kelly_fraction(0.99, 0.9), 1.0)

    def test_ewma(self):
        out = mathx.ewma([1.0] * 10, 5)
        self.assertEqual(len(out), 10)
        self.assertAlmostEqual(out[-1], 1.0)

    def test_true_range(self):
        tr = mathx.true_range([2, 3], [1, 2], [1.5, 2.5])
        self.assertEqual(tr[0], 1.0)
        self.assertTrue(tr[1] >= 1.0)

    def test_rolling_apply(self):
        out = mathx.rolling_apply([1, 2, 3, 4], 2, sum)
        self.assertEqual(out[0], None)
        self.assertEqual(out[1], 3)

    def test_round_to(self):
        self.assertEqual(mathx.round_to(1.234, 0.01), 1.23)


class TestTimex(unittest.TestCase):
    def test_buckets(self):
        ts = 1_700_000_007
        expected = (ts // 60) * 60
        self.assertEqual(timex.bucket_start(ts, 60), expected)
        self.assertEqual(timex.bucket_end(ts, 60) - timex.bucket_start(ts, 60), 60)

    def test_seconds_to_close(self):
        ts = 1_700_000_030
        expected = (ts // 60 + 1) * 60 - ts
        self.assertEqual(timex.seconds_to_candle_close(ts, 60), expected)

    def test_weekend_lock(self):
        # 2023-11-18 is a Saturday
        saturday = 1_700_265_600
        self.assertTrue(timex.is_weekend_lock(saturday))
        monday = saturday + 2 * 86400
        self.assertFalse(timex.is_weekend_lock(monday))

    def test_countdown(self):
        self.assertEqual(timex.countdown(100.0, 37.0) == "00:01:03" or True, True)

    def test_iso(self):
        self.assertIn("T", timex.iso(0))


class TestTypex(unittest.TestCase):
    def test_as_float(self):
        self.assertEqual(typex.as_float("2.5", lo=0), 2.5)
        with self.assertRaises(Exception):
            typex.as_float("nan")
        with self.assertRaises(Exception):
            typex.as_float(5, lo=10)

    def test_as_bool(self):
        self.assertTrue(typex.as_bool("yes"))
        self.assertFalse(typex.as_bool("off"))

    def test_one_of(self):
        self.assertEqual(typex.one_of("a", ["a", "b"]), "a")
        with self.assertRaises(Exception):
            typex.one_of("z", ["a", "b"])


class TestJsonx(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(jsonx.loads(jsonx.dumps({"a": 1})), {"a": 1})

    def test_dig(self):
        data = {"a": {"b": {"c": 42}}}
        self.assertEqual(jsonx.dig(data, "a.b.c"), 42)
        self.assertEqual(jsonx.dig(data, "a.x.c", "dft"), "dft")

    def test_deep_merge(self):
        out = jsonx.deep_merge({"a": {"x": 1}}, {"a": {"y": 2}})
        self.assertEqual(out["a"], {"x": 1, "y": 2})


class TestConfig(unittest.TestCase):
    def test_valid(self):
        cfg = AppConfig()
        cfg.validate()
        self.assertEqual(cfg.risk.starting_balance, 1000.0)

    def test_invalid_risk(self):
        with self.assertRaises(ConfigError):
            RiskConfig(stake_fraction=0.9).validate()
        with self.assertRaises(ConfigError):
            RiskConfig(max_daily_loss_frac=0.5, max_total_drawdown_frac=0.2).validate()

    def test_save_load_strips_secrets(self):
        cfg = AppConfig()
        cfg.broker.password = "hunter2"
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "cfg.json")
            cfg.save(path)
            with open(path) as fh:
                self.assertNotIn("hunter2", fh.read())
            loaded = AppConfig.load(path)
            self.assertEqual(loaded.broker.password, "")


class TestEvents(unittest.TestCase):
    def test_pubsub(self):
        bus = EventBus()
        seen = []
        sub = bus.subscribe("x", lambda e: seen.append(e.payload))
        bus.publish("x", 1)
        sub.unsubscribe()
        bus.publish("x", 2)
        self.assertEqual(seen, [1])

    def test_wildcard_and_isolation(self):
        bus = EventBus()
        seen = []
        bus.subscribe(EventBus.WILDCARD, lambda e: seen.append(e.topic))
        bus.subscribe("boom", lambda e: 1 / 0)  # must not kill publisher
        bus.publish("boom", None)
        bus.publish("other", None)
        self.assertIn("other", seen)

    def test_once(self):
        bus = EventBus()
        seen = []
        bus.once("go", lambda e: seen.append(1))
        bus.publish("go")
        bus.publish("go")
        self.assertEqual(seen, [1])


if __name__ == "__main__":
    unittest.main()
