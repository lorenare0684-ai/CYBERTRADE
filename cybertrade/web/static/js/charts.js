/* CYBERTRADE canvas charting: neon candlesticks + EMA + Bollinger bands */
"use strict";

class NeonChart {
  constructor(canvasId) {
    this.canvas = document.getElementById(canvasId);
    this.ctx = this.canvas.getContext("2d");
    this.candles = [];
    this.padding = { top: 24, right: 64, bottom: 22, left: 10 };
  }

  setData(candles) { this.candles = candles || []; this.render(); }

  _bounds() {
    let lo = Infinity, hi = -Infinity;
    for (const c of this.candles) {
      lo = Math.min(lo, c.l); hi = Math.max(hi, c.h);
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    const pad = (hi - lo) * 0.08 || 1e-6;
    return [lo - pad, hi + pad];
  }

  _x(i) {
    const w = this.canvas.width - this.padding.left - this.padding.right;
    const step = w / Math.max(this.candles.length, 1);
    return this.padding.left + i * step + step / 2;
  }

  _y(price, lo, hi) {
    const h = this.canvas.height - this.padding.top - this.padding.bottom;
    return this.padding.top + (hi - price) / (hi - lo) * h;
  }

  render() {
    const { ctx, canvas } = this;
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    // grid
    ctx.strokeStyle = "rgba(0,255,249,0.07)";
    ctx.lineWidth = 1;
    for (let gy = 0; gy <= 6; gy++) {
      const y = this.padding.top + gy * (H - this.padding.top - this.padding.bottom) / 6;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    if (!this.candles.length) return;

    const [lo, hi] = this._bounds();

    // price labels
    ctx.fillStyle = "#4a5680";
    ctx.font = "11px 'Share Tech Mono', monospace";
    for (let gy = 0; gy <= 6; gy++) {
      const price = hi - (hi - lo) * gy / 6;
      const y = this.padding.top + gy * (H - this.padding.top - this.padding.bottom) / 6;
      ctx.fillText(price.toFixed(5), W - this.padding.right + 8, y + 4);
    }

    // Bollinger + EMA overlays
    this._bands(lo, hi);
    this._ema(9, "#00fff9", lo, hi);
    this._ema(21, "#ff2bd6", lo, hi);

    // candles
    const step = (W - this.padding.left - this.padding.right) / this.candles.length;
    const bw = Math.max(2, Math.min(14, step * 0.6));
    this.candles.forEach((c, i) => {
      const x = this._x(i);
      const yO = this._y(c.o, lo, hi), yC = this._y(c.c, lo, hi);
      const yH = this._y(c.h, lo, hi), yL = this._y(c.l, lo, hi);
      const up = c.c >= c.o;
      const color = up ? "#39ff5e" : "#ff3860";
      ctx.strokeStyle = color;
      ctx.fillStyle = up ? "rgba(57,255,94,0.7)" : "rgba(255,56,96,0.7)";
      ctx.shadowColor = color; ctx.shadowBlur = 6;
      ctx.beginPath(); ctx.moveTo(x, yH); ctx.lineTo(x, yL); ctx.stroke();
      ctx.fillRect(x - bw / 2, Math.min(yO, yC), bw, Math.max(2, Math.abs(yC - yO)));
      ctx.shadowBlur = 0;
    });

    // last price line
    const last = this.candles[this.candles.length - 1];
    const y = this._y(last.c, lo, hi);
    ctx.strokeStyle = "rgba(255,230,0,0.6)";
    ctx.setLineDash([6, 4]);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W - this.padding.right, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#ffe600";
    ctx.fillText(last.c.toFixed(5), W - this.padding.right + 8, y + 4);
  }

  _ema(period, color, lo, hi) {
    const k = 2 / (period + 1);
    let ema = null;
    const pts = [];
    for (const c of this.candles) {
      ema = ema === null ? c.c : c.c * k + ema * (1 - k);
      pts.push(ema);
    }
    this._line(pts, color, lo, hi, 1.4);
  }

  _bands(lo, hi) {
    const n = 20;
    const up = [], mid = [], dn = [];
    for (let i = 0; i < this.candles.length; i++) {
      if (i < n - 1) { up.push(null); mid.push(null); dn.push(null); continue; }
      let s = 0;
      for (let j = i - n + 1; j <= i; j++) s += this.candles[j].c;
      const m = s / n;
      let v = 0;
      for (let j = i - n + 1; j <= i; j++) v += (this.candles[j].c - m) ** 2;
      const sd = Math.sqrt(v / n);
      mid.push(m); up.push(m + 2 * sd); dn.push(m - 2 * sd);
    }
    this._line(up, "rgba(0,255,249,0.35)", lo, hi, 1);
    this._line(dn, "rgba(0,255,249,0.35)", lo, hi, 1);
    this._line(mid, "rgba(255,255,255,0.25)", lo, hi, 1);
  }

  _line(values, color, lo, hi, width) {
    const { ctx } = this;
    ctx.strokeStyle = color; ctx.lineWidth = width;
    ctx.beginPath();
    let started = false;
    values.forEach((v, i) => {
      if (v === null) return;
      const x = this._x(i), y = this._y(v, lo, hi);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.lineWidth = 1;
  }
}

/* ---------- Phase-2: equity curve panel ---------- */
class NeonLine {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.points = [];            // [[ts, value], ...]
    this.color = "#39ff5e";
  }
  setData(points) { this.points = points || []; this.draw(); }
  draw() {
    const { ctx, canvas } = this;
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (this.points.length < 2) {
      ctx.fillStyle = "rgba(160,170,190,0.5)";
      ctx.font = "11px monospace";
      ctx.fillText("awaiting equity data…", 12, H / 2);
      return;
    }
    const vals = this.points.map((p) => p[1]);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.1 || 1;
    const yLo = lo - pad, yHi = hi + pad;
    const x = (i) => 4 + i * (W - 8) / (this.points.length - 1);
    const y = (v) => H - 6 - ((v - yLo) / (yHi - yLo)) * (H - 12);
    // baseline = start balance
    const y0 = y(vals[0]);
    ctx.strokeStyle = "rgba(255,255,255,0.15)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(0, y0); ctx.lineTo(W, y0); ctx.stroke();
    ctx.setLineDash([]);
    // fill under curve
    ctx.beginPath();
    this.points.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1]))));
    ctx.lineTo(x(this.points.length - 1), H); ctx.lineTo(x(0), H); ctx.closePath();
    ctx.fillStyle = "rgba(57,255,94,0.08)";
    ctx.fill();
    // curve
    ctx.strokeStyle = vals[vals.length - 1] >= vals[0] ? "#39ff5e" : "#ff3860";
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    this.points.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1]))));
    ctx.stroke();
    ctx.lineWidth = 1;
  }
}

/* ---------- Phase-2: Monte Carlo band fan ---------- */
class NeonBands {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.bands = [];             // [{t, p05, p50, p95}, ...]
    this.start = 0;
  }
  setData(bands, start) { this.bands = bands || []; this.start = start || 0; this.draw(); }
  draw() {
    const { ctx, canvas } = this;
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (this.bands.length < 2) {
      ctx.fillStyle = "rgba(160,170,190,0.5)";
      ctx.font = "11px monospace";
      ctx.fillText("awaiting simulation…", 12, H / 2);
      return;
    }
    const all = this.bands.flatMap((b) => [b.p05, b.p95]);
    const lo = Math.min(...all), hi = Math.max(...all);
    const pad = (hi - lo) * 0.08 || 1;
    const yLo = lo - pad, yHi = hi + pad;
    const x = (i) => 4 + i * (W - 8) / (this.bands.length - 1);
    const y = (v) => H - 6 - ((v - yLo) / (yHi - yLo)) * (H - 12);
    // ruin-ish shading below start
    if (this.start > 0) {
      const y0 = y(this.start);
      ctx.fillStyle = "rgba(255,56,96,0.05)";
      ctx.fillRect(0, y0, W, H - y0);
    }
    // p05-p95 envelope
    ctx.beginPath();
    this.bands.forEach((b, i) => (i ? ctx.lineTo(x(i), y(b.p95)) : ctx.moveTo(x(i), y(b.p95))));
    for (let i = this.bands.length - 1; i >= 0; i--) ctx.lineTo(x(i), y(this.bands[i].p05));
    ctx.closePath();
    ctx.fillStyle = "rgba(0,255,249,0.10)";
    ctx.fill();
    ctx.strokeStyle = "rgba(0,255,249,0.35)";
    ctx.beginPath();
    this.bands.forEach((b, i) => (i ? ctx.lineTo(x(i), y(b.p95)) : ctx.moveTo(x(i), y(b.p95))));
    ctx.stroke();
    ctx.beginPath();
    this.bands.forEach((b, i) => (i ? ctx.lineTo(x(i), y(b.p05)) : ctx.moveTo(x(i), y(b.p05))));
    ctx.stroke();
    // median
    ctx.strokeStyle = "#ffe600";
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    this.bands.forEach((b, i) => (i ? ctx.lineTo(x(i), y(b.p50)) : ctx.moveTo(x(i), y(b.p50))));
    ctx.stroke();
    ctx.lineWidth = 1;
  }
}

window.NeonChart = NeonChart;
window.NeonLine = NeonLine;
window.NeonBands = NeonBands;
