# Phase 32 — process recovery and supervised paper trading

A restart must not erase a loss limit, forget escrowed paper stakes, or
release a kill switch. This phase adds durable runtime checkpoints and a
bounded process supervisor. It does **not** promise profitable trading,
exactly-once execution at a remote venue, or recovery of missing market data.

## Start here

```bash
# Ordinary runtime commands checkpoint automatically.
python -m cybertrade web --host 0.0.0.0 --port 8899
python -m cybertrade gui
python -m cybertrade run

# Unattended PAPER execution only; no --live option exists here.
python -m cybertrade supervise --max-restarts 5 --restart-window 600

# The global config option goes BEFORE the subcommand.
python -m cybertrade --config profiles/paper.json supervise --startup-grace 120
```

`boot()` always starts **disarmed** (or with a latched kill). `run` and the
supervisor's `run` child explicitly request arming after recovery checks.
Web and desktop remain disarmed unless the operator arms them; `web --auto`
is still an explicit paper-arm request. A saved armed/live flag is never
restored. A saved `allow_live=true` cannot enable execution in a fresh web,
desktop, or non-`--live` headless process.

**Do not run `supervise` and the web terminal against the same checkpoint at
the same time.** Stop the supervisor before opening the same book for review.
The second writer is refused rather than allowed to split the account.

## Files and ownership

| File / config key | Purpose |
|---|---|
| `continuity_path` → `data/continuity.json` | Risk governor, cash anchors, paper book, active order metadata, pending settlement delivery, and an in-flight operation marker |
| `continuity.json.lock` | OS-held single-writer lease; PID is diagnostic only; the lock is released on process death |
| `heartbeat_path` → `data/heartbeat.json` | Process PID, unique run ID, state, wall timestamp, completed-cycle counter |
| `data/crashes/crash-*.txt` | Supervisor crash/hang/hold reports: bounded output tail, unique names, latest 20 retained |
| `data/operator.json` | Existing operator lockdown and strategy toggles; separate from financial recovery |
| `data/journal.db` | Existing durable trade journal; long-term trade history remains here |

Use **distinct paths for each paper book, dry-run profile, venue account, and
practice/real purse**. State is bound to broker mode/name and, for real venue
execution, the venue-provided user ID. Session/balance identity disagreement,
an unknown live identity, or an in-process account switch is a safety hold.
Session cookies and passwords are not recovery fields.

New state/report files are private (`0600` on POSIX). JSON writes use a unique
same-directory temporary file, flush + fsync, atomic replacement, and POSIX
parent-directory fsync. The lease inode is deliberately never deleted: an
old PID written in it is harmless, whereas unlinking a held lock can allow
split ownership. Canonical paths keep symlink aliases on the same book/lock.
Filesystem and OS durability guarantees still apply; this is not tamper-proof
storage or a substitute for verified backups.

An empty `continuity_path` explicitly disables financial persistence for an
ordinary runtime; an empty `heartbeat_path` disables its heartbeat file.
**Supervision refuses either setting.** Do not disable persistence to bypass
a recovery hold. Library/backtest engines are deliberately ephemeral unless
constructed with `TradingEngine(..., durable=True)`, so score-bay experiments
cannot overwrite the operating account.

## What survives

- **Governor:** full-precision daily starting/current/peak balance, daily and
  hourly order counts, loss/win streaks, cooldown, daily lock, kill/reason,
  asset/cluster exposure, recent results, and strategy win-rate counters.
- **Paper / dry-run book:** broker and ledger cash, starting balance/peak,
  original fill/order/position IDs, sides/strikes/payouts/expiries, attribution
  metadata, open escrow, and pending salvage settlements. Restore validates
  the entire book before applying it: a corrupt position is not silently
  dropped, and cash/exposure/linkage disagreement is not rounded away.
- **Chart trace:** the most recent 400 ledger equity points. The in-memory
  blotter and session performance aggregates restart; historical trades stay
  in the SQLite journal. Recovery does not reconstruct every historical
  performance metric from that journal.

Same-day restarts preserve loss anchors and consumed order budgets. A later
**UTC** day re-anchors daily limits on the next risk check, settlement, or
restore; elapsed hourly windows reset independently. Lifetime peak, kill,
loss streaks, cooldown, and open exposure remain. A backwards clock never
refills the hourly budget. Zero cash means a full drawdown, not a missing
balance that falls back to the starting bankroll.

## Crash boundary: commit or hold, never blind replay

All application order submissions, settlement pumps, manual closes, and
paper recovery resolutions go through the serialized OMS transaction path:

1. Write the current consistent book with `pending_operation` set.
2. Perform the mutation and its broker/ledger/risk bookkeeping.
3. Write the new consistent book with the marker cleared.

A completed checkpoint can be restored without recounting an open trade or
crediting an already-delivered settlement twice. A process dying between
steps 1 and 3 leaves **uncertainty**, not permission to repeat the operation.
The next boot holds trading for review. Unexpected mutation errors and
checkpoint write failures stop trading too. Applications extending the engine
must use the OMS, not mutate broker/risk/ledger internals around this boundary.

A missing file represents a new book. An existing corrupt/unsupported file,
future-dated checkpoint (more than 60 seconds ahead), mismatched account,
incomplete order linkage, or interrupted operation is **not** a new book.
The original file is preserved, even on shutdown; `CLEAR` cannot bypass a
financial recovery fault. Review the checkpoint, journal, and (where relevant)
the actual venue. Restore verified state or reconcile it offline; do not delete
or roll back the file merely to reset losses or make ARM available again.
The JSON checkpoint and SQLite journal are not one distributed transaction:
a journal row can exist from an operation whose final checkpoint was interrupted.
That is another reason an in-flight operation requires review.

## Contracts that expired while offline

There is no trustworthy expiry price just because the process has restarted.
A restored paper contract whose expiry was crossed offline remains in escrow
with a **RECOVERY HOLD**. It is not automatically refunded at its strike,
settled at a newly generated synthetic price, or salvaged at today's mark.
A restored future contract also waits for a fresh quote at/after expiry rather
than interpreting a missing quote as a refund.

For a held **paper** contract, the web OPEN POSITIONS row shows `REVIEW`,
`OFFLINE`, and a **RESOLVE** button. Enter the **known expiry price** and confirm
before the simulation records the outcome. Cancel if no expiry evidence is
available: leaving the contract held is safer than inventing P/L. This flows
through the same OMS bookkeeping, journal, and checkpoint; repeated resolution
of the same ID does not credit it again.

The local API equivalent is:

```text
POST /api/command
cmd: resolve_paper
position: the existing held position ID
expiry_price: a positive finite JSON number
```

This command refuses live brokers. It is not a way to alter the venue's
settlement. Both HUDs display recovery status; the browser disables ARM/CALL/PUT
while recovery or a kill blocks them. Backend checks remain authoritative.
Desktop users can inspect the recovery banner and use the web review flow
after stopping the desktop instance (the same-book writer lock still applies).

Synthetic market generators are not checkpointed or deterministically resumed.
Paper recovery preserves accounting, not continuity of an offline synthetic
price path. New paper quotes remain simulated quotes, not evidence of a missed
expiry or proof of real-market edge.

## Process supervisor contract

`cybertrade/watchdog.py` supervises the process. It is separate from
`cybertrade/bot/watchdog.py`, which monitors market/feed anomalies.

- The CLI constructs an unbuffered `python -m cybertrade run --supervised`
  child with the same config and optional scenario. Both parent and child
  reject non-paper execution; no confirmation or live flag is replayed.
- A unique run ID and the child PID bind its heartbeat. Only an **advancing
  completed-cycle counter** renews progress. A stale file, another PID, a prior
  session, changing wall timestamps, or a repeated counter cannot mask a hang.
- Deadlines use a monotonic clock. Startup grace defaults to **120 seconds**;
  after the first heartbeat, staleness defaults to **45 seconds** for M1, or
  `max(45, 2 × decision_interval + 15)` for longer timeframes. An explicit
  `--stale-seconds` must exceed the configured decision interval.
- Crashes/hangs spend a rolling restart budget: **5 restarts per 600 seconds**
  by default. Backoff starts at 2 seconds, doubles, adds ±20% jitter, and never
  exceeds 300 seconds. A healthy budget-length run resets the backoff exponent.
- Stdout/stderr are drained concurrently into a **64 KB tail**, not left in a
  pipe until exit. Reports scrub common credential patterns, but review reports
  before sharing them; redaction is not a guarantee against arbitrary secrets
  written by plugins or third-party code.
- Ctrl+C/SIGTERM cleans up the child. On POSIX the supervisor owns a child
  process group and terminates its descendants too. Termination escalates to a
  kill after a bounded wait; a child that cannot be reaped is not duplicated.

| Exit / event | Supervisor action |
|---|---|
| Child exit `0` | Stand down; operator stop is not a crash |
| Child exit `78`, or matching `kill` heartbeat | Safety hold; report and stop, **no respawn** |
| Crash / no progress | Report, terminate/reap if necessary, back off within budget |
| Budget exhausted | Exit `1`; operator intervention required |
| Invalid supervision settings | CLI exit `2` before spawning |

Live restarts remain manual. Durable live recovery preserves the governor,
reads current cash from the venue, never replays cached paper fills, and holds
when saved/current live exposure needs reconciliation. This phase does **not**
turn the unofficial Quotex bridge into an authoritative, exactly-once live
reconciliation service. It has not been validated against a real-money account.

## Verification

```bash
python -m unittest tests.test_phase32
python -m unittest discover -s tests
python -m cybertrade doctor
```

Phase 32 adds **73 tests**; the full suite is **593 passing tests in 39 test
modules**. Coverage includes actual abrupt process exits after an acknowledged
order and inside submission, OS-lock release after process death, pending
salvage, expiry holds/manual resolution, write-failure injection, UTC/backwards
clocks, account isolation, restart exhaustion, identity-bound liveness, a noisy
real child, and a hung real child. A separate CLI smoke check verified SIGTERM
cleanup and a committed paper checkpoint. JavaScript syntax and recovery-control
DOM behavior were checked. Tkinter is unavailable in the development sandbox;
desktop wiring was checked headlessly, not interactively rendered.
