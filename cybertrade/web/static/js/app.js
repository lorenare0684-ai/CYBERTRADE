/* CYBERTRADE web terminal controller: state polling + SSE + controls */
"use strict";

const chart = new NeonChart("chart");
let currentAsset = null;
let lastState = null;
let sseTries = 0;

/* ---------- helpers ---------- */
const $ = (id) => document.getElementById(id);

function setLed(id, cls) {
  const el = $(id);
  el.className = "led" + (cls ? " " + cls : "");
}

function fmt(n, d = 2) {
  return (typeof n === "number" && isFinite(n)) ? n.toFixed(d) : "—";
}

function nowClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  $("clock").textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function pushLog(rec) {
  const box = $("console");
  const line = document.createElement("div");
  line.className = "log-" + (rec.level || "INFO");
  const t = new Date((rec.ts || Date.now() / 1000) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  line.textContent =
    `[${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}] ` +
    `${(rec.level || "INFO").padEnd(4)} ${rec.name || ""} │ ${rec.msg || ""}`;
  box.appendChild(line);
  while (box.childElementCount > 300) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

/* ---------- render state ---------- */
function render(state) {
  lastState = state;

  // no venue session yet: the pairing screen is the way in, and the deck
  // behind it must not pretend to have a balance, a candle or an open order.
  if (state && state.paired === false) {
    pair.mode = "boot";
    pairShow(true);
    if (state.pairing) pairRender(state.pairing);
    return;
  }
  if (pair.mode !== "repair" || $("pair-veil").hidden) pairShow(false);

  const snap = state.snapshot || {};
  const health = snap.health || {};
  const acct = snap.account || {};
  const risk = snap.risk || {};
  const regimes = snap.regimes || {};
  const limits = risk.limits || {};
  renderRecovery(snap.continuity || {}, (risk.state || {}).kill);

  // LEDs
  setLed("led-feed", health.feed_ok ? "on" : "warn");
  setLed("led-broker", health.broker_ok ? "on" : "bad");
  const dd = acct.drawdown || 0;
  setLed("led-risk", dd > 0.15 ? "bad" : dd > 0.08 ? "warn" : "on");
  const st = health.engine_state || "disarmed";
  setLed("led-state", st === "live" ? "bad" : st === "armed" ? "on" : st === "kill" ? "bad" : "warn");
  $("led-state").lastChild && ($("led-state").lastChild.textContent = st.toUpperCase());

  // account
  $("balance").textContent = fmt(acct.balance);
  const daily = acct.daily_pnl || 0;
  $("daily").textContent = (daily >= 0 ? "+" : "") + fmt(daily);
  $("daily").style.color = daily >= 0 ? "#39ff5e" : "#ff3860";
  $("winrate").textContent = Math.round((health.win_rate || 0) * 100) + "%";
  $("trades").textContent = health.trades_total || 0;
  if (snap.edge) {
    $("edge-ev").textContent = (snap.edge.breakeven * 100).toFixed(1) + "%";
    $("cal-gap").textContent = fmt(snap.edge.calibration ? snap.edge.calibration.calibration_gap : 0);
    const hon = (snap.edge.calibration && snap.edge.calibration.honesty) || [];
    const he = $("honesty");
    if (he) {
      const liars = hon.filter((r) => r.liar);
      he.textContent = hon.length
        ? `${hon.length} strategies · ${liars.length} LIAR${liars.length === 1 ? "" : "S"}`
          + (liars.length ? ` · worst ${liars[0].strategy} ${Math.round(liars[0].p_edge_negative * 100)}%` : "")
        : "no ledger yet";
      he.className = liars.length ? "mini bad" : "mini";
    }
    const jd = snap.edge.journal;
    const de = $("decay");
    if (de && jd) {
      de.textContent = jd.available
        ? ((jd.quarantined && jd.quarantined.length)
            ? `⛓ QUARANTINE: ${jd.quarantined.join(", ")}`
            : (jd.decaying && jd.decaying.length)
            ? `⚠ DECAY: ${jd.decaying.map((r) => r.strategy).join(", ")}`
            : `${jd.trades} journaled · edge stable`)
        : "decay watch idle";
      de.className = ((jd.quarantined && jd.quarantined.length)
        || (jd.decaying && jd.decaying.length)) ? "mini bad" : "mini";
    }
  }
  // strategy deck (P26): live arsenal with ward badges + ON/OFF toggles
  const stDeck = snap.strategies;
  const deck = $("deck-body");
  if (deck && stDeck && stDeck.members) {
    const key = JSON.stringify([stDeck.members, stDeck.decay_ward]);
    if (deck.dataset.key !== key) {
      deck.dataset.key = key;
      const ward = new Set(stDeck.decay_ward || []);
      let live = 0;
      deck.innerHTML = stDeck.members.map((m) => {
        const badges = [];
        if (m.decay_quarantined || ward.has(m.name)) badges.push("⛓");
        if (m.winrate_quarantined) badges.push("⌂");
        if (!m.enabled) badges.push("OFF");
        else live++;
        const w = (stDeck.weights && stDeck.weights[m.name] != null)
          ? stDeck.weights[m.name] : (m.weight ?? 1);
        return `<div style="display:flex;gap:6px;align-items:center;padding:1px 0;`
          + `border-bottom:1px solid rgba(0,255,255,.08)">`
          + `<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" `
          + `title="${m.label || m.name}">${m.name} `
          + `<span style="opacity:.5">${m.family || ""}</span></span>`
          + `<span style="opacity:.8">${(m.win_rate * 100).toFixed(0)}%·${m.attempts}</span>`
          + `<span style="opacity:.8">×${Number(w).toFixed(2)}</span>`
          + `<span>${badges.join("")}</span>`
          + `<button class="btn sm" data-strat="${m.name}" data-en="${m.enabled ? 0 : 1}">`
          + `${m.enabled ? "ON" : "OFF"}</button></div>`;
      }).join("");
      const dc = $("deck-count");
      if (dc) dc.textContent = `${live}/${stDeck.members.length} live`;
    }
    // Posture bias + vote cap. These change trading behaviour every candle, so
    // they update on every poll rather than only when the member list does.
    const db = $("deck-bias");
    if (db) {
      const fw = stDeck.family_weights || {};
      const names = Object.keys(fw);
      let txt = names.length
        ? "posture bias · " + names
            .map((f) => `${f} ×${Number(fw[f]).toFixed(2)}`).join(" · ")
        : "posture bias · none (flat weighting)";
      if (stDeck.max_votes > 0) {
        txt += ` · cap ${stDeck.max_votes} vote${stDeck.max_votes === 1 ? "" : "s"}/candle`;
      }
      db.textContent = txt;
      db.title = "survivor.regime_rotation pushes the posture table into the "
        + "ensemble blend; strategy.max_signals_per_candle caps votes per candle";
    }
  }

  // meters
  const ddMax = (limits.max_total_drawdown_frac || 0.2) * 100;
  const dlMax = (limits.max_daily_loss_frac || 0.08) * 100;
  const ddPct = (dd * 100).toFixed(1);
  $("dd-bar").style.width = Math.min(100, (dd * 100) / ddMax * 100) + "%";
  $("dd-val").textContent = ddPct + "%";
  const dl = health.daily_loss_frac || 0;
  $("dl-bar").style.width = Math.min(100, (dl * 100) / dlMax * 100) + "%";
  $("dl-val").textContent = (dl * 100).toFixed(1) + "%";
  const open = acct.open_positions || 0, maxc = limits.max_concurrent || 3;
  $("exp-bar").style.width = (open / maxc * 100) + "%";
  $("exp-val").textContent = `${open}/${maxc}`;

  // posture ladder
  const posture = health.posture || "NORMAL";
  document.querySelectorAll(".rung").forEach((r) => {
    r.classList.toggle("active", r.dataset.p === posture);
  });

  // asset tabs
  const tabs = $("asset-tabs");
  if (tabs.childElementCount !== (state.assets || []).length) {
    tabs.innerHTML = "";
    (state.assets || []).forEach((a) => {
      const b = document.createElement("button");
      b.className = "tab"; b.textContent = a.replace("_otc", "");
      b.onclick = () => { currentAsset = a; refreshTabs(); render(lastState); };
      b.dataset.asset = a;
      tabs.appendChild(b);
    });
    if (!currentAsset && state.assets && state.assets.length) currentAsset = state.assets[0];
    refreshTabs();
  }

  // chart
  const candles = (state.candles || {})[currentAsset] || [];
  if (candles.length) {
    chart.setData(candles.map((c) => ({ o: c.o, h: c.h, l: c.l, c: c.c })));
    const last = candles[candles.length - 1];
    $("chart-asset").textContent = currentAsset;
    $("price-tag").textContent = last.c.toFixed(last.c > 50 ? 2 : 5);
  }

  // regime strip
  const reg = regimes[currentAsset] || {};
  $("regime-tag").textContent = "REGIME: " + (reg.regime || "---").toUpperCase();
  $("conf").textContent = fmt(reg.confidence);
  $("stress").textContent = fmt(reg.stress);
  $("volstate").textContent = (reg.volatility_state || "—").toUpperCase();
  const ses = snap.session;
  $("session").textContent = ses
    ? `${ses.name.toUpperCase()} ×${fmt(ses.min_scale)}`
    : new Date().getUTCHours() + " UTC";
  $("posture").textContent = posture;
  $("regime-detail").textContent =
    `REGIME ${(reg.regime || "?")} · trend ${reg.trend_direction ?? "?"} · stress ${fmt(reg.stress)}`;
  if (snap.edge) {
    $("regime-detail").textContent +=
      ` · EDGE be ${(snap.edge.breakeven * 100).toFixed(1)}% gate ${snap.edge.gate} rej ${snap.edge.rejects}` +
      (snap.tape ? ` · tape ${snap.tape.written}` : "");
  }
  $("engine-state").textContent = "STATE: " + (st || "?").toUpperCase();
  $("vetoes").textContent = "vetoes " + (health.vetoes || 0);

  renderBlotter(state.trades || []);
  renderPositions(state.positions || []);
  if (state.logs && state.logs.length) {
    const box = $("console");
    if (!box.childElementCount) state.logs.forEach(pushLog);
  }

  // Phase-2 HUD blocks
  if (equityChart) equityChart.setData(state.equity_curve || []);
  renderIntel(state);
  const bf = $("blackout-flag");
  if (bf) {
    const anyNow = (state.calendar || []).some((e) => {
      const now = (state.ts || Date.now() / 1000);
      return e.window_start <= now && now <= e.window_end;
    });
    bf.textContent = anyNow ? "⚠ BLACKOUT ACTIVE" : "no blackout";
    bf.className = anyNow ? "mini warn" : "mini";
  }
}

function refreshTabs() {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.asset === currentAsset);
  });
}

/* ---------- Phase-2: intel (calendar / alerts / clusters) ---------- */
function renderIntel(state) {
  const cal = (state.calendar || []).slice(0, 5);
  const box = $("cal-list");
  if (box) {
    box.innerHTML = cal.length
      ? cal.map((e) => {
          const t = new Date(e.ts * 1000);
          const hh = String(t.getUTCHours()).padStart(2, "0") + ":" + String(t.getUTCMinutes()).padStart(2, "0");
          const est = e.estimated ? "*" : "";
          return `<span class="cal-item ${e.impact}">${hh} ${e.currency} ${e.title}${est}</span>`;
        }).join("")
      : '<span class="dim">no scheduled events (est.*)</span>';
  }
  const alerts = (state.alerts || []).slice(0, 4);
  const tick = $("alert-ticker");
  if (tick) {
    tick.innerHTML = alerts.length
      ? alerts.map((a) => `<span class="alert-chip ${a.severity}">${a.title} — ${a.body}</span>`).join("")
      : '<span class="dim">alerts idle…</span>';
  }
  const clusters = state.clusters || {};
  const groups = {};
  Object.entries(clusters).forEach(([a, c]) => { (groups[c] = groups[c] || []).push(a); });
  const cm = $("cluster-mini");
  if (cm) {
    cm.textContent = "clusters " + (Object.keys(clusters).length
      ? Object.values(groups).map((g) => "[" + g.map((x) => x.replace("_otc", "")).join("·") + "]").join(" ")
      : "—");
  }
}

/* ---------- Phase-2: Monte Carlo lab ---------- */
let mcChart = null, equityChart = null;

async function refreshMC() {
  try {
    const pm = ($("mc-posterior") && $("mc-posterior").checked) ? "&mode=posterior" : "";
    const r = await fetch(`/api/montecarlo?runs=300&horizon=200${pm}`);
    const rep = await r.json();
    if (rep.empty) {
      const v = $("mc-verdict");
      if (v) { v.textContent = rep.verdict || "NO DATA"; v.className = "warn"; }
      if ($("mc-source")) $("mc-source").textContent = rep.summary || "no settled trades";
      return;
    }
    if (mcChart) mcChart.setData(rep.bands || [], rep.starting_balance);
    const v = $("mc-verdict");
    if (v) {
      v.textContent = rep.verdict || "—";
      v.className = /RUIN|DANGEROUS/.test(rep.verdict || "") ? "bad"
        : /CAUTION/.test(rep.verdict || "") ? "warn" : "ok";
    }
    if ($("mc-p05")) $("mc-p05").textContent = (rep.p05_terminal ?? 0).toFixed(0);
    if ($("mc-p50")) $("mc-p50").textContent = (rep.p50_terminal ?? 0).toFixed(0);
    if ($("mc-p95")) $("mc-p95").textContent = (rep.p95_terminal ?? 0).toFixed(0);
    if ($("mc-ruin")) $("mc-ruin").textContent = ((rep.risk_of_ruin ?? 0) * 100).toFixed(1) + "%";
    if ($("mc-source")) {
      $("mc-source").textContent =
        `${rep.runs || 0} paths · ${rep.horizon || 0} trades · src ${rep.source || "?"} (${rep.n_trades || 0} settled)`;
    }
  } catch (e) { /* lab is advisory only — never break the HUD */ }
}

function renderBlotter(trades) {
  const body = $("blotter-body");
  body.innerHTML = "";
  trades.slice(-12).reverse().forEach((t) => {
    const tr = document.createElement("tr");
    tr.className = t.refunded ? "refund" : t.won ? "won" : "lost";
    const ts = new Date(t.ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    tr.innerHTML =
      `<td>${p(ts.getHours())}:${p(ts.getMinutes())}:${p(ts.getSeconds())}</td>` +
      `<td>${t.asset}</td><td>${t.side.toUpperCase()}</td>` +
      `<td>${fmt(t.stake)}</td><td>${fmt(t.strike, 5)}</td>` +
      `<td>${fmt(t.expiry_price, 5)}</td>` +
      `<td>${t.refunded ? "REFUND" : t.won ? "WON" : "LOST"}</td>` +
      `<td>${(t.pnl >= 0 ? "+" : "") + fmt(t.pnl)}</td>`;
    body.appendChild(tr);
  });
  $("blotter-count").textContent = `${trades.length} settled`;
}

/* ---------- Phase-32: durable recovery status (not a trading signal) ---------- */
function renderRecovery(info, killed) {
  const el = $("recovery-status");
  if (!el) return;
  const blocked = Boolean(info.blocked || killed);
  let message = "RECOVERY OFF · ephemeral engine";
  if (info.enabled) {
    const saved = info.saved_ts ? new Date(info.saved_ts * 1000).toLocaleTimeString() : "pending";
    message = info.blocked ? `RECOVERY HOLD · ${info.reason || "operator review required"}`
      : `RECOVERY ${info.restored ? "RESTORED" : "ACTIVE"} · checkpoint ${saved}`;
    if (killed && !info.blocked) message += " · KILL LATCHED";
  }
  el.textContent = message;
  el.className = "recovery-status" + (blocked ? " blocked" : "");
  for (const id of ["btn-arm", "btn-call", "btn-put"]) {
    const button = $(id);
    if (button) {
      button.disabled = blocked;
      button.title = blocked ? (info.reason || "Clear the latched kill before trading") : "";
    }
  }
}

function escapeCell(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g,
    (ch) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]));
}

/* ---------- Phase-27: open positions + per-position cut ---------- */
function renderPositions(list) {
  const body = $("pos-body");
  if (!body) return;
  const rows = Array.isArray(list) ? list : [];
  const key = JSON.stringify(rows);
  if (body.dataset.key === key) return;
  body.dataset.key = key;
  body.innerHTML = rows.map((p) => {
    const cls = p.state === "itm" ? "won" : p.state === "otm" ? "lost" : "";
    const left = Math.max(0, Math.round(p.seconds_left || 0));
    const mm = String(Math.floor(left / 60)).padStart(2, "0");
    const ss = String(left % 60).padStart(2, "0");
    return `<tr class="${cls}">`
      + `<td>${escapeCell(p.strategy || "—")}</td><td>${escapeCell(p.asset)}</td>`
      + `<td>${escapeCell(String(p.side || "").toUpperCase())}</td>`
      + `<td>${fmt(p.stake)}</td><td>${fmt(p.strike, 5)}</td>`
      + `<td>${fmt(p.mark, 5)}</td>`
      + `<td>${escapeCell(String(p.state || "").toUpperCase())}</td><td>${p.recovery_hold ? "OFFLINE" : `${mm}:${ss}`}</td>`
      + (p.recovery_hold
        ? `<td><button class="btn sm yellow" data-resolve="${escapeCell(p.id)}">RESOLVE</button></td>`
        : `<td><button class="btn sm" data-close="${escapeCell(p.id)}">CLOSE</button></td>`)
      + `</tr>`;
  }).join("");
  const c = $("pos-count");
  if (c) c.textContent = `${rows.length} open`;
}

/* ---------- networking ---------- */
async function pollState() {
  try {
    const r = await fetch("/api/state");
    if (r.ok) render(await r.json());
    $("fps").textContent = "POLL ok";
  } catch (e) {
    $("fps").textContent = "POLL fail";
  }
}

function openSSE() {
  const es = new EventSource("/api/events");
  es.addEventListener("msg", (ev) => {
    try {
      const item = JSON.parse(ev.data);
      if (item.kind === "log") pushLog(item.data);
      else if (item.kind === "tick" || item.kind === "settle" || item.kind === "signal") pollState();
    } catch (e) { /* ignore */ }
    $("fps").textContent = "SSE live";
  });
  es.onerror = () => {
    es.close();
    sseTries++;
    $("fps").textContent = "SSE reconnect " + sseTries;
    setTimeout(openSSE, 2000);
  };
}

/* ---------- venue pairing (Chrome-assisted Quotex login) ---------- */
const pair = {
  timer: null,
  busy: false,
  mode: "boot",        // "boot" = no session yet, "repair" = re-pairing live
};

function pairSay(text, cls) {
  const el = $("pair-status");
  if (!el) return;
  el.textContent = text;
  el.className = "pair-status" + (cls ? " " + cls : "");
}

function pairPurse() {
  const el = document.querySelector('input[name="purse"]:checked');
  if (!el) return null;
  return el.value === "practice";
}

function pairShow(show) {
  const v = $("pair-veil");
  if (v) v.hidden = !show;
  if (!show) pairStopPolling();
}

/* Open the pairing screen against an already-live terminal, for the case
   where a session dies mid-run: the engine keeps its state and the cookie is
   re-seated in place, so nothing restarts and no position is touched. */
async function pairReveal() {
  pair.mode = "repair";
  pairShow(true);
  $("pair-cancel").disabled = true;
  pairSay("re-pair the venue session — the running terminal picks up the new cookie in place", "");
  await pairPoll();
}

function pairStopPolling() {
  if (pair.timer) {
    clearInterval(pair.timer);
    pair.timer = null;
  }
}

async function pairPost(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

async function pairStart() {
  if (pair.busy) return;
  const purse = pairPurse();
  if (purse === null) {
    pairSay("pick a purse: PRACTICE or REAL MONEY", "bad");
    return;
  }
  pair.busy = true;
  $("pair-start").disabled = true;
  $("pair-cancel").disabled = false;
  pairSay("launching Chrome — log in and solve the CAPTCHA in the window that opens…", "busy");
  const r = await pairPost("/api/pair/start", {
    profile: $("pair-profile").value,
    port: $("pair-port").value,
    timeout: $("pair-timeout").value,
    purse: purse,
  });
  if (!r.ok) {
    pair.busy = false;
    $("pair-start").disabled = false;
    $("pair-cancel").disabled = true;
    pairSay(r.error || "could not start pairing", "bad");
    return;
  }
  pairRender(r);
  pairStopPolling();
  pair.timer = setInterval(pairPoll, 1500);
}

async function pairPoll() {
  try {
    const r = await fetch("/api/pair/status");
    if (!r.ok) return;
    pairRender(await r.json());
  } catch (e) { /* keep polling */ }
}

function pairRender(s) {
  const st = s.state || "idle";
  const msg = s.message || "";
  if (st === "ready") {
    pairSay(msg || "session captured — the terminal is live", "ok");
    pairStopPolling();
    pair.busy = false;
    pair.mode = "boot";
    $("pair-cancel").disabled = true;
    setTimeout(() => { pairShow(false); pollState(); }, 1200);
    return;
  }
  if (st === "failed") {
    pairSay((s.error ? s.error + " — " : "") + "pairing failed. Fix it and start again.", "bad");
    pairStopPolling();
    pair.busy = false;
    $("pair-start").disabled = false;
    $("pair-cancel").disabled = true;
    return;
  }
  if (st === "launching" || st === "waiting") {
    const wait = s.elapsed ? ` (${Math.round(s.elapsed)}s elapsed)` : "";
    pairSay(msg + wait, "busy");
    return;
  }
  // idle
  pairSay(msg || "no venue session yet — pair one below", "");
}

async function pairCancel() {
  pairStopPolling();
  pair.busy = false;
  $("pair-start").disabled = false;
  $("pair-cancel").disabled = true;
  if (pair.mode === "repair") {
    pair.mode = "boot";
    pairShow(false);            // closing is not an error: the engine lives
    return;
  }
  const r = await pairPost("/api/pair/cancel", {});
  pairSay(r.error || "pairing cancelled", "");
}

if ($("pair-start")) $("pair-start").onclick = pairStart;
if ($("pair-cancel")) $("pair-cancel").onclick = pairCancel;
if ($("pair-open")) {
  $("pair-open").onclick = pairReveal;
  $("pair-open").onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") pairReveal(); };
}

async function cmd(body) {
  try {
    const r = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await pollState();
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/* ---------- controls ---------- */
// LIVE ONLY: arming from the browser still asks first — real order flow.
$("btn-arm").onclick = () => {
  const ok = window.confirm(
    "ARM LIVE TRADING?\n\n"
    + "This engine places REAL orders at Quotex with the configured purse.\n"
    + "Cancel to stay disarmed."
  );
  if (ok) cmd({ cmd: "arm" });
};
$("btn-disarm").onclick = () => cmd({ cmd: "disarm" });
$("btn-kill").onclick = () => cmd({ cmd: "kill" });
$("btn-call").onclick = () => cmd({ cmd: "trade", side: "call", asset: currentAsset, amount: 5 });
$("btn-put").onclick = () => cmd({ cmd: "trade", side: "put", asset: currentAsset, amount: 5 });
$("btn-news").onclick = () => cmd({ cmd: "news" });
$("btn-lock").onclick = () => cmd({ cmd: "lockdown" });
$("btn-clear").onclick = () => cmd({ cmd: "unlock" });
// strategy deck toggles (P26) — one delegated listener
const deckEl = $("deck-body");
deckEl && deckEl.addEventListener("click", (e) => {
  const b = e.target.closest("[data-strat]");
  if (!b) return;
  cmd({ cmd: "strategy", name: b.dataset.strat, enabled: b.dataset.en === "1" });
});
const posEl = $("pos-body");
posEl && posEl.addEventListener("click", (e) => {
  const b = e.target.closest("[data-close]");
  if (!b) return;
  cmd({ cmd: "close", position: b.dataset.close });
});
$("btn-mc") && ($("btn-mc").onclick = () => refreshMC());

/* ---------- boot ---------- */
setInterval(nowClock, 1000);
nowClock();
pollState();
setInterval(pollState, 2500);
openSSE();
if ($("equity-chart")) equityChart = new window.NeonLine($("equity-chart"));
if ($("mc-chart")) mcChart = new window.NeonBands($("mc-chart"));
refreshMC();
setInterval(refreshMC, 30000);

/* ---------- Phase-31: resolution-aware canvases + screen class ---------- */
function fitCanvas(cv) {
  if (!cv) return false;
  const holder = cv.parentElement;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  // Prefer the canvas's own CSS box (mc/equity canvases sit in taller panels);
  // fall back to the holder for zero-size pre-layout canvases.
  const cssW = Math.max(120, Math.round(cv.clientWidth || (holder && holder.clientWidth) || 300));
  const cssH = Math.max(80, Math.round(cv.clientHeight || (holder && holder.clientHeight) || 140));
  const w = Math.round(cssW * dpr);
  const h = Math.round(cssH * dpr);
  if (cv.width === w && cv.height === h) return false;
  cv.width = w;
  cv.height = h;
  return true;
}

function fitAllCanvases() {
  let changed = false;
  if (fitCanvas($("chart"))) changed = true;
  if (fitCanvas($("mc-chart")) && mcChart && mcChart.render) mcChart.render();
  if (fitCanvas($("equity-chart")) && equityChart && equityChart.render) equityChart.render();
  if (changed && chart && chart.render) chart.render();
  // mirror gui/layout.py breakpoints for anything JS-side that cares
  const w = window.innerWidth || document.documentElement.clientWidth;
  const cls = (w < 1100 || window.innerHeight < 620) ? "compact"
            : (w < 1600) ? "medium"
            : (w < 2560) ? "large" : "wide";
  document.documentElement.dataset.screenClass = cls;
}

window.addEventListener("resize", fitAllCanvases);
if (window.ResizeObserver) {
  const ro = new ResizeObserver(() => fitAllCanvases());
  const deck = document.querySelector(".deck");
  if (deck) ro.observe(deck);
}
fitAllCanvases();
