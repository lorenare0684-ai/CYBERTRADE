# DISCLAIMER — READ THIS BEFORE YOU LOSE MONEY

**CYBERTRADE // NEON PROTOCOL** is research and educational software. It is
provided "AS IS", without warranty of any kind.

## 1. No system survives every market condition

The phrase "all-weather" in this project refers to a **design goal** — degrade
gracefully across regimes via detection and defense — not a guarantee. Markets
gap, brokers halt, models break, and black swans eat stop losses for breakfast.
Binary/digital options can lose 100% of a stake in seconds. **You can lose all
money you put into trading, and more if you use leverage elsewhere.** Nothing
here is financial advice.

## 2. Unofficial Quotex integration — ToS risk

Quotex / qxbroker.com has **no official public API**. The wire protocol
implemented in `cybertrade/brokers/quotex/` is reconstructed from public
community sources (see `docs/QUOTEX_PROTOCOL.md`). It can break at any time.

**Automated order flow may violate the broker's Terms of Service** and can
result in account termination and loss of funds. You alone are responsible for
compliance with those terms and with the law of your jurisdiction (algorithmic
trading is regulated in many countries; binary options are banned for retail
traders in some jurisdictions, e.g. the EU/UK).

## 3. This build is live only

There is **no paper mode and no dry-run mode** in this build — they were
removed, not disabled. Every order placed by `gui`, `web` or `run` is a real
order at the venue. Every trading command therefore:

1. asks which purse to trade — `--demo` (PRACTICE balance) or `--real` (REAL
   MONEY); nothing is defaulted and a non-interactive shell with no choice
   fails instead of guessing with your money,
2. asks you to type `I UNDERSTAND` at an interactive prompt (`--yes` skips
   the typing for scripted operators who already know),
3. needs a venue session you obtained yourself (`cybertrade quotex login`).

Even live: **prefer the broker's PRACTICE balance** until a strategy has
proven itself on real fills.

## 4. Past performance ≠ future results

The backtest lab that used to live here was a simulation with simplified
friction, and it was removed with the rest of the simulator. Nothing in this
build claims a strategy works: only the venue's own fills and the SQLite
journal can tell you that, and they will.

## 5. Credentials

You must never commit credentials. The config serializer strips passwords on
save; Quotex session ids (`ssid`) are secrets — treat them like passwords and
pass them via terminal/environment, not files in Git.

## 6. No warranty

The authors and contributors are not liable for any damages or losses arising
from use of this software. If it wipes your account, that is on you. Trade
small. Trade demo. Touch grass.
