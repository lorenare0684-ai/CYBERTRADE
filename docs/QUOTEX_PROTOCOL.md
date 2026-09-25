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

Login robustness notes (all implemented, all covered by tests):

- **Host fallback** — sign-in tries `HTTP_BASE` then `HTTP_BASE_ALT`;
  `connect()` tries the configured `ws_url` then `WS_URL` / `WS_URL_ALT`.
  A dead route reads as a dead route, not a dead account.
- **Response tolerance** — the token is harvested from `session` / `ssid` /
  `token` / `access_token` at top level or under `data` / `result` /
  `payload`, from a bare JSON string, or from the `sessionid` / `ssid` /
  `session` / `PHPSESSID` cookie. Chunked+gzipped bodies are decoded.
- **Error taxonomy** — HTTP 401 says bad password; a challenge page (HTML /
  `cf-challenge` markers) says to pair via `cybertrade quotex login`
  instead of blaming the password.
- **Pasted frames** — `set_ssid` unwraps a copied
  `42["authorization",{"session":"…",…}]` frame down to the token.
- **Explicit rejection** — a venue `invalid session` / `unauthorized` error
  during authorization raises `BrokerAuthError` immediately (re-pair hint
  included) instead of proceeding as a fake-live login; the first data
  event counts as implicit authorization so healthy connects return fast.
- **Pairing capture** — Chrome pairing keeps *every* venue cookie
  (`sessionid` first, CF clearance included) for the websocket handshake,
  accepts all venue front doors (`qxbroker.com` / `quotex.com` /
  `quotex.io`), and reads cookies in the Quotex tab's DevTools context.

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
  `cybertrade.constants.ASSET_CATALOG` (offline fallback while a
  session is still connecting).
- History warm-start: `candleHistory` fills `QuotexAPI`'s cache;
  `brokers/quotex/sync.py` pushes it into `CandleSeries`/`HistoryBuffer`
  books (dupe-safe, failure-tolerant) so live strategies get indicator warmup
  exactly like the live feed does.
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

- This build is **live only**: there is no paper mode, no dry-run mode and
  no synthetic market. Nothing runs offline by default — a venue session is
  required before anything trades.
- `--live` requires flipping `risk.allow_live` in config **and** an interactive
  confirmation.  Even then, prefer `PRACTICE`.
- You are responsible for compliance with Quotex's Terms of Service and with
  any law governing automated trading in your jurisdiction.

## 9. Browser pairing (CAPTCHA-safe, Phase-29)

Pairing is offered by **both** terminals, because both need a session before
either can boot. The flow itself is identical and toolkit-free
(`cybertrade/gui/pairing.py`), so a Tk window and an HTTP handler drive the
same code:

- **desktop** — `cybertrade/gui/session_gate.py` opens a pre-flight
  `SessionGate` when `cmd_gui` cannot build an engine for want of a session;
  the LINK pane's button re-pairs mid-session.
- **browser** — `cybertrade/web/pairing.py` owns the state machine
  (`idle → launching → waiting → ready | failed`), exposed over
  `POST /api/pair/start`, `GET /api/pair/status` and `POST /api/pair/cancel`.
  `cmd_web` boots with **no engine** and serves a pairing screen; the engine
  attaches when a cookie lands, and a later re-pair calls
  `cli._reseat_session()` — `set_ssid` + `connect` on the live api — so
  nothing restarts and no position is touched.

Two details that only matter over HTTP: the pairing state must be visible
across requests (a human takes minutes), and a cookie that lands after a
cancel or a superseded attempt must be discarded — both are handled by an
epoch counter on the controller, not by the state word.

Cloudflare's challenge defeats programmatic logins. The fix is **not** a
headless browser or a CAPTCHA bypass — it is a human:

```
cybertrade quotex login
  → Chrome opens qxbroker.com (--user-data-dir=data/chrome-profile,
    --remote-debugging-port=9333, persistent profile: do this once)
  → YOU log in and solve the CAPTCHA by hand
  → pairing polls DevTools on 127.0.0.1:9333 (Storage.getCookies, with
    Network.getAllCookies as fallback) for the `sessionid` cookie
  → session persisted to cfg.qx_session_path (0600): {ssid, cookies, domain}
  → QuotexAPI.set_ssid(ssid, cookies) → websocket authorization §4
```

- Standard library only: `urllib` + `cybertrade.network.websocket`.
  No Playwright, no Selenium, no automation of the challenge itself.
- **The engine refuses synthetic data**: `broker.mode = quotex` wires
  `LiveQuotexFeed` (is_synthetic=False) from a strict session
  resolver; `TradingEngine.__init__` raises `ConfigError` if handed any
  generator-backed feed. Missing session → loud startup error naming
  `quotex login` — never a silent fallback.

## 10. Ghost wire — organic traffic discipline (Phase-30)

"Undetectable" in this codebase means one honest thing: **the client's
network manners are indistinguishable from the Chrome it was paired from
(§9)** — not CAPTCHA bypass, not fingerprint spoofing, not proxy rotation,
none of which exist here by standing rule.

| Mechanism | What it does |
|---|---|
| `ghost.Pacekeeper` | Every venue frame rides class gates: orders get jittered think-time (uniform 0.7–1.4 × `order_think_ms`), a hard `order_min_gap_ms`, and a sliding `max_orders_per_min` window; history/poll and generic frames get their own smaller gaps. A bot signature is *timing* — 12 orders/sec, metronomic think-time — so ours is deliberately sloppy. |
| `ghost.parity_headers` | HTTP + WS handshakes carry the paired browser's truthful extras (UA, `Accept-Language`, no-cache). `Sec-WebSocket-Extensions` is **omitted, never faked** — advertising a capability we do not implement is itself a fingerprint. |
| `ghost.reconnect_delay` | Exponential backoff (2s ×1.8, 60s cap) with ±20–25% jitter so reconnects never land on a fixed grid. |
| `ghost.is_session_fault` | Venue errors meaning "session dead" (`invalid session`, `unauthorized`, `cloudflare`, `captcha`, …) set `session_stale`, log CRITICAL with a `cybertrade quotex login` re-pair hint, and emit `session_stale` — never blind retries. |
| Subscriptions registry | `subscribeCandle` frames are remembered and replayed after *any* reconnect path (client-level `_try_reconnected` hook **or** supervisor-level `api.connect()`), so the chart stream restores itself. |
| `sync.backfill_gaps` | The live feed fetches **only missing bars** (chart-like behaviour) instead of re-pulling full history on a fixed grid; reconnect events trigger an immediate gap sweep. |
| `adapter.reconcile_venue` | Boot pulls the portfolio wire and adopts venue-open contracts (id + asset + plausible expiry) this process didn't place — crash restarts stay reconciled; metadata-less orphans are logged for manual review, never guessed. |

Config knobs (`BrokerConfig`): `ghost_pace` (default **True**),
`order_think_ms=140`, `order_min_gap_ms=350`, `max_orders_per_min=10`.
Timing is injectable (`Pacekeeper(clock=, sleep=, rnd=)`) for tests.
Paper mode is untouched — no venue frames exist to pace.
