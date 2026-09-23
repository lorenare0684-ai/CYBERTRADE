"""Phase-3 regression tests: regime crash-echo guard + ensemble consensus gates."""

from __future__ import annotations

import math
import unittest

from cybertrade.constants import MarketRegime, Side
from cybertrade.data.models import Candle
from cybertrade.data.synthetic import generate_candles
from cybertrade.regime.detector import RegimeDetector
from cybertrade.strategies.base import Strategy, StrategyContext
from cybertrade.strategies.ensemble import AllWeatherEnsemble


def _zig_candles(n: int, start: float = 1.0, drift: float = 0.0004,
                 noise: float = 0.0002, tf: int = 60, ts0: float = 1700000000.0):
    """Calm tape with tiny zig noise (small ret_sd) and gentle drift."""
    out = []
    price = start
    for i in range(n):
        o = price
        c = price * (1 + drift) + (noise if i % 2 else -noise)
        c = max(c, 1e-6)
        out.append(Candle(
            asset="EURUSD", timeframe_seconds=tf, open_ts=ts0 + i * tf,
            open=o, high=max(o, c) * 1.0001, low=min(o, c) * 0.9999,
            close=c, volume=10.0, closed=True,
        ))
        price = c
    return out


def _crash_bar(prev: Candle, pct: float = -0.03, ts_off: int = 1):
    o = prev.close
    c = prev.close * (1 + pct)
    return Candle(
        asset="EURUSD", timeframe_seconds=60,
        open_ts=prev.open_ts + 60 * ts_off,
        open=o, high=o, low=c * 0.999, close=c, volume=10.0, closed=True,
    )


def _bounce_bars(prev: Candle, n: int = 3, step: float = 0.006):
    out = []
    last = prev
    for i in range(n):
        o = last.close
        c = last.close * (1 + step)
        out.append(Candle(
            asset="EURUSD", timeframe_seconds=60,
            open_ts=last.open_ts + 60,
            open=o, high=c * 1.0001, low=o * 0.9999,
            close=c, volume=10.0, closed=True,
        ))
        last = out[-1]
    return out


class TestCrashEchoGuard(unittest.TestCase):
    """Flash-crash bounces must never be classified as fresh trends."""

    def setUp(self):
        self.det = RegimeDetector()
        self.calm = _zig_candles(70)
        self.crash = _crash_bar(self.calm[-1])
        self.bounce = _bounce_bars(self.crash, n=3)
        self.pre = self.calm
        self.at_crash = self.calm + [self.crash]
        self.in_echo = self.calm + [self.crash] + self.bounce

    def test_prewarm_calm_is_not_crisis(self):
        r = self.det.assess(self.pre)
        self.assertNotEqual(r.regime, MarketRegime.CRISIS)

    def test_crash_bar_is_crisis(self):
        r = self.det.assess(self.at_crash)
        self.assertEqual(r.regime, MarketRegime.CRISIS)

    def test_bounce_is_not_bull_trend(self):
        for i in range(1, 4):
            r = self.det.assess(self.in_echo[: 71 + i])
            self.assertNotEqual(
                r.regime, MarketRegime.BULL_TREND,
                f"bounce bar {i} misclassified as BULL_TREND",
            )
            self.assertIn(r.regime, (MarketRegime.CRISIS, MarketRegime.HIGH_VOL,
                                     MarketRegime.RANGE, MarketRegime.GAP))
        r = self.det.assess(self.in_echo)
        self.assertTrue(r.details.get("crash_echo"))

    def test_healthy_trend_still_reads_as_trend(self):
        candles = _zig_candles(80, drift=0.0018)
        r = self.det.assess(candles)
        self.assertIn(r.regime, (MarketRegime.BULL_TREND, MarketRegime.HIGH_VOL,
                                 MarketRegime.RANGE))

    def test_synthetic_flash_crash_scenario_never_clean_bull_in_echo(self):
        # generated flash_crash path: wherever the crash lands, the bars right
        # after a >3.5σ down bar must not be BULL_TREND
        for seed in (3, 11, 29):
            candles = generate_candles("flash_crash", bars=240, seed=seed)
            det = RegimeDetector()
            rets = [math.log(candles[i].close / candles[i - 1].close)
                    for i in range(1, len(candles))]
            import statistics

            sd = statistics.stdev(rets[-20:])
            for i in range(40, len(candles) - 2):
                window = candles[: i + 1]
                r = det.assess(window)
                recent = rets[max(0, i - 40): i]
                crashed = sd > 0 and min(recent) <= -3.5 * sd
                if crashed and r.details.get("crash_echo"):
                    self.assertNotEqual(
                        r.regime, MarketRegime.BULL_TREND,
                        f"seed={seed} bar={i}: crash echo read as BULL_TREND",
                    )


class _Stub(Strategy):
    def __init__(self, name: str, side: Side | None, conf: float = 0.7, **kw):
        self._side = side
        self._conf = conf
        self.name = name
        super().__init__(**kw)

    def decide(self, ctx: StrategyContext):
        if self._side is None:
            return None
        return self._signal(ctx, self._side, self._conf, f"stub {self._side.value}")


def _ctx(n: int = 40) -> StrategyContext:
    candles = _zig_candles(n)
    return StrategyContext(asset="EURUSD", candles=candles, timeframe_seconds=60,
                           ts=candles[-1].close_ts + 1)


class TestEnsembleGates(unittest.TestCase):
    """Consensus discipline: thin pluralities and solo self-consensus die."""

    def test_unanimous_all_put_fires_put(self):
        # regression: precedence bug let all-PUT votes fire CALL
        ens = AllWeatherEnsemble(
            members=[_Stub("a", Side.PUT, 0.8), _Stub("b", Side.PUT, 0.7)],
            mode="unanimous",
        )
        sig = ens.generate(_ctx())
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, Side.PUT)

    def test_unanimous_all_call_fires_call(self):
        ens = AllWeatherEnsemble(
            members=[_Stub("a", Side.CALL, 0.8), _Stub("b", Side.CALL, 0.7)],
            mode="unanimous",
        )
        sig = ens.generate(_ctx())
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, Side.CALL)

    def test_unanimous_mixed_stands_down(self):
        ens = AllWeatherEnsemble(
            members=[_Stub("a", Side.CALL, 0.9), _Stub("b", Side.PUT, 0.9)],
            mode="unanimous",
        )
        self.assertIsNone(ens.generate(_ctx()))

    def test_solo_mediocre_vote_rejected(self):
        # old formula minted conf ~0.80 from a single 0.55 vote; now ~0.50
        ens = AllWeatherEnsemble(members=[_Stub("solo", Side.CALL, 0.56)],
                                 mode="regime_weighted")
        self.assertIsNone(ens.generate(_ctx()))

    def test_solo_strong_vote_fires(self):
        ens = AllWeatherEnsemble(members=[_Stub("solo", Side.CALL, 0.85)],
                                 mode="regime_weighted")
        sig = ens.generate(_ctx())
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, Side.CALL)

    def test_thin_conflict_rejected(self):
        ens = AllWeatherEnsemble(
            members=[_Stub("bull", Side.CALL, 0.7), _Stub("bear", Side.PUT, 0.7)],
            mode="regime_weighted",
        )
        self.assertIsNone(ens.generate(_ctx()))

    def test_corroborated_majority_fires(self):
        ens = AllWeatherEnsemble(
            members=[_Stub("b1", Side.CALL, 0.7), _Stub("b2", Side.CALL, 0.65),
                     _Stub("s", Side.PUT, 0.6)],
            mode="regime_weighted",
        )
        sig = ens.generate(_ctx())
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, Side.CALL)

    def test_majority_mode_rejects_razor_margin(self):
        ens = AllWeatherEnsemble(
            members=[_Stub("a", Side.CALL, 0.7), _Stub("b", Side.CALL, 0.7),
                     _Stub("c", Side.PUT, 0.7), _Stub("d", Side.PUT, 0.7),
                     _Stub("e", Side.PUT, 0.65)],
            mode="majority",
        )
        # 3/5 = 0.6 ratio passes; but 2v1 razor (0.667) with low conf may fire.
        # razor: 2 vs 1
        ens2 = AllWeatherEnsemble(
            members=[_Stub("a", Side.CALL, 0.6), _Stub("b", Side.CALL, 0.6),
                     _Stub("c", Side.PUT, 0.9)],
            mode="majority",
        )
        sig = ens2.generate(_ctx())
        # 2/3 majority at conf 0.6 -> 0.6*(0.5+0.333)=0.5 -> rejected
        self.assertIsNone(sig)


if __name__ == "__main__":
    unittest.main()
