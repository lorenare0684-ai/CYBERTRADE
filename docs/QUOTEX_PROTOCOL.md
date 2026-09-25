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
  `payload`, from a bare JSON string, or from the `session` / `ssid` /
  `qx_session` / `sessionid` / `PHPSESSID` cookie. Chunked+gzipped bodies
  are decoded.
- **Error taxonomy** — HTTP 401 says bad password; a challenge page (HTML /
  `cf-challenge` markers) says to pair via `cybertrade quotex login`
  instead of blaming the password.
- **Pasted frames** — `set_ssid` unwraps a copied
  `42["authorization",{"session":"…",…}]` frame down to the token.
- **Explicit rejection** — `authorization/reject` (or an `invalid session` /
  `unauthorized` error) during authorization raises `BrokerAuthError`
  immediately (re-pair hint included) instead of proceeding as a fake-live
  login; `s_authorization` (or the first data event) counts as
  authorization so healthy connects return fast.
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
| `5`/`6` | binary event / ack: `451-["name",{"_placeholder":true,"num":0}]`, then the JSON payload as raw binary frame(s) |

## 4. Authentication frame (confirmed shape)

```json
42["authorization",{"session":"<ssid>","isDemo":1,"tournamentId":0}]
```

`isDemo: 1` selects the **PRACTICE** purse, `0` the REAL purse.  The venue
confirms with an `s_authorization` event and refuses with
`authorization/reject` (fail fast, re-pair — never retry).

## 5. Trading frames

### Place a binary option (`orders/open`, byte-shape of pyquotex `Buy`)

```json
42["settings/apply",{"chartId":"graph","settings":{"chartId":"graph","chartType":2,
  "currentExpirationTime":1637892541,"isFastOption":false,"isFastAmountOption":false,
  "isIndicatorsMinimized":false,"isIndicatorsShowing":true,"isShortBetElement":false,
  "chartPeriod":4,"currentAsset":{"symbol":"AUDCAD_otc"},"dealValue":6,
  "dealPercentValue":1,"isVisible":true,"timePeriod":60,"gridOpacity":8,
  "isAutoScrolling":1,"isOneClickTrade":true,"upColor":"#0FAF59","downColor":"#FF6251"}}]
42["orders/open",{
  "asset":"AUDCAD_otc",
  "amount":6,
  "time":60,
  "action":"put",
  "isDemo":1,
  "tournamentId":0,
  "requestId":1637892541,
  "optionType":100
}]
```

- `action`: `"call"` | `"put"`
- `requestId`: **integer** epoch seconds (monotonic per process); the venue
  echoes it on the ack — that is how a fill is matched.
- Two contract clocks (`broker.time_mode`):
  - **TIMER** (default): `optionType: 100`, `time` = **duration in
    seconds**.  Expires exactly `duration` after the fill — matches the
    local settlement clock; works for OTC and non-OTC, 5s and up.
  - **TIME**: `optionType: 3` (fast option; `1` = classic), `time` =
    **period-aligned expiry timestamp** from `protocol.expiration_for`
    (port of pyquotex `get_expiration_time_quotex`: next minute boundary
    for <60s, else the next `duration` grid line since midnight, skipping
    a step once more than half the period elapsed).  `settings/apply`
    then carries `isFastOption:true` + `endTime`.  A raw `now + duration`
    is **not** on the grid and the venue refuses it silently — the bug
    that made every live order vanish.
- `settings/apply` precedes each order (what the tab does on every
  asset/expiry change); it is advisory and failure-tolerant.

### Order lifecycle (server → client, binary attachments)

```
451-["s_orders/open",{"_placeholder":true,"num":0}]   + raw frame:
{"id":123456789,"requestId":1637892541,"asset":"AUDCAD_otc","amount":6,
 "command":1,"openPrice":0.9123,"closePrice":0,"profit":5.1,"percentProfit":85,
 "openTimestamp":1637892541,"closeTimestamp":1637892601,"isDemo":1,"uid":42,…}
```

- `id` is the venue **ticket** (needed for sell-back); `command` 0 = call,
  1 = put; `profit` on the *ack* is the **potential** win — never a result.
- Settlement arrives as `{"deals":[{"id":…,"profit":-6,"closePrice":…},…]}`
  (bare frame or `orders/closed` / `s_orders/close`): `profit > 0` win,
  `< 0` loss, `== 0` refund.  `QuotexAPI` folds acks + settlements into
  order state (`order_result()`, `order_id_for()`), and the adapter adopts
  the ticket onto the fill and settles the position on the venue's verdict.

### Legacy placement (older clients / `stable_api.buy`)

```json
42["buyOption",{"asset":"AUDCAD_otc","amount":6,"action":"put","duration":60,"isDemo":1,"requestId":1637892541}]
```

### Early sale / close

```json
42["orders/cancel",{"ticket":123456789}]
```

`ticket` is the venue order id from the `s_orders/open` ack (ints ride as
ints).  `QuotexAPI.sell_option()` translates a `requestId` into the ticket
when the ack has landed.  Older docs' `sellOption` / `orders/close` frames
are not answered by the live venue (`orders/close` is a *server* event).

### Purse switching (deferred past data verification)

```json
42["account/change",{"demo":1,"tournamentId":0}]   // demo:0 = REAL purse
```

Balances arrive pushed — there is no balance query.  The auth frame's
`isDemo` already selects the purse (all pyquotex ever sends on
connect), so `connect()` sends no switch: an `account/change` inside
the connect burst is the one structural difference behind starving
wires.  Boot calls `ensure_purse()` after data is proven, waits for
the `s_account/change` ack, and re-proves the stream — a switch that
starves the wire fails loudly there, before any order can touch the
wrong money.

### Session bootstrap (what the trade tab sends on every open)

```json
42["indicator/list"]
42["drawing/load"]
42["pending/list"]
42["chart_notification/get"]
42["instruments/get"]
```

### Application heartbeat (~every 5s — the venue expects it)

```json
42["tick"]
```

### Market data — the subscribe trio + history

```json
42["instruments/update",{"asset":"EURUSD_otc","period":60}]
42["chart_notification/get",{"asset":"EURUSD_otc","version":"1.0.0"}]
42["depth/follow","EURUSD_otc"]
42["history/load",{"asset":"EURUSD_otc","index":7,"time":1700000000,"offset":12000,"period":60}]
42["instruments/unsubscribe",{"asset":"EURUSD_otc"}]
42["depth/unfollow","EURUSD_otc"]
42["portfolio",{}]
```

`history/load`: `time` = window end (epoch seconds), `offset` = lookback in
*seconds* (`count × period`), `index` echoes back for correlation.  Any one
of the subscribe trio alone leaves the wire silent — all three, every time.
Older community docs named `subscribeCandle` / `candleHistory` /
`changeBalance` / `instrument` frames; the live venue never answers them.

## 6. Server → client events (parsed defensively)

| Event | Payload (community shapes) |
|-------|----------------------------|
| `candles` (legacy tolerance) | `{"asset":..., "candles":[{t,o,h,l,c}\| [t,o,c,h,l] ...]}` |
| `s_authorization` / `authorization/reject` | auth accepted (payload carries `{liveBalance, demoBalance, uid, …}` — absorbed as the first balance push) / refused |
| `s_account/change` (any `s_*`) | server confirm of the matching request (purse switch, …) |
| `instruments/list` (binary) | positional rows `[id, symbol, name, type, ?, payment, …, open@14, …, turbo@18, 24H/1M/5M@-10/-9/-8]` |
| `history/load` / `history/list/v2` (binary) | `{asset, index, candles: [[ts, price, direction], …]}` — ticks, aggregated client-side into OHLC (forming bar dropped) |
| `candle-generated` | `{asset, period, index, open, high, low, close}` — a closed bar |
| `quotes/stream` (binary) | `[[asset, ts, price, direction], …]` — the live quote push (bare batches without an event name are tolerated too) |
| `depth/change` (binary) | `[[asset, depth]]` — book depth for a followed asset (counts as "venue streams to us") |
| `balance` | `{demoBalance, liveBalance, …}` — pick by active purse |
| `s_orders/open` / `orders/opened` (binary) | order **ack**: `{id (ticket), requestId, asset, amount, command, openPrice, percentProfit, closeTimestamp, …}` |
| `deals` / `orders/closed` / `s_orders/close` (binary) | **settlement** rows `{id, profit, closePrice, …}` — sign of `profit` is the verdict |
| `order` / `orderResult` (legacy tolerance) | `{"id","requestId","status","openPrice","closePrice","profit"}` |
| `profit` | settlement PnL update |
| `notification` / `error` | human-readable strings/dicts |

All parsers live in `cybertrade/brokers/quotex/protocol.py`.  Binary
attachments (`451-` placeholder + a websocket **binary** frame) are
correlated with their placeholder in the socket client.  **Engine.IO v3
prefixes every binary frame with the packet-type byte `0x04`** — the
attachment on the wire is `\x04[["EURGBP",…]]`, and the decoder strips
that byte (raw or base64 `BFtb…`) before parsing.  Missing that strip
made every attachment undecodable and the session look
"authorized but starving" (Sep-2026 live capture).  Undecodable
attachments and stale placeholders now warn (sampled) instead of
vanishing at debug level.  The client sends **no `40`** after the
handshake: with `EIO=3` the server auto-connects `/` (browser and
pyquotex parity); legacy
`candleHistory` / `tick`-event / dict shapes are still tolerated.

## 6b. Instrument catalog + history sync (implemented)

- `42["instruments/get"]` requests the instrument listing; the reply
  (`instruments/list` first, then legacy `instrument` / `instruments` /
  `assets_list` / `assetList` / `assets` / `asset_list` — see
  `INSTRUMENT_EVENTS`) is absorbed by `protocol.parse_instruments`, which
  reads the live positional rows (`[id, symbol, name, type, ?, payment, …,
  open@14, …]`) and still tolerates four legacy community shapes:
  `{name: {…}}` mappings, `{asset|name|symbol, …}` rows, `[name, {…}]` pairs,
  and nested lists wrapping any of them.  Payouts accept `payout`/`profit` /
  `payoutPercent` (fraction or percent), open state `open`/`isOpen`/`active`,
  and asset class via `type`/`kind` into `AssetCatalog`
  (`brokers/quotex/catalog.py`).
- Detection runs three ways: the live feed requests the listing on start
  and on every reconnect; any quote for an unknown symbol registers it
  (kind inferred, payout static); and the engine adopts every listing row
  into the running universe (book + detector + subscription).  Anything the
  static table missed still appears the moment the venue names it.
- `payout_for()` consults the live catalog first, then
  `cybertrade.constants.ASSET_CATALOG` — a ~200-symbol static floor (forex +
  OTC, crypto, metals, energy, indices, stock OTCs, incl. verified venue ids)
  that keeps payouts honest while a session is still connecting.  Boards
  show LIVE once a venue listing lands, STATIC before that.
- `cybertrade quotex assets` prints the merged board grouped by asset class.
- History warm-start: `history/load` fills `QuotexAPI`'s cache (tick rows
  aggregated into OHLC, forming bar dropped);
  `brokers/quotex/sync.py` pushes it into `CandleSeries`/`HistoryBuffer`
  books (dupe-safe, failure-tolerant) so live strategies get indicator warmup
  exactly like the live feed does.
- Quotes arrive as bare `[[asset, ts, price, direction]]` batches; the
  dispatcher also tolerates legacy dict rows, bare price scalars, and
  `[asset, price, ts?]` rows.  Balances tolerate scalar pushes.

## 7. Known gaps / drift risks

1. **Cloudflare** — headless `api/signin` may return an HTML challenge; fall
   back to browser-derived `ssid`.
2. **Event renames** — `buyOption` → `orders/open` happened once, and the
   whole camelCase market-data vocabulary (`subscribeCandle` /
   `candleHistory` / `changeBalance`) turned out to be unanswered by the
   live venue (Sep-2026 realignment to `instruments/update` /
   `history/load` / `account/change`, proven against pyquotex master).
   `cybertrade/brokers/quotex/constants.py` keeps the live names plus
   legacy tolerance; override there.
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
    Network.getAllCookies as fallback) for the session cookie, with the
    trade page's `window.settings.token` (Runtime.evaluate) as fallback
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
| `ghost.is_session_fault` | Venue errors meaning "session dead" (`authorization/reject`, `invalid session`, `unauthorized`, `cloudflare`, `captcha`, …) set `session_stale`, log CRITICAL with a `cybertrade quotex login` re-pair hint, and emit `session_stale` — never blind retries. |
| Subscriptions registry | The subscribe trio (`instruments/update` + `chart_notification/get` + `depth/follow`) is remembered and replayed after *any* reconnect path (client-level `_try_reconnected` hook **or** supervisor-level `api.connect()`), so the chart stream restores itself. A `42["tick"]` heartbeat every ~5s plus the post-auth bootstrap complete the browser-identical footprint. |
| `sync.backfill_gaps` | The live feed fetches **only missing bars** (chart-like behaviour) instead of re-pulling full history on a fixed grid; reconnect events trigger an immediate gap sweep. |
| `adapter.reconcile_venue` | Boot pulls the portfolio wire and adopts venue-open contracts (id + asset + plausible expiry) this process didn't place — crash restarts stay reconciled; metadata-less orphans are logged for manual review, never guessed. |

Config knobs (`BrokerConfig`): `ghost_pace` (default **True**),
`order_think_ms=140`, `order_min_gap_ms=350`, `max_orders_per_min=10`.
Timing is injectable (`Pacekeeper(clock=, sleep=, rnd=)`) for tests.
Paper mode is untouched — no venue frames exist to pace.
