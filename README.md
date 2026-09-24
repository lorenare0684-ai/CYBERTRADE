# CYBERTRADE // NEON PROTOCOL

> **All-weather algorithmic trading terminal for Quotex-style binary options**
> with a full cyberpunk HUD — desktop *and* browser editions — built on the
> Python standard library alone. Zero third-party runtime dependencies.
>
> ### ⚠ THIS BUILD IS LIVE ONLY
>
> There is **no paper mode, no dry-run mode, and no synthetic market**. Every
> order this program places is a **real order at Quotex**. The simulator, the
> backtest lab, the crisis drills and the paper supervisor were removed
> outright — they are not merely switched off. The sections below are the
> phase-by-phase history of the project and describe features that no longer
> exist; the tables at the top are current.

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
- **Every boot is live.** Each trading command asks you to type
  `I UNDERSTAND`, and asks which purse to trade (PRACTICE or REAL) — nothing
  is defaulted for you. `--yes` skips the typing for scripted operators;
  `--demo` / `--real` skip the purse question.
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

# headless LIVE trading loop (durable risk governor + venue reconciliation)
python -m cybertrade run --demo            # --real trades REAL MONEY

# venue session: pair Chrome (human solves the CAPTCHA), then check/warm it
python -m cybertrade quotex login
python -m cybertrade quotex status
python -m cybertrade quotex warm --bars 250

# catalogs, journal analytics, payout math, risk lab, self-test
python -m cybertrade strategies
python -m cybertrade journal
python -m cybertrade edge --payout 0.85 --winrate 0.55
python -m cybertrade montecarlo --pnl "8.5,-10,8.5,-10" --json
python -m cybertrade calibrate
python -m cybertrade calendar
python -m cybertrade doctor

# run the test suite (per module keeps each run bounded)
python -m unittest tests.test_bot tests.test_phase32
```

## The stack

| Layer | What it is |
|---|---|
| **Indicators (54 registered)** | SMA/EMA/WMA/DEMA/TEMA/HMA/ALMA/KAMA/VWMA, RSI, Stoch-RSI, MACD, PPO, ROC, TSI, ATR, NATR, Bollinger (+bandwidth, %B), Keltner, Donchian, SuperTrend, PSAR, Ichimoku, ADX/DI, Aroon, Vortex, CCI, MFI, Williams %R, Ultimate/Accel-Decel oscillators, Elder Ray, squeeze, GARCH(1,1), EWMA/HV/Parkinson vol, Ulcer, OBV, VWAP, CMF, A/D, Force Index, EOM, Klinger, pivots … |
| **Patterns (12)** | Engulfing, hammer/hanger, morning/evening star, three soldiers/crows, pin bars, marubozu, inside/outside bars, tweezers, harami, doji + blended pattern score |
| **Strategies (40 registered)** | Trend, mean-reversion, breakout, momentum, volatility, price-action, defensive veto, and the **ALL-WEATHER ENSEMBLE** (regime-weighted voting, adaptive EWMA performance weights, strategy quarantine, conflict vetoes) |
| **Regime engine** | Trend/range/vol/crisis/gap classifier fusing ADX, regression slope, vol percentile, GARCH, gap scans → `RegimeReading` + stress score |
| **SURVIVOR playbook** | Condition → response matrix on a 5-rung posture ladder (ATTACK → NORMAL → GUARD → DEFENSE → LOCKDOWN): stake scaling, confidence floors, expiry caps, forbidden strategy families, news blackouts, weekend/friday locks, spread/liquidity vetoes |
| **Risk fortress** | Stake bands, fractional-Kelly + vol-target sizing, drawdown governor (daily lock + total kill), loss-streak cooldowns, per-asset / correlation-cluster caps, rate limits, payout floor, strategy win-rate floors |
| **Execution** | Broker ABC → QuotexBroker (real `api.buy`, venue reconciliation, an `allow_orders=False` data-only rail); OMS + ledger + SQLite journal |
| **Recovery (Phase 32)** | Single-writer, fsynced risk/ledger/order-registry checkpoints; intent/commit transaction markers; restart-preserved loss limits and kill; PID/run-bound completed-cycle heartbeats; SIGTERM follows the Ctrl+C cleanup path (the paper-only process supervisor was removed) |
| **Quotex integration** | Stdlib RFC6455 WebSocket client → Engine.IO v3 / Socket.IO codec → website `api/signin` session + `authorization` / `orders/open` / `sellOption` / `candleHistory` dialect with auto-reconnect and venue reconciliation; **Chrome pairing** (`quotex login`: human solves CAPTCHA, we read `sessionid` via localhost DevTools); live modes wire **venue candles only** — synthetic feeds structurally refused; **ghost wire** (Phase-30): human-paced frames, jittered reconnects, subscription replay, gap-only backfill, portfolio reconcile |
| **HUDs** | Desktop Tkinter terminal (boot animation, canvas candlesticks, gauges, meters, blotter, 7 panels) **and** browser terminal (glitch typography, scanlines, grid bloom, canvas chart, SSE live feed, fire control) — both **resolution-aware** (Phase-31: shared `gui/layout.py` breakpoints, plan-driven buttons/stat placement, explicit grid areas, DPR canvas fitting) |

## Architecture

```
cybertrade/
├── data/        models · candle history & MTF resample · live venue feed · feeds
├── indicators/  core · trend · momentum · volatility · volume · patterns
├── regime/      RegimeDetector
├── strategies/  trend · meanrev · breakout · momentum · volatility · pattern · ensemble
├── risk/        manager · limits · sizing
├── execution/   broker · oms · ledger
├── network/     websocket (RFC6455) · socketio (EIO v3) · http_client
├── brokers/quotex/  client · api · adapter · protocol · models
├── bot/         engine · survivor · watchdog · health
├── journal/     store (SQLite) · analytics
├── gui/         theme · widgets · chart · panels · app · boot
├── web/         server (HTTP+SSE) · static/ (cyberpunk dashboard)
├── continuity.py  runtime risk/ledger checkpoints · fail-closed recovery
├── shutdown.py    SIGTERM cleanup + SAFETY_HOLD_EXIT
└── cli.py       gui · web · run · quotex · calibrate · calendar · journal ·
                strategies · edge · montecarlo · doctor
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the data-flow diagrams.

## Configuration

`data/cybertrade_config.json` (auto-created; passwords are stripped on save).
Key sections: `risk` (limits & sizing), `strategy` (universe, timeframe,
ensemble mode), `survivor` (defense matrix), `broker` (`mode` is `quotex` and
nothing else; `demo_account` is your purse — `null` until you choose),
`display` (theme: `neon_abyss` / `magenta_hell` / `ghost_cyan`).

## Phase 32 — the watchdog (crash recovery without risk amnesia)

A process restart used to forget paper contracts and reset the risk-day
baseline. Runtime `gui`, `web`, and `run` now checkpoint the **governor and
paper book together**; the new `supervise` command guards unattended **paper**
execution without quietly re-arming live trading.

| Piece | Recovery behaviour |
|---|---|
| **Governor memory** | Same-day loss anchors, lifetime peak, order budgets, streaks, cooldown, kill/reason, and exposure survive restart. Only forward UTC day/elapsed-hour boundaries replenish their respective budgets; a backwards clock does not. |
| **Paper book + ledger** | Cash, escrow, original contract/fill/order IDs, attribution metadata, pending salvage, and a bounded equity trace restore together. Cash/exposure mismatches or corrupt rows block the entire recovery rather than dropping risk. |
| **Durable transaction boundary** | OMS mutations write an in-flight intent before execution and a cleared commit after bookkeeping. Interrupted operations hold for review—**no blind replay**. Private atomic JSON, fsync, and an OS lease prevent torn replacements and competing writers. |
| **Honest expiry recovery** | Contracts that expired offline stay held; no made-up strike refunds or wins from newly generated prices. The web **RESOLVE** control requires a known expiry price and confirmation, and works only for paper contracts. |
| **Process watchdog** | PID + unique run ID + advancing completed-cycle heartbeat; monotonic hang deadlines; concurrent bounded output drain; capped jittered backoff; default 5 restarts / 600s. Clean stops and safety holds do **not** respawn. |
| **Both HUDs** | Visible recovery status/hold reason. Web ARM/CALL/PUT controls disable during a recovery hold or latched kill; backend gates enforce it regardless of UI. |

```bash
python -m cybertrade supervise --max-restarts 5 --restart-window 600
```

Config adds `continuity_path` (default `data/continuity.json`) and
`heartbeat_path` (default `data/heartbeat.json`). Use distinct paths per
book/account, and stop the supervisor before opening the same book in a HUD.
Library/backtest engines stay ephemeral unless `durable=True` is requested.
Historical trade records remain in the SQLite journal; session blotter metrics
are not reconstructed wholesale. Synthetic price generators are not resumed.

**Live stays manual:** authenticated account identity must match; current cash
comes from the venue, never cached paper balances. Unresolved live exposure
requires reconciliation. This is not an exactly-once remote execution guarantee
or a substitute for the broker's records. Existing corrupt/in-flight files are
preserved, not silently overwritten by a fresh bankroll.

Full operating guide: **[`docs/RECOVERY.md`](docs/RECOVERY.md)**.
Suite: **593 green in 39 test modules**, including **73 new Phase-32 tests**
and real-process crash/pipe/cleanup probes. The actual supervisor CLI also
passed a SIGTERM shutdown + committed-checkpoint smoke check. Desktop Tk wiring
was checked headlessly; Tkinter is not installed in this sandbox.

## Phase 31 — the adaptive HUD (resolution-aware placement)

The desktop shell opened a hardcoded `1280x800` on every machine and the
browser twin had fixed columns — plus the OPEN POSITIONS panel had **no
placement rule at all**. Both HUDs are now resolution-aware off one shared
breakpoint table (`gui/layout.py`, pure stdlib — headless-testable;
mirrored exactly in CSS media queries):

| Class | Breakpoint | Placement behaviour |
|---|---|---|
| **compact** | `<1100px` wide **or** `<620px` tall (phones/tablets/short laptops) | single-column flow, page scrolls, stat cells 3/row, fire buttons wrap 3+2, meters 220px, gauges 90px, blotter 8 rows, **secondary labs hidden** (`risklab`, `equity`) — chart/fire/account/positions never leave |
| **medium** | `<1600px` | baseline dashboard: side rail 300–340px, 4-col stats, 5 fire buttons one row |
| **large** | `<2560px` | 2-column emphasis, stats 5/row, meters 420, gauges 150, blotter 14 |
| **wide** | `≥2560px` |3-column: chart+positions above blotter, side rail right; stats 6/row, blotter 18 |

**Desktop:** window opens at 92% of the real screen (centered, capped
2200×1400, minsize never exceeds the display), OS DPI awareness +
`tk scaling` follow the plan, a debounced `<Configure>` (250ms/40px
buckets) re-flows tab rows, stat cells, fire-control buttons, meters,
gauges and table heights on every breakpoint crossing; footer shows the
live `describe()` line. `display.fit_screen=False` restores the legacy
fixed shell.

**Web:** explicit `grid-template-areas` for **every** panel (chart, side,
**pos**, blotter, console, intel), wide/medium/compact/phone/short-height
media queries, ≥44px touch targets on phones, and `ResizeObserver` +
`devicePixelRatio` canvas fitting so the chart/MC/equity bitmaps track
their CSS boxes on any resize (JS mirrors the class into
`data-screen-class`).

Suite at **520 green** (`tests/test_phase31.py` 21 tests).

## Phase 30 — the ghost wire (organic pacing + advanced venue continuity)

"Most advanced + undetectable" resolves to one honest definition: **this
client's network manners are indistinguishable from the Chrome it was
paired from** — human timing, truthful headers, no faked capabilities.
No CAPTCHA bypass, no fingerprint spoofing, no proxy rotation: none of
that exists in this codebase, by standing rule (`QUOTEX_PROTOCOL` §10).

| Piece | What it does |
|---|---|
| `ghost.Pacekeeper` | Every venue frame rides class gates: **orders** get jittered think-time (uniform 0.7–1.4 × `order_think_ms`), a hard `order_min_gap_ms`, and a sliding `max_orders_per_min` window; history/poll and generic frames get smaller gaps. Bots are metronomic — we are deliberately sloppy, and timings are injectable for tests. |
| `ghost.parity_headers` | HTTP + WS handshakes carry the paired browser's truthful extras (UA, `Accept-Language`, no-cache). `Sec-WebSocket-Extensions` is **omitted, never faked** — advertising a capability we don't implement is itself a fingerprint. |
| `ghost.reconnect_delay` | Exponential backoff (2s ×1.8, 60s cap) ±20–25% jitter — reconnects never land on a fixed grid. |
| `ghost.is_session_fault` | "Session dead" venue errors (`invalid session`, `unauthorized`, `cloudflare`, `captcha`, …) → `session_stale` flag, CRITICAL log with a `quotex login` re-pair hint, one-shot `session_stale` event — never blind retries. |
| Subscription registry | `subscribeCandle` frames replay after **either** reconnect path (client-level hook or supervisor `api.connect()`); double-connect closes the old socket first — no ghost wires. |
| `sync.backfill_gaps` | The live feed fetches **only missing bars** (chart-like: notice the hole, fill the hole) instead of re-pulling full history on a grid; `reconnected` events trigger an immediate sweep. |
| `adapter.reconcile_venue` | Boot pulls the portfolio wire (`parse_portfolio`, schema-tolerant) and adopts venue-open contracts with plausible expiry metadata — crash restarts and multi-tab sessions reconcile; metadata-less orphans are logged for manual review, never guessed. |

Config: `BrokerConfig.ghost_pace=True`, `order_think_ms=140`,
`order_min_gap_ms=350`, `max_orders_per_min=10` (validated). Paper mode
untouched — no venue frames exist to pace.

Suite at **499 green** (`tests/test_phase30.py` 20 tests).

## Phase 29 — the airlock (Chrome pairing + LIVE candles only)

The CAPTCHA that defeated programmatic login gets the answer it deserves:
**a human**. `cybertrade quotex login` opens standard Chrome with a
persistent profile (`data/chrome-profile`, DevTools on
`127.0.0.1:9333`); you log in and solve the CAPTCHA yourself; pairing
polls `Storage.getCookies` over a stdlib websocket (with
`Network.getAllCookies` as fallback) until the `sessionid` cookie for
`qxbroker.com` appears, then persists it to `cfg.qx_session_path`
(0600) and verifies via `set_ssid + connect + account_snapshot`. No
Playwright, no headless browser, no CAPTCHA bypass.

**Both terminals pair.** The GUI and the web terminal both need venue
candles, which need a session — so instead of dead-ending a first-time
operator in a shell, each opens its own pairing screen: pick a purse (never
defaulted), set the profile/port/timeout, hit **CHROME LOGIN**, and the
terminal goes live the moment the cookie lands.

| | desktop GUI (`cybertrade gui`) | browser terminal (`cybertrade web`) |
|---|---|---|
| first run | pre-flight `SessionGate` window | pairing screen, no engine yet |
| mid-session | LINK pane's **CHROME LOGIN** | footer's **RE-PAIR VENUE** |
| re-pair effect | re-arms on the new cookie | re-seats the api in place — no restart, no rebuild |

The SSID is never written to disk by either path and is never sent to the
browser: `run_pairing` saves it 0600 and hands it to a callback that re-seats
the live api. Nothing trades, and nothing is simulated, until a real session
exists.

**Live modes refuse synthetic tape.** `broker.mode = quotex`
now wire `LiveQuotexFeed` (`is_synthetic=False`) from a strict
`_live_api` resolver: `cfg.broker.ssid` → `QX_SSID` env → paired
session file → username/password; nothing present → `ConfigError`
naming `quotex login`. `TradingEngine.__init__` re-checks the contract
— generator-backed feed + live mode = `ConfigError` — and warmup with
zero venue candles raises `FeedError` instead of booting on empty
books. The boot tick wiring skips the direct api handler when a live
feed already forwards venue ticks (no double-count), and
`quotex status/warm` pick the session file up too. There is no offline mode
left — live is the only mode.

Suite at **479 green** (`tests/test_phase29.py` 16 tests; phase9/15
rewired to the no-silent-fallback contract).

## Phase 28 — the memory (operator decisions survive restart)

A crisis lockdown and deck toggles were process memory: restart the
engine and the lockdown silently lifts and disabled strategies trade
again. `statestore` now persists both atomically (`data/operator.json`,
gitignored, tmp+replace) and `boot()` restores them after the calibration
ledger: `lockdown_reason` re-engaged with a `lockdown_restore` ALERT,
disabled members stay off, health says what came back. Corrupt/missing
files load as empty — a bad file never stops boot, and an engine that
never hears an operator never writes one. Server lockdown/unlock/strategy
commands and `clear_kill`'s programmatic unlock all persist.

Suite at **463 green** (`tests/test_phase28.py` 7 tests).

## Phase 27 — the positions bay (open contracts, live marks, one-cut close)

The ACCOUNT CORE showed an exposure *count*; nothing showed the contracts.
**OPEN POSITIONS** lists every open order with strategy, strike, live feed
mark, **ITM / OTM / EVEN**, expiry countdown, and a per-row **CLOSE**
button — `{"cmd":"close","position":id}` runs P19's
`broker.close_position` for ONE contract (salvage haircut on losers,
modeled payout on winners) and immediately pumps the pending settlement so
ledger, journal, and HUD see the cut: the lifeboat's downstream path,
selectively. Unknown/missing ids rejected; `manual_close` ALERT published.

Suite at **456 green** (`tests/test_phase27.py` 5 tests).

## Phase 26 — the strategy deck (40 strategies, visible and switchable)

The arsenal already streamed into `state()["strategies"]` and
`/api/strategies` — nothing rendered it, and nothing could stop a strategy
from the console. The **STRATEGY DECK** panel lists all 40 members with
live WR · attempts · adaptive weight, **ward badges** (⛓ decay quarantine,
⌂ win-rate floor, OFF), a `N/40 live` counter, and per-member **ON/OFF
toggles** (`{"cmd":"strategy","name":…,"enabled":…}` → `member.enabled`,
health-noted as an operator action). Disabled members do not vote;
describe() now carries `winrate_quarantined` / `decay_quarantined` /
`decay_ward` per the P25 wards.

Suite at **451 green** (`tests/test_phase26.py` 6 tests).

## Phase 25 — the quarantine ward (decay flags now block trades)

P18 *flagged* fading strategies and alerted — and nothing isolated them.
Worse: live journal rows were keyed by the ensemble blob
(`ensemble_all_weather`), so `strategy_decay`'s per-strategy buckets never
fired outside rigged tests. Three fixes close the loop:

- **attribution** — orders are named after the strongest same-side voter
  (`_attributed_strategy`); the journal, decay watch, and calibration now
  learn which edge is actually fading
- **enforcement** — `_decay_alerted` (the set P18 already maintained) now
  BLOCKS: `_try_execute` vetoes quarantined names (`quarantine veto …`),
  the ensemble skips them as voters (`quarantined_votes`, synced every
  check), and recovery publishes `release`
- **visibility** — `state()["edge"].journal.quarantined` + HUD strip
  `⛓ QUARANTINE: names`

Suite at **445 green** (`tests/test_phase25.py` 9 tests).

## Phase 24 — fill realism (storms widen entries; the slip limit is real)

Fills were never the price you saw in a crisis. `risk.slippage` estimates
**expected adverse entry slip once per candidate trade** — stress ×
session liquidity × armed drill — and the value flows two ways:

- **the survivor vetoes** entries whose expected slip exceeds
  `RiskConfig.max_slippage_bps` (8.0) — a limit that was previously
  *declared but never enforced*, now a real governor: default config
  refuses to trade where fills would be worse than 8bps
- **the paper broker applies** the surviving value as an adverse strike
  offset (`max(order.meta.slippage_bps, broker default)`), so storm wins
  must clear the handicap; the fill records the actual slip

Heuristics (documented, venue-independent): calm thick tape stays at the
configured base (zero drift outside adverse conditions); stress scales to
~40bps at the crisis ceiling; thin session +6; drill floor 10; hard cap 75.

Suite at **436 green** (`tests/test_phase24.py` 8 tests).

## Phase 23 — the score bay (backtests & gauntlets from the cockpit)

`RUN BACKTEST` and `RUN GAUNTLET` buttons in the terminal launch **score
jobs** on a worker thread — one at a time, status in `state()["job"]`,
results rendered into a console-styled `#scorecard` panel (stress-matrix
table + posterior card, or the five-storm survival table). The live engine
is never touched: gauntlets boot a **temp engine** (own journal/calibration
under `/tmp`) and the hub **mutes bus forwarding** while it runs, so the
temp engine's ticks cannot flicker the live chart. Web defaults are sized
for interactivity (backtest `bars=200`, gauntlet `ticks=150` ≈ 11s);
tests use tiny overrides.

Suite at **428 green** (`tests/test_phase23.py` 6 tests).

## Phase 22 — the session clock (when you trade is a market condition)

The clock on the wall is part of the market. `risk.sessions` classifies
each broker symbol (fx / crypto / metal / index) against the UTC FX session
(asia · london · overlap · newyork · offhours) and returns a **stake
multiplier** — 03:00 Asia is not the London/NY overlap, and a weekend on
synthetic OTC is a deep discount (0.25×), never a ban (the survivor's
weekend lock still owns that gate). The engine multiplies it into
`regime_scale` (session × survivor family weight), the cockpit HUD shows
`SESSION <name> ×<min_scale>` from `state()["session"]`, and the journal
gains a **`by_session` P&L breakdown** so thin-hour edges are visible
instead of averaged away.

Profiles are conservative heuristics, documented as such — not claims
about any venue's fill quality.

Suite at **422 green** (`tests/test_phase22.py` 7 tests).

## Phase 21 — the cockpit (live drills + console controls)

The DRILL row in the web terminal crashes the **running** market: five
magenta buttons (FLASH CRASH / GAP / NEWS / VACUUM / WHIPSAW) + STOP + a
live `#drill` strip (name, steps, posture floor, salvaged). The shock now
enters at **one point** — `SyntheticFeed.price_filter`, wired to
`drill.shock_price` at boot — so tick, chart candles, broker marks, and the
regime detector all see the same storm (no more calm warmup candles behind
a crash). The console LOCKDOWN button launches the P19 lifeboat through the
real survivor chain (`posture_for` honours manual lockdown).

- `POST /api/command {"cmd":"drill","scenario":…|stop}` + `"drill"` in
  `/api/state` — same `terminal.command` surface the HTTP API and tests use
- `StressDrill.disarm()` aborts mid-storm (market returns to its own path)
- the single-shock rule is regression-tested (`_on_tick` must not re-apply)

Suite at **415 green** (`tests/test_phase21.py` 7 tests).

## Phase 20 — crisis drills (chaos engineering for the defense stack)

Claims are cheap — **`cybertrade drill`** makes "survives every market
condition" measurable. Each drill drives a real scenario process (the same
`data.synthetic` generators the gauntlet backtest uses) through the live
engine's tick seam while the whole defense chain stays armed: regime
detector, survivor postures, risk guards, checkpoint, watchdog, and the
Phase-19 lifeboat. The book it shocks is the one you were already holding.

```
$ python3 -m cybertrade drill --ticks 250
  SURVIVAL GAUNTLET — live defense stack (PAPER)  seed=1337
  scenario             posture floor  book salv  kill      pnl  verdict
  flash_crash          DEFENSE           1    0    no   -10.00  SURVIVED (defended)
  gap_open             LOCKDOWN          1    0    no     8.50  SURVIVED (defended)
  news_spike           LOCKDOWN          1    0    no   -10.00  SURVIVED (defended)
  liquidity_vacuum     LOCKDOWN          1    1    no     8.50  SURVIVED (lifeboat)
  regime_whipsaw       LOCKDOWN          1    1    no     8.50  SURVIVED (lifeboat)
```

- five storms: `flash_crash gap_open news_spike liquidity_vacuum regime_whipsaw`
- scored from what **actually happened** — posture floor reached, contracts
  salvaged, kill switch tripped — never from hopes
- verdicts are honest: `SURVIVED (untested)` when the storm never engaged
  the defenses (reported, not hidden), `SURVIVED (defended)`, `SURVIVED
  (lifeboat)`, `KILLED`

Suite at **408 green** (`tests/test_phase20.py` 12 tests).

## Phase 19 — the lifeboat (crisis salvage of open positions)

Binaries cannot stop out — they ride to expiry. When Survivor screams
**LOCKDOWN** (flash crash, news spike, manual), open contracts used to just
take the storm head-on. The lifeboat now **sells every open contract back at
the salvage mark** on the next cycle: losing contracts recover
`salvage_rate` (default 25% of stake — the venue's sell-back quote),
winners bank the modeled payout, and each settlement rides `settle_due` like
an expiry (ledger, journal, decay watch all see it — no special paths).

- venue path: `api.sell_option(broker_id)` fires first (best-effort); the
  local salvage mark happens **even if the wire fails** — a dropped
  connection must not strand risk
- `Settlement.salvage` + the `returned = stake + pnl` unification keep every
  existing cash case byte-identical
- `RiskConfig.crisis_salvage=True` / `salvage_rate=0.25` (validated); the
  trigger is the computed posture (`survivor.posture_for(reading)`), so
  manual `engage_lockdown` launches the lifeboat too

Suite at **396 green** (`tests/test_phase19.py` 8 tests).

## Phase 18 — the decay watch (stale edges, flagged live)

`decay_check` averages everyone together — one strategy bleeding out hides
behind another's hot streak. `strategy_decay` watches **each record
separately** (recent-vs-prior win rate over sliding windows, −12pt
threshold, worst-first) and the engine turns it into a live watch: on every
settle the watch evaluates, fires **once per decay spell**
(`Topic.ALERT` + health feed), and **re-arms after recovery** so a second
spell alerts again.

| Surface | Shows |
|---|---|
| HUD decay strip | `⚠ DECAY: bollinger_fade, rsi_fade` or `128 journaled · edge stable` |
| `GET /api/state` → `edge.journal` | `{trades, decaying[], watched}` |
| `python3 -m cybertrade journal` | pooled `decay` + full breakdowns (already wired) |

Suite at **388 green** (`tests/test_phase18.py` 6 tests).

## Phase 17 — the report card (gauntlet × posterior)

A stress run is not a track record — pooled WR ≈ 50% everywhere in the
gauntlets, and any single row can be luck wearing a strategy's clothes.
Every `backtest` now ends by asking the P10 question of its own record:
`matrix_card` pools per-strategy evidence across **every** scenario×seed run
(`BacktestResult.strategy_evidence`) and reports each row's posterior mass
below breakeven — **LIAR past 50%** — plus the pooled honesty of the whole
harness (`BacktestReport.evidence` + `p_edge_negative` per run).

```
  ── report card · pooled 614W/601L · P(edge<0) 53.8% · breakeven 0.5405 @ payout 0.85 ──
  strategy                        W    L   hit%  P(edge<0)  flag
  bollinger_fade                 38   62  38.0%     99.7%  LIAR
  ...
```

The table sorts worst-first; `--out` JSON carries `strategy_evidence` per run
for offline pooling. Suite at **382 green** (`tests/test_phase17.py` 6 tests).

## Phase 16 — the wire heals (bounded reconnect supervision)

`BrokerConfig.reconnect_max` sat unused since Phase 1 while a dropped venue
socket meant silent starvation. The socket layer stays deliberately dumb
(no hidden retry threads); recovery is now explicit, bounded, and
observable. `network/supervisor.py::ReconnectSupervisor` polls link health
every engine cycle:

- **exponential backoff** (`base 1s → max 60s`) with a hard attempt cap
  (`reconnect_max`, default 12) — then it **gives up loudly** instead of
  retrying forever
- **heals only links that were up** — a venue that never connected is not a
  dropped wire (`standby`), and we do not hammer it
- **resubscribes on success** (`request_instruments`) and every transition
  lands in the health feed + `Topic.CONNECTION` bus events
  (`retry` / `reconnect` / `giveup`)

Suite at **376 green** (`tests/test_phase16.py` 5 tests).

## Phase 15 — the live wire (`mode=quotex`, safety stack intact)

`BrokerConfig.mode` declared `paper | quotex | dryrun` since Phase 1 and
`dryrun` became real in Phase 9 — but **`quotex` was still just a comment**.
The adapter was already a complete `Broker` (`submit` → `api.buy`, local
`settle_due` at expiry via streamed quotes, venue-event reconciliation via
`_reconcile`); the gap was the build path and its interlocks.

The mode trilogy is now whole, with defense in depth:

| `broker.mode` | without `--live` | with `--live` + `I UNDERSTAND` |
|---|---|---|
| `paper` | paper fills | **blocked** (`allow_live` default False + arm gate) |
| `dryrun` | venue quotes, paper fills, `DRY_RUN` rail | same (rail is structural) |
| `quotex` | **dry-run** (venue candles, paper fills) and says so | **live wire** — real `api.buy` at the venue |

`allow_orders` rides on `cfg.risk.allow_live`, and the I-UNDERSTAND gate
(`_confirm_live`) now runs **before** the engine is built so the flag is
already true at construction. `demo_account=True` (default) keeps the venue
on demo money even then. Suite at **371 green** (`tests/test_phase15.py` 9
tests: live submit reaches venue, rail still blocks, build wiring both
directions, gate matrix).

## Phase 14 — the venue actually connects (SSID session + real warm)

The dry-run harness built a `QuotexAPI` with **no session and no connect** —
live quotes and history could never arrive. `_build_venue(cfg)` now
authenticates from the honest sources — in-memory `--ssid`/`config.broker.ssid`
(session-only, stripped from every serialization like the password), the
`QX_SSID` env var, or `username`+`password` via `login()` — builds
`QuotexBroker(allow_orders=False)`, and any failure degrades to paper quotes.

Engine boot then pulls **real venue history into the books before trading**
(`warm_book` per asset, 120 bars, best-effort — a slow venue delays warmup,
it never blocks boot) so live strategies get the same indicator warmup paper
takes for granted. New `quotex` command surface:

| Command | Does |
|---|---|
| `python3 -m cybertrade quotex login` | **Phase-29 pairing** — opens Chrome (persistent profile), you log in + solve the CAPTCHA, session saved 0600 and verified |
| `python3 -m cybertrade quotex status` | connect, host/demo, balance, instrument count, sample payouts |
| `python3 -m cybertrade quotex warm --bars 250` | `candleHistory` round-trip per asset, reports candles added |

Suite at **362 green** (`tests/test_phase14.py` 8 tests).

## Phase 13 — wire the journal (the record survives)

The sqlite journal store (`data/journal.db`, `TradeJournal`) existed since
Phase 3 and **nothing ever wrote to it** — `journal` analytics read an empty
book while every settlement evaporated with the process. Now the engine feeds
it: every settle lands via `record_trade` (INSERT OR REPLACE, idempotent by
settlement id), sessions are bookended (`start_session` at boot with broker
mode + starting balance, `end_session` at shutdown with the final balance),
and a locked or corrupt journal degrades gracefully instead of bricking boot.

The MC lab keeps its sample across restarts: `hub.montecarlo` falls back to
the journal when the in-memory trade list is empty (`source: "journal"`) —
`simulate_from_records` already ate dicts with a `pnl` key, so the sqlite rows
plug in verbatim. `python3 -m cybertrade journal` now reports on real history
(streaks, decay check, per-strategy/asset/regime breakdowns, equity curve).

Suite at **354 green** (`tests/test_phase13.py` 7 tests).

## Phase 12 — bet against the liar (posterior-pessimistic sizing)

Kelly on a lucky thin record is how accounts die politely. Since Phase 6 the
gate used the blended posterior *mean* — which a 5–3 record flatters to ~0.63.
`CalibrationTracker.p_win_lower()` now feeds the **sizer** the Beta lower
quantile of the evidence (`RiskConfig.kelly_quantile=0.05`, sampled via
`beta_quantile()` — same epistemic stance as the P10 lab and P11 ledger):

| Record | mean (gate) | p05 (sizing) | Sizing |
|---|---|---|---|
| 5W/3L | ~0.63 → Kelly unlocks | ~0.35 | **fixed fraction** — thin records don't Kelly |
| 80W/20L | ~0.78 | ~0.72 | Kelly at the pessimist's price |
| cold start | shrunk opinion | same | unchanged (opinions can't be quantiled) |

The design split is deliberate: **the gate judges expected value (mean), the
size is what the record deserves while it might be lying (quantile)**. Raw
confidence gets no vote in sizing — opinions never raise the estimate, and
the `min(blob, voter-blend)` rule survives so discredited voters can only
lower it. `kelly_quantile=0.0` restores the mean path. Live engine and
backtester both size this way (gauntlets stay honest), `vol_target_enabled`
still preempts Kelly as before.

Bonus: the MC LAB panel gains a **posterior mode** toggle (hits
`/api/montecarlo?mode=posterior` on the existing display).

Suite at **347 green** (`tests/test_phase12.py` 12 tests).

## Phase 11 — the calibrator that survives its process (honesty ledger)

Learning that evaporates on restart is a demo, not a survivor. The calibration
ledger now **persists** (`data/calibration.json`, atomic write via
`CalibrationTracker.save()/load()`), reloads on `engine.boot()`, and saves on
`engine.shutdown()` — with one rule: **an empty session never clobbers real
data** (a crashed boot cannot erase what the market taught). Backtests keep
their own fresh trackers and never pollute the live ledger.

Every strategy record also gets the P10 question asked individually:
**`honesty(payout)`** samples `P(win) ~ Beta(wins+2, losses+2)` per strategy
and reports `p_edge_negative` — posterior mass below breakeven — flagging
**LIAR** past 50%. Rows sort worst-first.

| Surface | What it shows |
|---|---|
| `python3 -m cybertrade calibrate --payout 0.85` | full ledger table, W/L, hit%, P(edge<0), LIAR flags |
| `GET /api/state` → `edge.calibration.honesty[]` | per-strategy rows + `evidence` |
| HUD honesty strip | `42 strategies · 3 LIARS · worst strat 97%` |

Suite at **335 green** (`tests/test_phase11.py` 12 tests: roundtrip, no-clobber,
boot→shutdown persistence, liar flags, worst-first order, CLI).

## Phase 10 — is the record lying? (Beta-posterior Monte Carlo)

Bootstrapped Monte Carlo **cannot see parameter uncertainty** — resample a
lucky 20-trade record and every simulation looks like genius.
`risk/montecarlo.simulate_posterior(wins, losses, payout)` draws
`P(win) ~ Beta(W+2, L+2)` once per path (the calibrated posterior; prior
matches `ReliabilityBucket`) and plays `horizon` constant-stake trades at
the payout. The headline is **`p_edge_negative`** — the honest posterior
probability that the true edge sits below breakeven `1/(1+payout)` — plus a
dedicated **"RUIN LIKELY"** verdict when that mass exceeds 50%.

| Record | P(true edge < 0) @ payout 0.85 | Reading |
|---|---|---|
| 80W/20L | small | earned confidence |
| 12W/8L | large | *uncertainty* is the verdict |
| 20W/80L | ≈ 1 | the record is a liar |
| 0W/0L | ≈ 0.56 | the Beta(2,2) prior |

Surfaces: `python3 -m cybertrade montecarlo --wins 80 --losses 20 --payout
0.85`, `GET /api/montecarlo?mode=posterior` (HUD `source` shows
`posterior W/L`), `CalibrationTracker.evidence()` / `.evidence_for(strategy)`.

**Dry-run runbook (live data, zero risk)** — the practical companion:
1. `python3 -m cybertrade run --dry-run --live` with venue credentials
   configured (SSID session; this codebase never stores credentials).
2. Real ticks/instruments stream in via `.api`; `allow_orders=False` blocks
   every order at `DRY_RUN` before any wire call; fills are paper at
   clamped live quotes.
3. Read `GET /api/montecarlo?mode=posterior` and `montecarlo --wins/--losses`
   for the honest take on what the session taught the calibrator.

## Phase 9 — the dry-run harness (live quotes, paper fills, zero venue orders)

The last mile of the original ask: observe what the terminal *would* do at
a real venue — without ever placing a real order. `BrokerConfig.mode =
"dryrun"` (long declared, now real):

- **Adapter rail** — `QuotexBroker(api, allow_orders=False)` raises
  `DRY_RUN` at `submit` before anything else touches the wire.
- **`DryRunBroker`** (`execution/dryrun.py`) — paper fills and P&L locally;
  hurdle quotes come from the live venue facade (clamped 0.5–0.95, catalog
  fallback on quote failure). Exposes `.api` so engine boot streams live
  ticks/instruments into the normal pipeline while execution stays local.
- **CLI** — `python3 -m cybertrade run --dry-run` wires it (venue session
  if credentials configured, clean catalog fallback otherwise).

Pinned with stubs (no Quotex session here): rail blocks before any wire
call, default-allow still fails only at the connection check, quotes flow
from venue → gate → fill *at the venue quote*, venue `buy` counters stay
zero. `tests/test_phase9.py` (8 tests); suite at **313 green**.

## Phase 8 — the runtime payout gate (the venue quotes the hurdle)

Every previous gate computed EV against the **config default** payout (0.85)
while the venue actually pays 0.78–0.92 depending on asset and expiry —
the paper broker was already quoting honestly (`payout_for`: catalog +
expiry decay + jitter); the engine simply ignored it. Now the gate, Kelly
sizing, adaptive expiry, and `StrategyContext` all consume the **real
quote** (`broker.payout_for`), so the hurdle is per-trade: same P(win),
0.80 quote is a veto, 0.95 quote is a fill *at 0.95*. The HUD shows the
worst-case quote + per-asset payout map.

This also exposed a two-layer clash: Phase-7's `min_payout=0.85` floor
fought quote noise around 0.85 (payout_floor rejects, edge gate never
fired). Clean split: **`min_payout` = 0.80 scam floor**, edge gate = EV
math on the true quote. Gauntlet unchanged by construction (synthetic
scenarios pay flat) — pinned by `tests/test_phase8.py` (5 tests);
suite at **305 green**.

## Phase 7 — the WHEN matrix (regime-conditional calibration)

Phase 6's ceiling said unconditional claims don't predict outcomes. The
remaining information is *context*: a strategy can be lethal in
`bull_trend` and toxic in `range`, and its unconditional record averages
the two into a lie. `CalibrationTracker.p_regime` keeps (strategy × regime)
reliability tables, shrunk toward the strategy-level prior and silent below
4 samples; `p_win_for(..., regime=…)` routes every estimate through them
(`RiskConfig.regime_cal`, default on). This hunt also found that `oms.submit`
never carried votes — **live per-voter learning was silently dead**; fixed
(`votes` in order meta) and pinned by test.

A/B gauntlet (30 runs/arm): grade mix moves **A10→A14**, trades tighten
(40.5→39.3), survival identical… and pooled WR stays ~50%. Third
experiment, same ceiling — the ensemble's synthetic calls aren't sharper
than coin flips net of the spread. So the wrap also moves the *hurdle*
instead of the WR: **`min_payout` default 0.75 → 0.85** — at 90% payouts
breakeven is 52.6%, at 95% it is 51.3%; below 75% you need 57%+ just to
tread water. `tests/test_phase7.py` (10 tests); suite at **300 green**.

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
| Monte Carlo risk lab | `risk/montecarlo.py` | `python3 -m cybertrade montecarlo` (+ `--wins/--losses` posterior), `GET /api/montecarlo` (`?mode=posterior`), GUI "MC LAB" |
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
