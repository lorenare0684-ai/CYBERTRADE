# CYBERTRADE // NEON PROTOCOL

> **All-weather algorithmic trading terminal for Quotex-style binary options**
> with a full cyberpunk HUD — desktop *and* browser editions — built on the
> Python standard library alone. Zero third-party runtime dependencies.

```
 ██████╗██╗   ██╗██████╗ ███████╗██████╗ ████████╗██████╗  █████╗ ██████╗ ███████╗
██╔════╝╚██╗ ██╔╝██╔══██╗██╔════╝██╔══██╗╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝
██║      ╚████╔╝ ██████╔╝█████╗  ██████╔╝   ██║   ██████╔╝███████║██║  ██║█████╗
██║       ╚██╔╝  ██╔══██╗██╔══╝  ██╔══██╗   ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝
╚██████╗   ██║   ██████╔╝███████╗██████╔╝   ██║   ██║  ██║██║  ██║██████╔╝███████╗
 ╚═════╝   ╚═╝   ╚═════╝ ╚══════╝╚═════╝    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝
```

## ⚠ Read this first

- **No system survives every market condition.** "All-weather" here is a
  design posture (regime detection + a defensive playbook + hard circuit
  breakers), not a guarantee. See [`DISCLAIMER.md`](DISCLAIMER.md).
- **Paper trading is the default.** Live order flow needs `risk.allow_live`,
  `--live`, and an interactive `I UNDERSTAND` confirmation — and even then
  prefer the broker's PRACTICE purse.
- The Quotex bridge is **unofficial** (community-reverse-engineered protocol,
  see [`docs/QUOTEX_PROTOCOL.md`](docs/QUOTEX_PROTOCOL.md)). Automation may
  violate the broker's Terms of Service.

## Quick start

```bash
# self-test the environment (10 checks)
python -m cybertrade doctor

# browser terminal (cyberpunk HUD, live charts, SSE telemetry)
python -m cybertrade web --port 8899 --auto

# desktop terminal (Tkinter; needs python3-tk)
python run_gui.py

# headless paper trading loop
python -m cybertrade run

# the ALL-WEATHER GAUNTLET — every market condition, 10 stress scenarios
python -m cybertrade backtest --bars 600

# walk-forward parameter search (with overfit tripwires)
python -m cybertrade optimize --scenario regime_whipsaw

# strategy / scenario catalogs and journal analytics
python -m cybertrade strategies
python -m cybertrade scenarios
python -m cybertrade journal

# run the 163-test verification suite
python -m unittest discover -s tests
```

## The stack

| Layer | What it is |
|---|---|
| **Indicators (45)** | SMA/EMA/WMA/DEMA/TEMA/HMA/ALMA/KAMA/VWMA, RSI, Stoch-RSI, MACD, PPO, ROC, TSI, ATR, NATR, Bollinger (+bandwidth, %B), Keltner, Donchian, SuperTrend, PSAR, Ichimoku, ADX/DI, Aroon, Vortex, CCI, MFI, Williams %R, Ultimate/Accel-Decel oscillators, Elder Ray, squeeze, GARCH(1,1), EWMA/HV/Parkinson vol, Ulcer, OBV, VWAP, CMF, A/D, Force Index, EOM, Klinger, pivots … |
| **Patterns (12)** | Engulfing, hammer/hanger, morning/evening star, three soldiers/crows, pin bars, marubozu, inside/outside bars, tweezers, harami, doji + blended pattern score |
| **Strategies (33 + ensemble)** | 6 trend, 6 mean-reversion, 5 breakout, 6 momentum, 4 volatility, 5 price-action, defensive veto, and the **ALL-WEATHER ENSEMBLE** (regime-weighted voting, adaptive EWMA performance weights, strategy quarantine, conflict vetoes) |
| **Regime engine** | Trend/range/vol/crisis/gap classifier fusing ADX, regression slope, vol percentile, GARCH, gap scans → `RegimeReading` + stress score |
| **SURVIVOR playbook** | Condition → response matrix on a 5-rung posture ladder (ATTACK → NORMAL → GUARD → DEFENSE → LOCKDOWN): stake scaling, confidence floors, expiry caps, forbidden strategy families, news blackouts, weekend/friday locks, spread/liquidity vetoes |
| **Risk fortress** | Stake bands, fractional-Kelly + vol-target sizing, drawdown governor (daily lock + total kill), loss-streak cooldowns, per-asset / correlation-cluster caps, rate limits, payout floor, strategy win-rate floors |
| **Execution** | Broker ABC → PaperBroker (payout/latency/slippage/ATM-refund modelling) → DryRunBroker → QuotexBroker; OMS + ledger + SQLite journal |
| **Quotex integration** | Stdlib RFC6455 WebSocket client → Engine.IO v3 / Socket.IO codec → website `api/signin` session + `authorization` / `orders/open` / `sellOption` / `candleHistory` dialect with auto-reconnect and venue reconciliation |
| **Backtest lab** | Event-driven binary-option simulator + 10-scenario gauntlet (bull/bear trend, range chop, low-vol grind, high-vol expansion, flash crash, gap open, news spike, liquidity vacuum, regime whipsaw), survival scoring, walk-forward optimizer |
| **HUDs** | Desktop Tkinter terminal (boot animation, canvas candlesticks, gauges, meters, blotter, 7 panels) **and** browser terminal (glitch typography, scanlines, grid bloom, canvas chart, SSE live feed, fire control) |

## Architecture

```
cybertrade/
├── data/        models · candle history & MTF resample · synthetic regimes · feeds
├── indicators/  core · trend · momentum · volatility · volume · patterns
├── regime/      RegimeDetector
├── strategies/  trend · meanrev · breakout · momentum · volatility · pattern · ensemble
├── risk/        manager · limits · sizing
├── execution/   broker · paper · oms · ledger
├── network/     websocket (RFC6455) · socketio (EIO v3) · http_client
├── brokers/quotex/  client · api · adapter · protocol · models
├── bot/         engine · survivor · watchdog · health
├── backtest/    engine · scenarios · report · optimize
├── journal/     store (SQLite) · analytics
├── gui/         theme · widgets · chart · panels · app · boot
├── web/         server (HTTP+SSE) · static/ (cyberpunk dashboard)
└── cli.py       gui · web · run · backtest · optimize · journal · doctor
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the data-flow diagrams.

## Configuration

`data/cybertrade_config.json` (auto-created; passwords are stripped on save).
Key sections: `risk` (limits & sizing), `strategy` (universe, timeframe,
ensemble mode), `survivor` (defense matrix), `broker` (paper|dryrun|quotex),
`display` (theme: `neon_abyss` / `magenta_hell` / `ghost_cyan`), `backtest`.

## Phase 6 — per-voter calibration (and one honest negative result)

A gauntlet truth: the ensemble blob erased strategy identity — one liar in
the chorus paid no personal price. Phase 6 fixes the ledger:

- **`CalibrationTracker.observe_votes` / `p_win_for`** — every voter behind a
  blended signal gets its own reliability record; the gate reads
  `min(blob, voter-blend)`: voter evidence may only *lower* the estimate,
  never paper over a record the ensemble has discredited.
- **Kelly on evidence** — `size_stake` now sizes on calibrated P(win), not
  claimed confidence. This hunt also caught a real argument-order bug in
  `kelly_scaled` wiring (caps/`kelly_fraction` shifted one slot — Kelly
  stakes were clamped to ~1.0 whenever vol-targeting was off).
- **`min_edge` default 0.02 → 0.05** — with per-voter granularity the band
  finally engages (the old band was a 0.008-confidence sliver).

Two gauntlet arms against the Phase-5 control (30 runs each). The naive
per-voter *blend* diluted the loss-streak veto (grades A9 B20 **C1**); the
`min()` gate restores it: **A10 B20 C0**, mean return **−0.57%** (best of
all arms), tightest book (40.5 trades/run), 153.6 rejects/run. Pooled win
rate stays ~50% in *every* arm — confidence claims simply don't predict
outcomes well enough to find a 54%+ subset. What the quant layer buys is
discipline and tail control, not clairvoyance. `tests/test_phase6.py`
(11 tests); suite at **290 green**.

## Phase 5 — the gate under fire (gauntlet parity)

The backtester now runs the **same calibrated edge gate** as the live engine,
so the GAUNTLET measures what the quant layer buys instead of assuming it:
fresh `CalibrationTracker` per run, `TickFlow` fed bar-by-bar (orderflow
strategies vote in backtest too), negative-EV veto + `edge_gate` band,
settlements teach the calibrator. Plus **adaptive expiry** —
`RiskConfig.expiry_select="adaptive"` picks the candidate horizon with the
best modeled edge (`quant/expiry.py`, drift from conviction, vol from
recent closes); default stays `"signal"`.

Controlled gauntlet result (30 scenario-seed runs per arm, same code):
the **negative-EV veto is the workhorse** (~138 rejections/run, mean trades
47.6 vs 47.5, survival 0.691 vs 0.689). The `scale` band barely engages:
cold-start `p_for` moves in coarse steps, so the `edge ∈ [0, min_edge)`
band is a thin confidence sliver. Taken trades still clear the 54.05%
breakeven hurdle only ~half the time — calibration granularity (per
*strategy*, not per ensemble) is the next frontier. `tests/test_phase5.py`
(12 tests); suite at **279 green**.

## Phase 4 — quant edge layer

Phase 3 left the honest wound open: mean win rate 49.3% against a **54.05%
breakeven** at the standard 85% payout. Phase 4 attacks exactly that.

- **Payout math (`cybertrade/quant/binary.py`)** — `breakeven_winrate`,
  `edge_of`, fractional-Kelly `kelly_stake`, drifted-GBM `probability_itm`,
  expiry edge profiles. `python3 -m cybertrade edge --payout 0.85 --winrate
  0.55` prints your real hurdle (and the confidence-vs-evidence lecture).
- **Calibration (`cybertrade/quant/calibration.py`)** — per-strategy
  reliability buckets blended with a Beta(2,2) prior and a raw-confidence
  shrink. `CalibrationTracker.p_for` turns claimed confidence into the
  P(win) your own ledger has earned. Confidence is a claim; calibrated
  P(win) is evidence.
- **The edge gate (`RiskConfig.edge_gate`: `off|scale|hard`, `min_edge`)** —
  every entry must clear `P(win)·(1+payout) − 1 > 0` *after* calibration.
  Negative EV is vetoed unconditionally (even with the gate `off`).
  Thin-but-positive edge is stake-scaled (`scale`) or vetoed (`hard`).
- **Order flow (`indicators/orderflow.py`, `strategies/orderflow.py`)** —
  tick imbalance, cumulative delta, volume profile/POC; three strategies
  (`imbalance_momentum`, `absorption_fade`, `poc_reversion`) that fall
  silent without flow data instead of guessing.
- **Tape forensics (`data/tape.py`)** — signal-grade bus events recorded to
  bounded daily JSONL (`data/tapes/`) for replay and post-mortems.

36 new tests (`tests/test_phase4.py`); suite at **267 green**.

## Phase 3 — defect burn-down + venue depth

- **Crash-echo regime guard** — flash-crash bounces can no longer masquerade
  as fresh bull trends; shock/gap detection now requires materiality (size),
  not just N-sigma (which misfires on compressed-vol tapes).
- **Ensemble consensus gates** — `unanimous` precedence fix (all-PUT never
  fires CALL), `min_dominance=0.60`, corroboration discount on solo votes.
- **Quotex depth** — live instrument catalog sync (`parse_instruments` +
  `AssetCatalog`), history warm-start (`brokers/quotex/sync.py`), tolerant
  tick/balance parsing, engine boot wiring for live quote streams.
- Measured on the 30-run gauntlet: mean WR 48.5% → 49.3%, worst-case
  flash_crash DD 7.2% → 4.0%, C-grades 1 → 0, grade-A rows 6 → 11.

## Phase 2 — intelligence layer

| capability | module | surface |
| --- | --- | --- |
| Divergence detection (regular/hidden bull/bear) | `indicators/divergence.py` | 4 new strategies: `rsi_divergence`, `macd_hidden_divergence`, `cci_divergence_fade`, `mtf_confluence` |
| Correlation clusters | `risk/correlation.py` | per-cluster exposure caps via `risk.authorize(cluster=…)` |
| Monte Carlo risk lab | `risk/montecarlo.py` | `python -m cybertrade montecarlo`, `GET /api/montecarlo`, GUI "MC LAB" |
| Economic calendar / news blackouts | `bot/calendar.py` | `python -m cybertrade calendar`, `GET /api/calendar`, engine entry veto |
| Alert center (webhook/bell) | `bot/alerts.py` | halt/loss/news/equity rules → console + optional webhook |
| User strategy plugins | `strategies/plugins.py` | `~/.cybertrade/plugins/*.py` — see `docs/STRATEGY_AUTHORING.md` |
| Live scenario hot-swap | `data/synthetic.py`, feeds | `POST /api/command {"cmd":"scenario"}`, GUI scenario dropdown |
| Equity + MC HUD charts | web `charts.js` | equity trace, MC P05/P50/P95 fan, news grid, alert ticker |

MC semantics: fixed-fraction sizing makes literal zero unreachable, so the
ruin line is **50% of starting balance**. Verdicts: `SURVIVABLE` (ruin ≤ 1% and
P5 ≥ 80% of start), `CAUTION`, `DANGEROUS`, `RUIN LIKELY`. Calendar release
times are *estimated* (NFP is exact-ish first-Friday 12:30 UTC; FOMC/CPI are
approximate) unless you supply `~/.cybertrade/calendar.json` — the UI marks
estimates with `*`.

## Testing

163 unit/integration tests cover indicators (known-value checks), pattern
geometry, regime classification per scenario, every strategy family on every
regime, risk limits (kill switch, cooldowns, rate caps, drawdown governor),
paper fills/settlements incl. ATM refunds, OMS lifecycle, backtest
determinism + flash-crash capital preservation, journal persistence, the
WebSocket/Engine.IO/Socket.IO codecs (wire-level golden strings against the
confirmed Quotex shapes), and the web terminal's HTTP/SSE surface.

```bash
python -m unittest discover -s tests        # 163 tests
python -m cybertrade doctor                 # 10 environment checks
```

## Honest engineering notes

1. **100k lines?** This codebase chooses *working, tested logic* over line
   count — every module earns its keep. Padding to a vanity metric would be
   the opposite of "bug free".
2. **"Survive every market condition"** is enforced *defensively*: the
   gauntlet runs the full matrix and the grading metric is survival score
   (capital preservation first, PnL second). The flash-crash scenario is the
   regression test: **balance must remain > 0 with DD < 35%**.
3. **The Quotex dialect can break** whenever the site changes (it has before —
   `buyOption` → `orders/open`). Parsers accept payload drift; the protocol
   notes document every known variant.

## License

MIT. You trade at your own risk.
