"""GUI panels: dashboard, trader, strategies, risk, lab, settings, connection."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Any, Callable, Dict, List, Optional

from ..constants import Side
from ..utils.mathx import clamp
from .chart import CandleChart
from .theme import MONO, MONO_BOLD, MONO_SMALL, BIG_NUM, Theme
from .widgets import DataTable, Gauge, LogConsole, Meter, NeonButton, NeonPanel, StatBox


def _reflow(parent, widgets, per_row: int, bg: str) -> None:
    """Phase-31: re-pack ``widgets`` into wrapped rows of ``per_row``.

    Previous generated row frames are destroyed first so repeated layout
    passes (window resizes) never stack ghost rows.
    """
    for frame in getattr(parent, "_reflow_frames", []):
        try:
            frame.destroy()
        except Exception:  # noqa: BLE001
            pass
    frames = []
    row = None
    per = max(1, int(per_row))
    for i, w in enumerate(widgets):
        if i % per == 0:
            row = tk.Frame(parent, bg=bg)
            row.pack(fill="x", padx=2, pady=2)
            frames.append(row)
        try:
            w.pack_forget()
        except Exception:  # noqa: BLE001
            pass
        w.pack(in_=row, side="left", padx=8, pady=2)
    parent._reflow_frames = frames


class DashboardPanel(tk.Frame):
    """HUD: account core, meters, gauges, survivor ladder, recent trades."""

    def __init__(self, master, theme: Theme, **kw) -> None:
        super().__init__(master, bg=theme["bg"], **kw)
        self.theme = theme

        top = tk.Frame(self, bg=theme["bg"])
        top.pack(fill="x", padx=6, pady=4)
        self.balance = StatBox(top, theme, "balance", color="cyan"); self.balance.pack(side="left", padx=10)
        self.daily = StatBox(top, theme, "p/l today"); self.daily.pack(side="left", padx=10)
        self.winrate = StatBox(top, theme, "win rate", color="green"); self.winrate.pack(side="left", padx=10)
        self.trades = StatBox(top, theme, "trades"); self.trades.pack(side="left", padx=10)
        self.posture = StatBox(top, theme, "posture", color="yellow"); self.posture.pack(side="left", padx=10)
        self.regime = StatBox(top, theme, "regime", color="magenta"); self.regime.pack(side="left", padx=10)
        self._stat_parent = top
        self._stats = [self.balance, self.daily, self.winrate,
                       self.trades, self.posture, self.regime]

        mid = tk.Frame(self, bg=theme["bg"])
        mid.pack(fill="x", padx=6, pady=4)
        self.dd = Meter(mid, theme, "drawdown", color="magenta", width=340)
        self.dl = Meter(mid, theme, "daily loss", color="yellow", width=340)
        self.exp = Meter(mid, theme, "exposure", color="cyan", width=340)
        for m in (self.dd, self.dl, self.exp):
            m.pack(anchor="w", pady=2)

        gauges = tk.Frame(self, bg=theme["bg"])
        gauges.pack(fill="x", padx=6, pady=4)
        self.g_wr = Gauge(gauges, theme, "win rate", "green")
        self.g_stress = Gauge(gauges, theme, "stress", "red")
        self.g_dd = Gauge(gauges, theme, "drawdown", "magenta")
        self._gauges = (self.g_wr, self.g_stress, self.g_dd)
        for g in self._gauges:
            g.pack(side="left", padx=18)

        self.ladder = tk.Frame(self, bg=theme["bg"])
        self.ladder.pack(fill="x", padx=6, pady=4)
        self.rungs: Dict[str, tk.Label] = {}
        for name in ("ATTACK", "NORMAL", "GUARD", "DEFENSE", "LOCKDOWN"):
            lbl = tk.Label(self.ladder, text=name, width=12, font=MONO_BOLD,
                           bg=theme["bg2"], fg=theme["dim"], bd=1, relief="groove")
            lbl.pack(side="left", padx=4)
            self.rungs[name] = lbl
        self._ladder_widgets = list(self.rungs.values())

        self.blotter = DataTable(
            self, theme,
            ["ts", "asset", "side", "stake", "strike", "result", "p/l"],
            height=10,
        )
        self.blotter.pack(fill="both", expand=True, padx=6, pady=4)

    def apply_layout(self, plan) -> None:
        """Phase-31: resolution-aware placement of stats/meters/gauges."""
        _reflow(self._stat_parent, self._stats, plan.stat_cols, self.theme["bg"])
        for m in (self.dd, self.dl, self.exp):
            m.resize(plan.meter_width)
        for g in self._gauges:
            g.resize(plan.gauge_size)
        _reflow(self.ladder, self._ladder_widgets, plan.ladder_per_row,
                self.theme["bg"])
        self.blotter.set_height(plan.blotter_rows)

    def update_state(self, state: Dict[str, Any]) -> None:
        t = self.theme
        health = state.get("health", {})
        acct = state.get("account", {})
        risk = state.get("risk", {})
        limits = risk.get("limits", {})

        self.balance.set(f"{acct.get('balance', 0):.2f}", "cyan")
        daily = acct.get("daily_pnl", 0.0)
        self.daily.set(f"{daily:+.2f}", "green" if daily >= 0 else "red")
        wr = health.get("win_rate", 0.0)
        self.winrate.set(f"{wr * 100:.1f}%", "green")
        self.trades.set(str(health.get("trades_total", 0)))
        self.posture.set(str(health.get("posture", "NORMAL")), "yellow")
        self.regime.set(str(health.get("regime", "?")), "magenta")

        dd = acct.get("drawdown", 0.0)
        self.dd.set(dd * 100, (limits.get("max_total_drawdown_frac", 0.2)) * 100)
        dl = health.get("daily_loss_frac", 0.0)
        self.dl.set(dl * 100, (limits.get("max_daily_loss_frac", 0.08)) * 100)
        open_n = acct.get("open_positions", 0)
        self.exp.set(open_n, max(1, limits.get("max_concurrent", 3)))

        self.g_wr.set(wr, f"{wr * 100:.0f}%")
        stress = 0.0
        regimes = state.get("regimes") or {}
        if regimes:
            first = next(iter(regimes.values()))
            stress = float(first.get("stress", 0.0))
        self.g_stress.set(stress, f"{stress:.2f}")
        self.g_dd.set(dd, f"{dd * 100:.1f}%")

        posture = str(health.get("posture", "NORMAL")).upper()
        palette_for = {
            "ATTACK": "cyan", "NORMAL": "green", "GUARD": "yellow",
            "DEFENSE": "magenta", "LOCKDOWN": "red",
        }
        for name, lbl in self.rungs.items():
            if name == posture:
                color = palette_for[name]
                lbl.config(bg=t[color], fg=t["bg"], relief="sunken")
            else:
                lbl.config(bg=t["bg2"], fg=t["dim"], relief="groove")

        rows = []
        for tr in state.get("trades", [])[-25:]:
            ts = tr.get("ts", 0)
            rows.append((
                f"{int(ts) % 86400 // 3600:02d}:{int(ts) % 3600 // 60:02d}:{int(ts) % 60:02d}",
                str(tr.get("asset", "")),
                str(tr.get("side", "")),
                f"{tr.get('stake', 0):.1f}",
                f"{tr.get('strike', 0):.5f}",
                "REFUND" if tr.get("refunded") else ("WON" if tr.get("won") else "LOST"),
                f"{tr.get('pnl', 0):+.2f}",
            ))

        def tag(row):
            return "refund" if row[5] == "REFUND" else ("won" if row[5] == "WON" else "lost")

        self.blotter.rows(rows[-12:], tag_fn=tag)


class TraderPanel(tk.Frame):
    """Chart + manual order ticket + fire control."""

    def __init__(self, master, theme: Theme, command_cb: Callable[[dict], Any], **kw) -> None:
        super().__init__(master, bg=theme["bg"], **kw)
        self.theme = theme
        self.command_cb = command_cb
        self.chart = CandleChart(self, theme)
        self.chart.pack(fill="both", expand=True, padx=6, pady=4)

        row = tk.Frame(self, bg=theme["bg"])
        row.pack(fill="x", padx=6, pady=4)
        self.asset_var = tk.StringVar(value="EURUSD_otc")
        tk.Label(row, text="ASSET", bg=theme["bg"], fg=theme["dim"], font=MONO_SMALL).pack(side="left")
        tk.Entry(row, textvariable=self.asset_var, width=14, font=MONO,
                 bg="#0a0c18", fg=theme["text"], insertbackground=theme["cyan"]).pack(side="left", padx=6)
        self.stake_var = tk.StringVar(value="5")
        tk.Label(row, text="STAKE", bg=theme["bg"], fg=theme["dim"], font=MONO_SMALL).pack(side="left")
        tk.Entry(row, textvariable=self.stake_var, width=6, font=MONO,
                 bg="#0a0c18", fg=theme["text"], insertbackground=theme["cyan"]).pack(side="left", padx=6)
        self.expiry_var = tk.StringVar(value="60")
        tk.Label(row, text="EXP", bg=theme["bg"], fg=theme["dim"], font=MONO_SMALL).pack(side="left")
        tk.Entry(row, textvariable=self.expiry_var, width=6, font=MONO,
                 bg="#0a0c18", fg=theme["text"], insertbackground=theme["cyan"]).pack(side="left", padx=6)

        self.call_btn = NeonButton(row, theme, "▲ CALL", color=theme["green"],
                                   command=lambda: self._trade("call"))
        self.call_btn.pack(side="left", padx=4)
        self.put_btn = NeonButton(row, theme, "▼ PUT", color=theme["red"],
                                  command=lambda: self._trade("put"))
        self.put_btn.pack(side="left", padx=4)

        row2 = tk.Frame(self, bg=theme["bg"])
        row2.pack(fill="x", padx=6, pady=2)
        self._action_row = row2
        self.fire_btns = []
        for label, color, cmd in (
            ("ARM ▶", "green", "arm"),
            ("DISARM", "cyan", "disarm"),
            ("KILL ✖", "red", "kill"),
            ("NEWS FLAG", "yellow", "news"),
            ("LOCKDOWN", "magenta", "lockdown"),
        ):
            btn = NeonButton(row2, theme, label, color=theme[color],
                             width=110,
                             command=lambda c=cmd: self.command_cb({"cmd": c}))
            btn.pack(side="left", padx=4)
            self.fire_btns.append(btn)

        self.status = tk.Label(self, text="◈ LIVE — real order flow",
                               bg=theme["bg"], fg=theme["red"],
                               font=MONO_SMALL, anchor="w")
        self.status.pack(fill="x", padx=8)

    def apply_layout(self, plan) -> None:
        """Phase-31: fire-control buttons re-wrap for the window class."""
        for b in (self.call_btn, self.put_btn, *self.fire_btns):
            b.resize(plan.button_w, plan.button_h)
        _reflow(self._action_row, self.fire_btns, plan.fire_per_row,
                self.theme["bg"])

    def _trade(self, side: str) -> None:
        self.command_cb({
            "cmd": "trade",
            "side": side,
            "asset": self.asset_var.get().strip(),
            "amount": float(self.stake_var.get() or 5),
            "expiry": int(self.expiry_var.get() or 60),
        })

    def update_state(self, state: Dict[str, Any]) -> None:
        assets = state.get("assets", [])
        asset = self.asset_var.get() if self.asset_var.get() in assets else (assets[0] if assets else "")
        candles = (state.get("candles") or {}).get(asset, [])
        self.chart.set_data(
            [{"o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in candles[-160:]],
            asset=asset,
        )
        health = state.get("health", {})
        self.status.config(
            text=f"◈ {health.get('engine_state', '?').upper()} · posture {health.get('posture', '-')} · "
                 f"signals {health.get('signals_total', 0)} · vetoes {health.get('vetoes', 0)}"
        )


class StrategiesPanel(tk.Frame):
    """Strategy registry table with live performance + ensemble weights."""

    def __init__(self, master, theme: Theme, **kw) -> None:
        super().__init__(master, bg=theme["bg"], **kw)
        self.theme = theme
        self.table = DataTable(
            self, theme, ["strategy", "family", "wr", "n", "pnl", "weight"], height=22
        )
        self.table.pack(fill="both", expand=True, padx=6, pady=4)

    def apply_layout(self, plan) -> None:
        """Phase-31: deck table grows with real estate."""
        self.table.set_height(max(10, plan.blotter_rows + 6))

    def update_state(self, state: Dict[str, Any]) -> None:
        strat = state.get("strategies") or state.get("snapshot", {}).get("strategies", {})
        members = strat.get("members", [])
        weights = strat.get("weights", {})
        rows = []
        for m in members:
            rows.append((
                str(m.get("name", "")),
                str(m.get("family", "")),
                f"{float(m.get('win_rate', 0)) * 100:.0f}%",
                str(m.get("attempts", 0)),
                f"{float(m.get('pnl', 0)):+.1f}",
                f"{weights.get(m.get('name', ''), 0):.2f}",
            ))
        self.table.rows(rows)


class RiskPanel(tk.Frame):
    """Risk limits, rejection feed, survivor configuration mirror."""

    def __init__(self, master, theme: Theme, **kw) -> None:
        super().__init__(master, bg=theme["bg"], **kw)
        self.theme = theme
        self.meters = tk.Frame(self, bg=theme["bg"])
        self.meters.pack(fill="x", padx=6, pady=4)
        self.m_dd = Meter(self.meters, theme, "total dd", "magenta", 360)
        self.m_dl = Meter(self.meters, theme, "daily loss", "yellow", 360)
        self.m_rate = Meter(self.meters, theme, "day trades", "cyan", 360)
        for m in (self.m_dd, self.m_dl, self.m_rate):
            m.pack(anchor="w", pady=2)
        self.console = LogConsole(self, theme, height=20)
        self.console.pack(fill="both", expand=True, padx=6, pady=4)

    def apply_layout(self, plan) -> None:
        """Phase-31: risk meters and console track the plan."""
        for m in (self.m_dd, self.m_dl, self.m_rate):
            m.resize(plan.meter_width)
        self.console.set_height(plan.console_lines)

    def update_state(self, state: Dict[str, Any]) -> None:
        risk = state.get("risk", {})
        snap = risk.get("state", {})
        limits = risk.get("limits", {})
        self.m_dd.set(risk.get("total_drawdown", 0) * 100,
                      (limits.get("max_total_drawdown_frac", 0.2)) * 100)
        self.m_dl.set(risk.get("daily_loss_frac", 0) * 100,
                      (limits.get("max_daily_loss_frac", 0.08)) * 100)
        self.m_rate.set(snap.get("trades_today", 0),
                        max(1, limits.get("max_trades_per_day", 80)))
        for rej in risk.get("recent_rejections", []):
            check = rej.get("check", {})
            self.console.append(
                f"[reject] {check.get('name')} :: {check.get('message')}", "WARNING"
            )


class RiskLabPanel(tk.Frame):
    """Monte Carlo risk lab over the engine's OWN settled trades."""

    def __init__(self, master, theme: Theme, run_cb: Callable[[], str],
                 mc_cb: Optional[Callable[[], str]] = None, **kw) -> None:
        super().__init__(master, bg=theme["bg"], **kw)
        self.theme = theme
        row = tk.Frame(self, bg=theme["bg"])
        row.pack(fill="x", padx=6, pady=4)
        NeonButton(row, theme, "â MC LAB", color=theme["magenta"],
                   command=self._mc, width=140).pack(side="left", padx=4)
        self.run_cb = run_cb
        self.mc_cb = mc_cb
        self.out = LogConsole(self, theme, height=24)
        self.out.pack(fill="both", expand=True, padx=6, pady=4)
        self.out.append(
            "risk lab bootstraps the settled-trade ledger only — no invented "
            "samples, no simulated market.", "INFO")

    def apply_layout(self, plan) -> None:
        """Phase-31: lab console scales with the plan."""
        self.out.set_height(max(12, plan.console_lines))

    def _mc(self) -> None:
        """Monte Carlo risk-lab summary from settled trades."""
        if self.mc_cb is None:
            self.out.append("MC lab unavailable in this view", "WARN")
            return
        self.out.append("â bootstrapping Monte Carlo risk lab", "INFO")
        try:
            text = self.mc_cb()
            for line in text.splitlines():
                self.out.append(line, "INFO")
        except Exception as exc:  # noqa: BLE001
            self.out.append(f"MC lab failed: {exc}", "ERROR")


class SettingsPanel(tk.Frame):
    """Configuration surface — risk, strategy, survivor, display."""

    def __init__(self, master, theme: Theme, save_cb: Callable[[dict], Any], **kw) -> None:
        super().__init__(master, bg=theme["bg"], **kw)
        self.theme = theme
        self.save_cb = save_cb
        self.vars: Dict[str, tk.StringVar] = {}
        form = tk.Frame(self, bg=theme["bg"])
        form.pack(fill="both", expand=True, padx=10, pady=8)
        fields = [
            ("risk.stake_fraction", "stake fraction"),
            ("risk.max_stake", "max stake"),
            ("risk.max_concurrent", "max concurrent"),
            ("risk.max_daily_loss_frac", "daily loss cap"),
            ("risk.max_total_drawdown_frac", "total dd cap"),
            ("risk.min_payout", "min payout"),
            ("strategy.timeframe", "timeframe"),
            ("strategy.expiry_seconds", "expiry (s)"),
            ("strategy.min_confidence", "min confidence"),
            ("strategy.ensemble_mode", "ensemble mode"),
            ("survivor.spread_limit_mult", "spread limit x"),
            ("display.theme", "theme"),
        ]
        for i, (key, label) in enumerate(fields):
            tk.Label(form, text=label.upper(), bg=theme["bg"], fg=theme["dim"],
                     font=MONO_SMALL).grid(row=i // 2, column=(i % 2) * 2, sticky="w", pady=3)
            var = tk.StringVar()
            self.vars[key] = var
            tk.Entry(form, textvariable=var, width=16, font=MONO,
                     bg="#0a0c18", fg=theme["text"],
                     insertbackground=theme["cyan"]).grid(row=i // 2, column=(i % 2) * 2 + 1, pady=3, padx=6)
        NeonButton(form, theme, "◈ SAVE CONFIG", color=theme["magenta"],
                   command=self._save, width=200).grid(row=len(fields) // 2 + 1, column=0, pady=12)

    def load(self, cfg: Dict[str, Any]) -> None:
        for key, var in self.vars.items():
            section, name = key.split(".", 1)
            value = cfg.get(section, {}).get(name, "")
            var.set(str(value))

    def _save(self) -> None:
        out: Dict[str, Dict[str, str]] = {}
        for key, var in self.vars.items():
            section, name = key.split(".", 1)
            out.setdefault(section, {})[name] = var.get()
        self.save_cb(out)


class ConnectionPanel(tk.Frame):
    """Venue session surface — Quotex SSID pairing (the only venue)."""

    def __init__(self, master, theme: Theme, connect_cb: Callable[[dict], Any], **kw) -> None:
        super().__init__(master, bg=theme["bg"], **kw)
        self.theme = theme
        self.connect_cb = connect_cb
        form = tk.Frame(self, bg=theme["bg"])
        form.pack(fill="both", expand=True, padx=14, pady=10)

        tk.Label(form, text="VENUE", bg=theme["bg"], fg=theme["dim"],
                 font=MONO_SMALL).grid(row=0, column=0, sticky="w")
        tk.Label(form, text="QUOTEX (LIVE ONLY)", bg=theme["bg"], fg=theme["red"],
                 font=MONO_BOLD).grid(row=0, column=1, sticky="w")

        self.ssid_var = tk.StringVar()
        tk.Label(form, text="QUOTEX SSID", bg=theme["bg"], fg=theme["dim"],
                 font=MONO_SMALL).grid(row=1, column=0, sticky="w", pady=6)
        tk.Entry(form, textvariable=self.ssid_var, width=46, font=MONO, show="â¢",
                 bg="#0a0c18", fg=theme["text"],
                 insertbackground=theme["cyan"]).grid(row=1, column=1, pady=6)

        # the purse is never defaulted: "" until the operator picks one
        self.purse = tk.StringVar(value="")
        tk.Radiobutton(form, text="PRACTICE BALANCE", variable=self.purse,
                       value="practice", bg=theme["bg"], fg=theme["cyan"],
                       selectcolor=theme["bg2"], activebackground=theme["bg"],
                       font=MONO).grid(row=2, column=1, sticky="w")
        tk.Radiobutton(form, text="REAL MONEY", variable=self.purse,
                       value="real", bg=theme["bg"], fg=theme["red"],
                       selectcolor=theme["bg2"], activebackground=theme["bg"],
                       font=MONO).grid(row=3, column=1, sticky="w")

        NeonButton(form, theme, "â CONNECT", color=theme["green"],
                   command=self._connect, width=180).grid(row=4, column=1, pady=14)
        self.status = tk.Label(form, text="disconnected", bg=theme["bg"],
                               fg=theme["dim"], font=MONO_SMALL)
        self.status.grid(row=5, column=1, sticky="w")
        self.warn = tk.Label(
            form,
            text="â Unofficial integration. Automation may violate Quotex ToS.\n"
                 "LIVE ONLY build — orders are real. No system survives every "
                 "market condition — see DISCLAIMER.md.",
            bg=theme["bg"], fg=theme["yellow"], font=MONO_SMALL, justify="left",
        )
        self.warn.grid(row=6, column=0, columnspan=2, sticky="w", pady=18)

    def _connect(self) -> None:
        self.connect_cb({
            "mode": "quotex",
            "ssid": self.ssid_var.get().strip(),
            "demo": self.purse.get() == "practice",
        })

    def set_status(self, text: str, color: Optional[str] = None) -> None:
        self.status.config(text=text, fg=self.theme[color or "dim"])


__all__ = [
    "DashboardPanel",
    "TraderPanel",
    "StrategiesPanel",
    "RiskPanel",
    "RiskLabPanel",
    "SettingsPanel",
    "ConnectionPanel",
]
