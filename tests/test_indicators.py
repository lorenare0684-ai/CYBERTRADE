"""Indicator and pattern correctness tests."""

from __future__ import annotations

import math
import unittest

from cybertrade.data.synthetic import generate_candles
from cybertrade.indicators import (
    adx,
    atr,
    bollinger_bands,
    crossover,
    ema,
    ichimoku,
    macd,
    psar,
    rsi,
    supertrend,
)
from cybertrade.indicators import patterns
from cybertrade.indicators.core import alma, dema, donchian, hma, kama, rma, sma, stoch_rsi, tema, vwma, wma
from cybertrade.indicators.momentum import cci, mfi, stochastic, williams_r
from cybertrade.indicators.volatility import (
    bandwidth,
    bollinger_bands as bb2,
    garch_11,
    historical_volatility,
    squeeze_momentum,
)
from cybertrade.indicators.volume import obv, vwap, chaikin_money_flow


def _ohlc(n=300, seed=5):
    cs = generate_candles("bull_trend", bars=n, seed=seed)
    return (
        [c.open for c in cs],
        [c.high for c in cs],
        [c.low for c in cs],
        [c.close for c in cs],
        [c.volume for c in cs],
    )


class TestMovingAverages(unittest.TestCase):
    def setUp(self):
        _, _, _, self.c, _ = _ohlc()

    def test_sma_known(self):
        out = sma([1, 2, 3, 4, 5], 3)
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[4], 4.0)

    def test_ema_seeded(self):
        out = ema([1.0] * 50, 10)
        self.assertAlmostEqual(out[-1], 1.0)

    def test_wma_weights(self):
        out = wma([1, 2, 3], 3)
        # (1*1 + 2*2 + 3*3) / 6 = 14/6
        self.assertAlmostEqual(out[2], 14 / 6)

    def test_hull_tracks_price(self):
        out = hma(self.c, 20)
        self.assertLess(abs(out[-1] - self.c[-1]) / self.c[-1], 0.05)

    def test_all_mas_converge_on_constant(self):
        series = [3.14] * 120
        for fn in (sma, ema, wma, rma, dema, tema, hma, kama, alma):
            out = fn(series, 10)
            self.assertAlmostEqual(out[-1], 3.14, places=6, msg=fn.__name__)

    def test_vwma_constant(self):
        out = vwma([2.0] * 30, [5.0] * 30, 5)
        self.assertAlmostEqual(out[-1], 2.0)


class TestOscillators(unittest.TestCase):
    def setUp(self):
        _, self.h, self.l, self.c, self.v = _ohlc()

    def test_rsi_bounds(self):
        r = rsi(self.c, 14)
        vals = [v for v in r if v is not None]
        self.assertTrue(all(0 <= v <= 100 for v in vals))

    def test_rsi_all_up_is_100(self):
        up = [float(i) for i in range(1, 40)]
        r = rsi(up, 14)
        self.assertAlmostEqual(r[-1], 100.0)

    def test_macd_shapes(self):
        line, sig, hist = macd(self.c)
        self.assertEqual(len(line), len(self.c))
        self.assertIsNotNone(line[-1])
        self.assertAlmostEqual(hist[-1], line[-1] - sig[-1])

    def test_stochastic_range(self):
        k, d = stochastic(self.h, self.l, self.c)
        vals = [v for v in k if v is not None]
        self.assertTrue(all(-1e-6 <= v <= 100 + 1e-6 for v in vals))

    def test_cci_reasonable(self):
        vals = [v for v in cci(self.h, self.l, self.c) if v is not None]
        self.assertTrue(all(abs(v) < 1000 for v in vals))

    def test_stoch_rsi(self):
        k, d = stoch_rsi(self.c, 14, 3, 3)
        self.assertEqual(len(k), len(self.c))

    def test_crossover_detects(self):
        a = [None, 1.0, 1.0, 3.0, 3.0]
        b = [None, 2.0, 2.0, 2.0, 2.0]
        cross = crossover(a, b)
        self.assertTrue(cross[3])


class TestChannels(unittest.TestCase):
    def setUp(self):
        self.o, self.h, self.l, self.c, self.v = _ohlc()

    def test_bollinger_ordering(self):
        up, mid, low, pctb = bollinger_bands(self.c, 20, 2.0)
        for i in (-1, -20, -50):
            self.assertGreater(up[i], mid[i])
            self.assertGreater(mid[i], low[i])

    def test_donchian(self):
        up, mid, low = donchian(self.h, self.l, 20)
        self.assertGreaterEqual(up[-1], max(self.h[-20:]))
        self.assertLessEqual(low[-1], min(self.l[-20:]))

    def test_atr_positive(self):
        a = atr(self.h, self.l, self.c, 14)
        self.assertGreater(a[-1], 0)

    def test_bandwidth(self):
        bw = bandwidth(self.c, 20)
        self.assertGreater(bw[-1], 0)

    def test_squeeze_runs(self):
        s, m = squeeze_momentum(self.h, self.l, self.c)
        self.assertEqual(len(s), len(self.c))

    def test_ichimoku(self):
        t, k, a, b, ch = ichimoku(self.h, self.l, self.c)
        self.assertIsNotNone(t[-1])
        self.assertEqual(len(t), len(self.c))


class TestTrend(unittest.TestCase):
    def setUp(self):
        _, self.h, self.l, self.c, _ = _ohlc()

    def test_adx_range(self):
        line, plus, minus = adx(self.h, self.l, self.c, 14)
        vals = [v for v in line if v is not None]
        self.assertTrue(all(0 <= v <= 100 for v in vals))

    def test_supertrend_direction(self):
        line, direction = supertrend(self.h, self.l, self.c)
        self.assertTrue(set(direction) <= {-1, 0, 1})

    def test_psar(self):
        s = psar(self.h, self.l)
        self.assertEqual(len(s), len(self.c))
        self.assertIsNotNone(s[-1])


class TestVolVolume(unittest.TestCase):
    def setUp(self):
        _, self.h, self.l, self.c, self.v = _ohlc()

    def test_garch_positive(self):
        g = garch_11(self.c)
        self.assertGreater(g[-1], 0)

    def test_hist_vol(self):
        hv = historical_volatility(self.c, 20)
        self.assertGreater(hv[-1], 0)

    def test_obv(self):
        o = obv(self.c, self.v)
        self.assertEqual(len(o), len(self.c))

    def test_vwap(self):
        v = vwap(self.h, self.l, self.c, self.v)
        self.assertIsNotNone(v[-1])

    def test_cmf_bounds(self):
        vals = [v for v in chaikin_money_flow(self.h, self.l, self.c, self.v) if v is not None]
        self.assertTrue(all(-1.001 <= v <= 1.001 for v in vals))


class TestPatterns(unittest.TestCase):
    def test_bull_engulfing(self):
        o = [5.0, 5.0, 4.5]
        h = [5.2, 5.1, 5.3]
        l = [4.8, 4.4, 4.4]
        c = [5.0, 4.6, 5.2]
        marks = patterns.engulfing(o, h, l, c)
        self.assertEqual(marks[-1], 1)

    def test_hammer(self):
        o = [5.0, 5.0]
        h = [5.1, 5.1]
        l = [4.9, 4.0]
        c = [5.0, 5.02]
        marks = patterns.hammer(o, h, l, c)
        self.assertEqual(marks[-1], 1)

    def test_marubozu(self):
        marks = patterns.marubozu([4.0], [5.0], [4.0], [5.0])
        self.assertEqual(marks[0], 1)

    def test_score_patterns_runs(self):
        cs = generate_candles("range_chop", bars=80)
        o = [c.open for c in cs]
        h = [c.high for c in cs]
        l = [c.low for c in cs]
        cl = [c.close for c in cs]
        score, details = patterns.score_patterns(o, h, l, cl)
        self.assertEqual(len(score), len(cs))
        self.assertTrue(all(-1 <= s <= 1 for s in score))


if __name__ == "__main__":
    unittest.main()
