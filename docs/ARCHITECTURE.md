# CYBERTRADE — Architecture

## Data flow (live terminal)

```
 [SyntheticFeed | QuotexAPI ticks]                      ┌──────────────┐
        │ Tick                                         │ WebTerminal  │
        ▼                                              │  (HTTP + SSE)│
  QuoteBook ──► TradingEngine.cycle() ──► snapshot ───►│  CyberpunkHUD│
        │           │                                  └──────────────┘
        │           ├─► RegimeDetector ─► RegimeReading (regime, stress)
        │           │
        │           ├─► StrategyContext ─► AllWeatherEnsemble.generate()
        │           │        │  votes from 33 strategies
        │           │        ▼
        │           │   Signal (side, confidence, expiry)
        │           │        │
        │           │        ▼
        │           │   Survivor.evaluate() ─► posture + stake_scale | VETO
        │           │        │
        │           │        ▼
        │           │   RiskManager.size_stake() → stake
        │           │        │
        │           │        ▼
        │           └──► OrderManager.submit() ─► RiskManager.authorize()
        │                                            │ (15 hard checks)
        │                                            ▼
        │                                    Broker.submit() → Fill → Position
        ▼
  Watchdog (heartbeats, price-jump/gap anomalies) ─► kill callback
```

## Settlement loop

```
OMS.pump(now) ─► Broker.settle_due() ─► Settlement(WON|LOST|REFUND)
      │                                       │
      ├─► Ledger.record_settlement()          ├─► RiskManager.on_close()
      ├─► TradeJournal.record_trade()         │      (cooldown / daily lock / kill)
      └─► Ensemble.record_result()            └─► Ensemble.reinforce_vote()
                                                    (adaptive weights)
```

## Survivor posture ladder

| Posture | Trigger examples | Response |
|---|---|---|
| ATTACK | strong trend + calm vol | stake ×1.15, all families |
| NORMAL | bull/bear trend, range | stake ×1.0, confidence ≥ 0.55 |
| GUARD | low-vol grind, high-vol, unknown | stake ×0.65, confidence ≥ 0.62, no pattern trades, expiry ≤ 600s |
| DEFENSE | crisis/gap, stress ≥ 0.65, friday cutoff | stake ×0.35, confidence ≥ 0.72, trend-family only, expiry ≤ 300s |
| LOCKDOWN | stress ≥ 0.85, news blackout, weekend (non-OTC), kill switch | **no trades** |

Every decision is logged and streamed to both HUDs via the EventBus.

## Network stack (Quotex)

```
ssl/socket ─► WebSocket (RFC 6455 frames, masked client)
          ─► Engine.IO v3 (0{sid} handshake, 2↔3 ping/pong, 4* messages)
          ─► Socket.IO (40 connect, 42["event",...])
          ─► Quotex dialect (authorization, orders/open, candleHistory, …)
```

Reconnect ladder: exponential backoff 2s → 60s, max 12 attempts, session
re-auth on every reconnect. All wire shapes documented in
`docs/QUOTEX_PROTOCOL.md`.

## Testing strategy

1. **Known-value math** — moving averages on constants, RSI of monotone
   series, Kelly bounds, drawdown of fixed curves.
2. **Wire-level goldens** — Engine.IO/Socket.IO encode/decode round-trips and
   literal protocol strings (`42["authorization",{...}]`).
3. **Survival regressions** — flash-crash scenario must never reach zero
   equity; every strategy must run crash-free on every regime.
4. **Clock discipline** — simulated/backdated streams re-anchor rate limits;
   cooldowns accept an explicit `now`.
