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
  const snap = state.snapshot || {};
  const health = snap.health || {};
  const acct = snap.account || {};
  const risk = snap.risk || {};
  const regimes = snap.regimes || {};
  const limits = risk.limits || {};

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
  const dr = $("drill");
  if (dr) {
    const d = snap.drill;
    dr.textContent = d
      ? `DRILL ${String(d.name).toUpperCase()} · ${d.ticks} steps · floor ${d.posture_min}`
        + ` · salv ${d.salvaged}${d.killed ? " · KILLED" : ""}`
      : "drill idle";
    dr.className = d ? "mini bad" : "mini";
  }
  // score bay (P23)
  const job = snap.job;
  const jobEl = $("job");
  if (jobEl && job) {
    jobEl.textContent = job.state === "running"
      ? `JOB ${String(job.kind || "?").toUpperCase()} RUNNING…`
      : `JOB ${String(job.state || "idle").toUpperCase()}`;
    jobEl.className = (job.state === "running" || job.state === "error") ? "mini bad" : "mini";
  }
  const sc = $("scorecard");
  if (sc && job && (job.state === "done" || job.state === "error")) {
    const key = `${job.kind}:${job.state}:${job.finished || job.started}`;
    if (sc.dataset.key !== key) {
      sc.dataset.key = key;
      if (job.state === "error") {
        sc.textContent = "JOB ERROR: " + (job.error || "unknown");
      } else {
        const r = job.result || {};
        const rows = (r.rows || []).map(x =>
          `${String(x.name).padEnd(20)} ${String(x.posture_min).padEnd(14)} `
          + `salv ${x.salvaged} pnl ${(Number(x.pnl) || 0).toFixed(2)}  ${x.verdict}`
        ).join("\n");
        sc.textContent = [
          r.table || "",
          rows,
          r.card || "",
          r.runs ? `runs ${r.runs}${r.alive != null ? ` · alive ${r.alive}` : ""}` : "",
        ].filter(Boolean).join("\n\n");
      }
      sc.style.display = "block";
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

/* ---------- Phase-2: scenario hot-swap ---------- */
async function loadScenarios() {
  try {
    const r = await fetch("/api/scenarios");
    const list = await r.json();
    const sel = $("scenario-select");
    if (!sel || !Array.isArray(list)) return;
    (list || []).forEach((s) => {
      const o = document.createElement("option");
      o.value = s.name; o.textContent = s.name + " — " + (s.risk || "");
      sel.appendChild(o);
    });
  } catch (e) { /* non-fatal */ }
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
$("btn-arm").onclick = () => cmd({ cmd: "arm" });
$("btn-disarm").onclick = () => cmd({ cmd: "disarm" });
$("btn-kill").onclick = () => cmd({ cmd: "kill" });
$("btn-call").onclick = () => cmd({ cmd: "trade", side: "call", asset: currentAsset, amount: 5 });
$("btn-put").onclick = () => cmd({ cmd: "trade", side: "put", asset: currentAsset, amount: 5 });
$("btn-news").onclick = () => cmd({ cmd: "news" });
$("btn-lock").onclick = () => cmd({ cmd: "lockdown" });
$("btn-clear").onclick = () => cmd({ cmd: "unlock" });
const DRILLS = {
  "btn-drill-crash": "flash_crash",
  "btn-drill-gap": "gap_open",
  "btn-drill-news": "news_spike",
  "btn-drill-vac": "liquidity_vacuum",
  "btn-drill-whip": "regime_whipsaw",
};
for (const [id, scenario] of Object.entries(DRILLS)) {
  const el = $(id);
  el && (el.onclick = () => cmd({ cmd: "drill", scenario }));
}
$("btn-drill-stop") && ($("btn-drill-stop").onclick = () => cmd({ cmd: "drill", scenario: "stop" }));
const RUNS = { "btn-run-bt": "backtest", "btn-run-gauntlet": "gauntlet" };
for (const [id, kind] of Object.entries(RUNS)) {
  const el = $(id);
  el && (el.onclick = () => cmd({ cmd: "run", kind }));
}
// strategy deck toggles (P26) — one delegated listener
const deckEl = $("deck-body");
deckEl && deckEl.addEventListener("click", (e) => {
  const b = e.target.closest("[data-strat]");
  if (!b) return;
  cmd({ cmd: "strategy", name: b.dataset.strat, enabled: b.dataset.en === "1" });
});
$("btn-scenario") && ($("btn-scenario").onclick = async () => {
  const sel = $("scenario-select");
  if (!sel || !sel.value || !currentAsset) return;
  await cmd({ cmd: "scenario", asset: currentAsset, scenario: sel.value });
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
loadScenarios();
refreshMC();
setInterval(refreshMC, 30000);
