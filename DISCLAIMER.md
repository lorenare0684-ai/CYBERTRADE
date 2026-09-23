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

## 3. Paper first, always

The default mode of every entry point (`gui`, `web`, `run`) is **paper
simulation**. Live order flow requires all of:

1. `risk.allow_live: true` in the config file,
2. `--live` on the CLI,
3. typing `I UNDERSTAND` at an interactive prompt,
4. a broker session you obtained yourself.

Even then: **prefer the broker's PRACTICE account** (`demo: true`).

## 4. Past performance ≠ future results

Backtests in this repo (including the "gauntlet" of stress scenarios) are
simulations with simplified friction. Real fills are worse. A strategy that
survives every scenario in `backtest/scenarios.py` can still fail on real
markets in ways those scenarios do not model.

## 5. Credentials

You must never commit credentials. The config serializer strips passwords on
save; Quotex session ids (`ssid`) are secrets — treat them like passwords and
pass them via terminal/environment, not files in Git.

## 6. No warranty

The authors and contributors are not liable for any damages or losses arising
from use of this software. If it wipes your account, that is on you. Trade
small. Trade demo. Touch grass.
