# Runtime recovery for the live build

A restart must not erase a loss limit, forget a venue order, or release a
kill switch. The runtime checkpoints below carry the risk governor, the
ledger and the **live order registry**. This build does **not** promise
profitable trading, exactly-once execution at a remote venue, or recovery of
missing market data.

> **Live only.** The paper supervisor, the paper book and the local paper
> resolutions were removed with paper mode. There is no `supervise` command;
> unattended restarts are the operator's job (a systemd unit, a shell loop),
> and every restart still passes the `I UNDERSTAND` gate.

## Start here

```bash
# Ordinary runtime commands checkpoint automatically.
python -m cybertrade web --host 0.0.0.0 --port 8899
python -m cybertrade gui
python -m cybertrade run --demo
```

`boot()` always starts **disarmed** (or with a latched kill). `run` and
`web --auto` explicitly request arming after recovery checks; web and desktop
otherwise stay disarmed until the operator presses ARM. A saved armed/live
flag is never restored, and a saved `allow_live=true` cannot enable execution
in a fresh process — the human gate runs every boot.

**Do not run two processes against the same checkpoint at
the same time.** Stop the first before opening the same checkpoint for review.
The second writer is refused rather than allowed to split the account.

## Files and ownership

| File / config key | Purpose |
|---|---|
| `continuity_path` → `data/continuity.json` | Risk governor, cash anchors, ledger, live order registry, and an in-flight operation marker |
| `continuity.json.lock` | OS-held single-writer lease; PID is diagnostic only; the lock is released on process death |
| `heartbeat_path` → `data/heartbeat.json` | Process PID, unique run ID, state, wall timestamp, completed-cycle counter |
| `data/operator.json` | Existing operator lockdown and strategy toggles; separate from financial recovery |
| `data/journal.db` | Existing durable trade journal; long-term trade history remains here |

Use **distinct paths for each venue account and practice/real purse**. State
is bound to broker mode/name and, for live execution, the venue-provided user
ID. Session/balance identity disagreement,
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
ordinary runtime; an empty `heartbeat_path` disables its heartbeat file. Do
not disable persistence to bypass a recovery hold. Only a runtime built with
`TradingEngine(..., durable=True)` writes checkpoints at all, so experiments
cannot overwrite the operating account.

## What survives

- **Governor:** full-precision daily starting/current/peak balance, daily and
  hourly order counts, loss/win streaks, cooldown, daily lock, kill/reason,
  asset/cluster exposure, recent results, and strategy win-rate counters.
- **Ledger:** cash, escrow, starting balance/peak and the bounded equity trace.
- **Live order registry:** the orders whose contract the venue still holds
  open, with their original IDs, sides, stakes, payouts, expiries and
  attribution metadata. Settled contracts leave the book on their own.
- Restore validates the whole checkpoint before applying any of it: a corrupt
  order row, a cash/exposure disagreement, or a mismatched account blocks the
  entire recovery rather than dropping risk.

Same-day restarts preserve loss anchors and consumed order budgets. A later
**UTC** day re-anchors daily limits on the next risk check, settlement, or
restore; elapsed hourly windows reset independently. Lifetime peak, kill,
loss streaks, cooldown, and open exposure remain. A backwards clock never
refills the hourly budget. Zero cash means a full drawdown, not a missing
balance that falls back to the starting bankroll.

## Crash boundary: commit or hold, never blind replay

All application order submissions, settlement pumps and manual closes go
through the serialized OMS transaction path:

1. Write the current consistent checkpoint with `pending_operation` set.
2. Perform the mutation and its broker/ledger/risk bookkeeping.
3. Write the new consistent checkpoint with the marker cleared.

A process dying between steps 1 and 3 leaves **uncertainty**, not permission
to repeat the operation: the next boot holds trading for review. Unexpected
mutation errors and checkpoint write failures stop trading too. Applications
extending the engine must use the OMS, not mutate broker/risk/ledger internals
around this boundary.

A missing file is a fresh runtime. An existing corrupt/unsupported file,
future-dated checkpoint (more than 60 seconds ahead), mismatched account,
or interrupted operation is **not**. The original file is preserved, even on
shutdown; `CLEAR` cannot bypass a financial recovery fault. Review the
checkpoint, the journal, and the venue's own open positions, then restore or
reconcile offline — do not delete the file merely to reset losses or make ARM
available again. The JSON checkpoint and the SQLite journal are not one
distributed transaction: a journal row can exist from an operation whose final
checkpoint was interrupted.

## Venue exposure is reconciled, never replayed

Cash always comes from the venue (`broker.account()`); no local cash is ever
credited into a venue balance, and no cached fill is ever replayed as an
order. When a restart finds saved exposure or the broker reports open
contracts, the runtime holds with
`live exposure requires venue reconciliation; no order replay performed`,
latches the kill switch, and waits for a human.

The operator's job is then to compare the checkpoint's order registry against
the venue's own open positions, resolve anything the venue no longer lists
through the venue, and only then restart. There is no local command that
settles a live contract: the old `resolve_paper` control was removed with
paper mode, because fabricating an expiry price at a real venue is worse than
sitting on your hands.

## Shutdown and exit codes

There is no process supervisor in this build — the one that existed was
paper-only by design (it never re-armed live trading on a crash) and it was
removed rather than left lying around. What survives is the contract every
runtime owes the operator:

- `cybertrade.shutdown.stop_on_sigterm()` makes SIGTERM follow the same
  cleanup path as Ctrl+C (main thread only).
- `cybertrade.shutdown.SAFETY_HOLD_EXIT` is **78**: a safety hold stopped the
  run, look before you restart it.
- A latched kill, a recovery hold, or a config/venue error all exit 78 rather
  than 0, so a wrapper script cannot mistake a stopped trader for a finished
  one.

Unattended restarts are the operator's own concern (a systemd unit, a shell
loop). Whatever wraps it must not auto-confirm the `I UNDERSTAND` gate.

## Verification

```bash
python -m unittest tests.test_phase32
python -m unittest discover -s tests
python -m cybertrade doctor
```

The live recovery suite (`tests/test_phase32.py`) covers write-failure
injection, UTC/backwards clocks, account isolation, second-writer exclusion,
identity-bound liveness, corrupted checkpoints, interrupted operations, the
SIGTERM/exit-code contract, and both HUDs' recovery surfaces. The paper-only
cases (abrupt process exits around paper orders, paper salvage, manual paper
resolution, restart-budget exhaustion, noisy/hung child reaping) were removed
with the supervisor they tested.

Tkinter is unavailable in the development sandbox; desktop wiring was checked
headlessly, not interactively rendered.
