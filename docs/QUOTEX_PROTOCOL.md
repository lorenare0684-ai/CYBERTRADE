# Quotex Wire Protocol Notes (UNOFFICIAL)

> **Warning** — this document describes a *community reverse-engineered*
> protocol.  Quotex/qxbroker.com has **no official public API**.  Everything
> below was reconstructed from public sources (open-source clients such as
> `quotexapi`/`pyquotex`, Stack Overflow traces, and observed traffic).
> Field names and event names drift between site deployments and may stop
> working without notice.  Automated order flow may violate the broker's
> Terms of Service.  Use the **practice account** and see `DISCLAIMER.md`.

## 1. Transport stack

```
TLS  →  WebSocket (RFC 6455)  →  Engine.IO v3  →  Socket.IO v2  →  Quotex events
wss://ws2.qxbroker.com/socket.io/?EIO=3&transport=websocket
```

CYBERTRADE implements every layer from the Python standard library:

| Layer       | Module                                  |
|-------------|-----------------------------------------|
| WebSocket   | `cybertrade/network/websocket.py`       |
| Engine.IO v3 + Socket.IO | `cybertrade/network/socketio.py` |
| HTTP login  | `cybertrade/network/http_client.py`     |
| Quotex dialect | `cybertrade/brokers/quotex/protocol.py` |

## 2. Website session

```
POST https://qxbroker.com/api/signin
Content-Type: application/json
{"email": "...", "password": "...", "remember": 1}
```

Response sets `sessionid` (and Cloudflare) cookies and returns a session
token (`session` / `ssid`) used by the websocket authorization frame.  When
Cloudflare's browser challenge blocks headless login, copy the `ssid` from a
real browser session and inject it (`QuotexAPI.set_ssid` / CLI `--ssid`).

## 3. Engine.IO v3 handshake

The first websocket text frame from the server:

```
0{"sid":"<id>","upgrades":[],"pingInterval":25000,"pingTimeout":5000}
```

- Server sends `2` (ping) — client must reply `3` (pong) *with the same
  payload*.  (In EIO v3 the **server** pings; the client pongs.)
- Application messages are Engine.IO type `4` carrying a Socket.IO packet.

Socket.IO packet grammar (`<type>[namespace,][ack-id,]<json>`):

| Type | Meaning |
|------|---------|
| `0`  | connect (e.g. `40`) |
| `1`  | disconnect |
| `2`  | event (e.g. `42["name",{...}]`) |
| `3`  | ack |
| `4`  | error (e.g. `44{"message":"..."}`) |

## 4. Authentication frame (confirmed shape)

```json
42["authorization",{"session":"<ssid>","isDemo":1,"tournamentId":0}]
```

`isDemo: 1` selects the **PRACTICE** purse, `0` the REAL purse.

## 5. Trading frames

### Place a binary option (modern `orders/open`, confirmed via Stack Overflow trace)

```json
42["orders/open",{
  "asset":"AUDCAD_otc",
  "amount":6,
  "time":1637893200,
  "action":"put",
  "isDemo":1,
  "requestId":1637892541,
  "optionType":1
}]
```

- `action`: `"call"` | `"put"`
- `time`: unix **expiry timestamp** (seconds)
- `optionType`: 1 = digital, 2 = binary (venue variants)

### Legacy placement (older clients / `stable_api.buy`)

```json
42["buyOption",{"asset":"AUDCAD_otc","amount":6,"action":"put","duration":60,"isDemo":1,"requestId":"..."}]
```

### Early sale / close

```json
42["sellOption",{"id":"<orderId>"}>
```

### Balance & purse switching

```json
42["balance",{}]
42["changeBalance",{"accountType":"PRACTICE"}]   // or "REAL"
```

### Market data

```json
42["candleHistory",{"asset":"EURUSD_otc","timeframe":60,"count":200,"requestId":"..."}]
42["subscribeCandle",{"asset":"EURUSD_otc","timeframe":60}]
42["unsubscribeCandle",{"asset":"EURUSD_otc","timeframe":60}]
42["portfolio",{}]
```

## 6. Server → client events (parsed defensively)

| Event | Payload (community shapes) |
|-------|----------------------------|
| `candleHistory` / `candles` | `{"asset":..., "candles":[{t,o,h,l,c}\| [t,o,c,h,l] ...]}` |
| `tick` / `quote` | `{"asset","price","ts"}` or `{"s","p","t"}` |
| `balance` / `balanceUpdate` | `{"balance":..., "accountType":"PRACTICE"}` |
| `order` / `orderResult` | `{"id","requestId","status","openPrice","closePrice","profit"}` |
| `profit` | settlement PnL update |
| `notification` / `error` | human-readable strings/dicts |

All parsers live in `cybertrade/brokers/quotex/protocol.py` and accept both
dict and list envelope variants (`QXCandle.from_payload` etc.).

## 6b. Instrument catalog + history sync (implemented)

- `42["instrument",{}]` requests the instrument listing; the reply event
  (`instrument` / `instruments` / `assets`) is absorbed by
  `protocol.parse_instruments`, which tolerates four community shapes:
  `{name: {…}}` mappings, `{asset|name|symbol, …}` rows, `[name, {…}]` pairs,
  and nested lists wrapping any of them.  Payouts accept `payout`/`profit`
  (fraction or percent), open state `open`/`isOpen`, and asset class via
  `type`/`kind` into `AssetCatalog` (`brokers/quotex/catalog.py`).
- `payout_for()` consults the live catalog first, then
  `cybertrade.constants.ASSET_CATALOG` (offline/paper fallback).
- History warm-start: `candleHistory` fills `QuotexAPI`'s cache;
  `brokers/quotex/sync.py` pushes it into `CandleSeries`/`HistoryBuffer`
  books (dupe-safe, failure-tolerant) so live strategies get indicator warmup
  exactly like paper mode.
- Ticks tolerate dict rows, bare price scalars, and `[asset, price, ts?]`
  rows; balances tolerate scalar pushes.

## 7. Known gaps / drift risks

1. **Cloudflare** — headless `api/signin` may return an HTML challenge; fall
   back to browser-derived `ssid`.
2. **Event renames** — `buyOption` → `orders/open` already happened once.
   `cybertrade/brokers/quotex/constants.py` keeps aliases; override there.
3. **Candle envelope** — at least three historical shapes exist; the parser
   absorbs `{"candles":[...]}`, `{"data":[...]}` and bare lists.
4. **Settlement truth** — always reconcile final PnL from venue `orderResult`
   events (`QuotexBroker._reconcile`), not from local price guesses.

## 8. Ethical / ToS posture

- Default mode of CYBERTRADE is **paper simulation**; no network traffic.
- `--live` requires flipping `risk.allow_live` in config **and** an interactive
  confirmation.  Even then, prefer `PRACTICE`.
- You are responsible for compliance with Quotex's Terms of Service and with
  any law governing automated trading in your jurisdiction.
