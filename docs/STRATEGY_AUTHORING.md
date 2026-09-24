# STRATEGY AUTHORING // CYBERTRADE // NEON PROTOCOL

Two ways to add edge to the terminal:

1. **Built-in strategies** — subclass `cybertrade.strategies.base.Strategy` and
   register the class in `cybertrade/strategies/__init__.py`.
2. **User plugins** — drop a `.py` file into `~/.cybertrade/plugins/` (or any
   directory you point `AppConfig.plugins_dir` at). No core file edits, no
   restart of the library API, and a broken plugin can **never** take the bot
   down — failures are quarantined and logged.

---

## 1. The contract

```python
from cybertrade.strategies.base import Strategy, StrategyContext
from cybertrade.data.models import Signal
from cybertrade.constants import Side, MarketRegime

class MyEdge(Strategy):
    name = "my_edge"            # unique registry key
    label = "My Edge"           # human label for the UI
    family = "custom"           # taxonomy used by the survivor/ensemble
    min_bars = 40               # warmup guard — generate() refuses shorter books
    lookback = 120              # how much history the engine feeds you
    preferred_regimes = (MarketRegime.RANGE,)   # ensemble weighting hint

    def decide(self, ctx: StrategyContext):
        """Return a Signal (edge) or None (no trade). Called once per candle."""
        closes = ctx.closes
        if closes[-1] < closes[-5]:
            return self._signal(
                ctx,
                side=Side.PUT,
                confidence=0.62,
                reason="5-bar slip fade",
            )
        return None
```

Hard rules the base class already enforces for you:

- `generate()` wraps `decide()` — exceptions are logged and become `None`.
  A crashing strategy cannot hurt the account.
- `enabled = False` silences a strategy instantly.
- Non-trade sides / zero confidence are dropped before the risk stack.
- `_signal(ctx, side, confidence, reason, **meta)` fills asset, timeframe,
  expiry, price, and timestamp from the context. Use it. Never construct
  `Signal` by hand with stale timestamps.

## 2. StrategyContext — what you may look at

| member | shape | notes |
| --- | --- | --- |
| `candles` | `list[Candle]` | oldest → newest, last may be live |
| `closes` `highs` `lows` `opens` `volumes` | `list[float]` | convenience views |
| `regime` | `RegimeReading` | `.regime`, `.stress`, `.confidence`, `.volatility_state` |
| `timeframe_seconds` `expiry_seconds` | `int` | book TF and default contract length |
| `payout` | `float` | venue payout — breakeven WR is `1/(1+payout)` |
| `ts` | `float` | decision timestamp (no future data, ever) |
| `extra` | `dict` | engine-side extras (HTF books, spreads…) when available |

**No lookahead.** Everything you see is closed candles up to `ts`. Indicators
that confirm with `right` bars of lag (pivots, divergences) are deliberately
lagged — that is your survival edge against curve-fit ghosts.

## 3. Registering a built-in

Add to `STRATEGY_REGISTRY` in `cybertrade/strategies/__init__.py`:

```python
"my_edge": MyEdge,
```

It joins `build_all_weather()` automatically and shows up in
`python -m cybertrade strategies` and the web/GUI strategy panels.

## 4. Drop-in plugins

```
~/.cybertrade/plugins/my_edge.py
```

Expose either a class or a factory:

```python
STRATEGY_CLASS = MyEdge           # instantiated once at load
# — or —
def register():
    return [MyEdge(), OtherEdge()]   # full control over instances
```

Load status is visible at boot (`plugins loaded=N failed=M`) and every
strategy name inside the ensemble's describe() payload. Rules:

- unique `name` — duplicates are refused (first one wins)
- import errors, `register()` crashes, and non-Strategy returns are
  quarantined per-file; siblings still load
- plugins attach to the live ensemble at engine boot (`ensemble.attach`)

## 5. Testing your edge

Minimum bar: a deterministic constructed case, like
`tests/test_phase2.py::TestDivergenceStrategies`:

```python
ctx = StrategyContext(asset="EURUSD", candles=candles, timeframe_seconds=60,
                      ts=candles[-1].close_ts + 1)
sig = MyEdge().generate(ctx)
assert sig is None or sig.side in (Side.CALL, Side.PUT)
```

Then check the payout math — the only opinion that matters before you risk
anything:

```bash
python3 -m cybertrade edge --payout 0.85 --confidence <your claim>
python3 -m cybertrade montecarlo --payout 0.85 --wins 55 --losses 45
```

A 0.85 payout needs **more than 52.6% wins** just to break even, so a strategy
that is merely "usually right" still loses money. There is no backtest lab in
this build: a strategy earns its keep on the venue's own fills, recorded in
the SQLite journal (`cybertrade journal`), or it does not. Honesty is the
house style — see `DISCLAIMER.md`.

## 6. Divergence toolbox (Phase-2)

`cybertrade.indicators.divergence` gives you the swing machinery:

```python
from cybertrade.indicators.divergence import find_pivots, last_divergence

pivots = find_pivots(closes, 3, 3, is_high=False)      # confirmed swing lows
div = last_divergence(closes, rsi(closes, 14), within=4)  # most recent event
if div and div.is_regular and div.is_bull:
    ...  # regular bullish reversal
```

Kinds: `regular_bull`, `regular_bear`, `hidden_bull`, `hidden_bear`
(continuations are `hidden_*`). `div.strength` is 0..1; `confirm_index` is the
first bar where the event *could* have been known.

---
*Automated trading involves real-money risk and may violate a broker's ToS.
Paper first. The market always gets a vote.*
