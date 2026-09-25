"""GUI panels: dashboard, trader, strategies, risk, lab, settings, connection."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Any, Callable, Dict, List, Optional

from ..constants import Side
from ..utils.mathx import clamp
from .chart import CandleChart
from .pairing import (
    DEFAULT_CDP_PORT, DEFAULT_PROFILE, DEFAULT_TIMEOUT, pairing_form,
)
from .theme import MONO, MONO_BOLD, MONO_SMALL, BIG_NUM, Theme
from .widgets import DataTable, Gauge, LogConsole, Meter, NeonButton, NeonPanel, StatBox

# glyphs the panel draws (kept as names so the source stays ASCII-clean)
BULLET = "\u2022"
PLAY = "\u25b6"
FLAG = "\u2691"
WARN = "\u26a0"
ELLIPSIS = "\u2026"


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
        from ..constants import DEFAULT_ASSETS

        self.asset_menu = tk.OptionMenu(row, self.asset_var, *DEFAULT_ASSETS)
        self.asset_menu.config(bg="#0a0c18", fg=theme["text"], font=MONO,
                               width=14, highlightthickness=0)
        try:
            self.asset_menu["menu"].config(bg="#0a0c18", fg=theme["text"],
                                           font=MONO)
        except Exception:  # noqa: BLE001 — headless shim has no menu theme
            pass
        self.asset_menu.pack(side="left", padx=6)
        self._menu_names = tuple(DEFAULT_ASSETS)
        self._charted = ""
        self._updates = 0
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

    def _rebuild_menu(self, names: List[str]) -> None:
        """Refresh the asset dropdown when the venue catalog changes."""
        try:
            menu = self.asset_menu["menu"]
            menu.delete(0, "end")
            for name in names:
                menu.add_command(label=name,
                                 command=lambda v=name: self.asset_var.set(v))
        except Exception:  # noqa: BLE001 — a stale menu never breaks trading
            pass
        self._menu_names = tuple(names)

    def _fetch_candles(self, asset: str) -> List[Dict[str, Any]]:
        """On-demand chart payload for assets outside the snapshot window."""
        try:
            res = self.command_cb({"cmd": "candles", "asset": asset})
        except Exception:  # noqa: BLE001
            return []
        if isinstance(res, dict):
            return list(res.get("candles") or [])
        return []

    def update_state(self, state: Dict[str, Any]) -> None:
        catalog = (state.get("catalog") or {}).get("rows", [])
        names = [r.get("name", "") for r in catalog if r.get("name")]
        pool = names or list(state.get("assets", []))
        if names and tuple(names) != self._menu_names:
            self._rebuild_menu(names)
        current = self.asset_var.get()
        asset = current if current in pool else (pool[0] if pool else "")
        self._updates += 1
        candles = (state.get("candles") or {}).get(asset, [])
        if not candles and asset and (asset != self._charted
                                      or self._updates % 30 == 0):
            candles = self._fetch_candles(asset)
        self._charted = asset
        self.chart.set_data(
            [{"o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in candles[-160:]],
            asset=asset,
        )
        info = ""
        for row in catalog:
            if row.get("name") == asset:
                payout = float(row.get("payout") or 0.0) * 100.0
                flag = "OPEN" if row.get("open") else "SHUT"
                info = f" · {asset} {payout:.0f}% {flag}"
                break
        health = state.get("health", {})
        self.status.config(
            text=f"◈ {health.get('engine_state', '?').upper()} · posture {health.get('posture', '-')} · "
                 f"signals {health.get('signals_total', 0)} · vetoes {health.get('vetoes', 0)}"
                 f"{info}"
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
    """Venue session surface — Quotex pairing (the only venue).

    Two ways in: paste an SSID you already hold, or let Chrome do the login
    (``pair_session``) and read the cookie it hands back. Both end at the same
    ``connect_cb`` with a purse, because both end at the same live wire.
    """

    def __init__(
        self,
        master,
        theme: Theme,
        connect_cb: Callable[[dict], Any],
        login_cb: Optional[Callable[[dict], Any]] = None,
        **kw,
    ) -> None:
        super().__init__(master, bg=theme["bg"], **kw)
        self.theme = theme
        self.connect_cb = connect_cb
        self.login_cb = login_cb
        self._pairing = False
        form = tk.Frame(self, bg=theme["bg"])
        form.pack(fill="both", expand=True, padx=14, pady=10)

        tk.Label(form, text="VENUE", bg=theme["bg"], fg=theme["dim"],
                 font=MONO_SMALL).grid(row=0, column=0, sticky="w")
        tk.Label(form, text="QUOTEX (LIVE ONLY)", bg=theme["bg"], fg=theme["red"],
                 font=MONO_BOLD).grid(row=0, column=1, sticky="w")

        self.ssid_var = tk.StringVar()
        tk.Label(form, text="QUOTEX SSID", bg=theme["bg"], fg=theme["dim"],
                 font=MONO_SMALL).grid(row=1, column=0, sticky="w", pady=6)
        tk.Entry(form, textvariable=self.ssid_var, width=46, font=MONO,
                 show=BULLET, bg="#0a0c18", fg=theme["text"],
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

        buttons = tk.Frame(form, bg=theme["bg"])
        buttons.grid(row=4, column=1, sticky="w", pady=14)
        self.connect_btn = NeonButton(buttons, theme, PLAY + " CONNECT",
                                      color=theme["green"], command=self._connect,
                                      width=150)
        self.connect_btn.pack(side="left", padx=(0, 10))
        self.login_btn = NeonButton(buttons, theme, FLAG + " CHROME LOGIN",
                                    color=theme["yellow"], command=self._login,
                                    width=190)
        self.login_btn.pack(side="left")
        if self.login_cb is None:      # no pairing wired — hide the button
            self.login_btn.pack_forget()

        self.status = tk.Label(form, text="disconnected", bg=theme["bg"],
                               fg=theme["dim"], font=MONO_SMALL)
        self.status.grid(row=5, column=1, sticky="w")

        # -- Chrome pairing: profile dir, DevTools port, wait budget --------
        tk.Label(form, text="CHROME PAIRING", bg=theme["bg"], fg=theme["yellow"],
                 font=MONO_SMALL).grid(row=6, column=0, sticky="w", pady=(14, 2))
        pair = tk.Frame(form, bg=theme["bg"])
        pair.grid(row=6, column=1, sticky="w", pady=(14, 2))
        tk.Label(pair, text="profile", bg=theme["bg"], fg=theme["dim"],
                 font=MONO_SMALL).pack(side="left")
        self.profile_var = tk.StringVar(value=DEFAULT_PROFILE)
        tk.Entry(pair, textvariable=self.profile_var, width=22, font=MONO,
                 bg="#0a0c18", fg=theme["text"],
                 insertbackground=theme["cyan"]).pack(side="left", padx=4)
        tk.Label(pair, text="cdp port", bg=theme["bg"], fg=theme["dim"],
                 font=MONO_SMALL).pack(side="left", padx=(10, 0))
        self.port_var = tk.StringVar(value=str(DEFAULT_CDP_PORT))
        tk.Entry(pair, textvariable=self.port_var, width=7, font=MONO,
                 bg="#0a0c18", fg=theme["text"],
                 insertbackground=theme["cyan"]).pack(side="left", padx=4)
        tk.Label(pair, text="wait s", bg=theme["bg"], fg=theme["dim"],
                 font=MONO_SMALL).pack(side="left", padx=(10, 0))
        self.timeout_var = tk.StringVar(value=str(DEFAULT_TIMEOUT))
        tk.Entry(pair, textvariable=self.timeout_var, width=6, font=MONO,
                 bg="#0a0c18", fg=theme["text"],
                 insertbackground=theme["cyan"]).pack(side="left", padx=4)

        self.hint = tk.Label(
            form,
            text="Chrome opens qxbroker.com in that profile — sign in and solve\n"
                 "the CAPTCHA yourself; we only read the session cookie\n"
                 "Chrome grants over localhost DevTools. Nothing is bypassed.",
            bg=theme["bg"], fg=theme["dim"], font=MONO_SMALL, justify="left",
        )
        self.hint.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 8))

        self.warn = tk.Label(
            form,
            text=WARN + " Unofficial integration. Automation may violate Quotex ToS.\n"
                 "LIVE ONLY build — orders are real. No system survives every "
                 "market condition — see DISCLAIMER.md.",
            bg=theme["bg"], fg=theme["yellow"], font=MONO_SMALL, justify="left",
        )
        self.warn.grid(row=8, column=0, columnspan=2, sticky="w", pady=10)

    # -- actions ----------------------------------------------------------
    def _purse(self) -> Optional[bool]:
        purse = self.purse.get()
        if purse not in ("practice", "real"):
            self.set_status("pick a purse: PRACTICE or REAL MONEY", "yellow")
            return None
        return purse == "practice"

    def _connect(self) -> None:
        if self._pairing:
            self.set_status("a Chrome pairing is already running", "yellow")
            return
        purse = self._purse()
        if purse is None:
            return
        self.connect_cb({
            "mode": "quotex",
            "ssid": self.ssid_var.get().strip(),
            "demo": purse,
        })

    def _login(self) -> None:
        """Chrome-assisted pairing — the human solves the CAPTCHA."""
        if self._pairing:
            self.set_status("a Chrome pairing is already running", "yellow")
            return
        purse = self._purse()
        if purse is None:
            return
        if self.login_cb is None:
            self.set_status("Chrome pairing unavailable in this build", "red")
            return
        ok, error, values = pairing_form(
            profile=self.profile_var.get(),
            port=self.port_var.get(),
            timeout=self.timeout_var.get(),
        )
        if not ok:
            self.set_status(error, "yellow")
            return
        self.set_busy(True, "launching Chrome — log in and solve the CAPTCHA…")
        self.login_cb({
            "mode": "quotex",
            "demo": purse,
            "profile": values["profile"],
            "port": values["port"],
            "timeout": values["timeout"],
        })

    # -- views ------------------------------------------------------------
    def set_busy(self, busy: bool, message: str = "") -> None:
        """Toggle the pairing state so a blocked UI never looks frozen."""
        self._pairing = busy
        self.login_btn.set_label(ELLIPSIS + " PAIRING" if busy
                                 else FLAG + " CHROME LOGIN")
        self.login_btn.set_color(self.theme["dim"] if busy else self.theme["yellow"])
        self.connect_btn.set_color(self.theme["green"] if not busy
                                   else self.theme["dim"])
        if message:
            self.set_status(message, "yellow" if busy else "dim")

    def adopt_session(self, ssid: str, label: str = "session captured") -> None:
        """Fill the SSID field with a freshly paired cookie.

        Adopting a session ends the pairing by definition, so the busy state
        is cleared here too — a caller cannot forget to.
        """
        self.set_busy(False)
        self.ssid_var.set(ssid)
        self.set_status(label, "green")

    def set_status(self, text: str, color: Optional[str] = None) -> None:
        self.status.config(text=text, fg=self.theme[color or "dim"])


# asset-board row marks (kept as names so the source stays ASCII-clean)
TRI = "\u25b8"  # active asset on the TRADE tab
DOT = "\u25cf"  # engine-tracked asset
IDLE = "\u00b7"  # listed but idle
DASH = "\u2014"  # missing payout / price


class AssetsPanel(tk.Frame):
    """Scrollable asset board: every catalog symbol at a glance.

    One row per asset — name, payout %, OPEN/SHUT flag, last price.
    A search box plus a kind filter narrow the 200-strong list;
    clicking a row selects that asset on the TRADE tab (charts it).
    """

    _ROW = "{mark} {name:<12} {pay:>5} {flag:<4} {px:>11}"

    def __init__(self, master, theme: Theme, select_cb: Callable[[str], Any],
                 **kw) -> None:
        super().__init__(master, bg=theme["bg"], **kw)
        self.theme = theme
        self._select_cb = select_cb
        self._shown: List[Dict[str, Any]] = []
        self._lines: List[str] = []
        self._kinds = ["ALL"]
        self.search_var = tk.StringVar(value="")
        self.kind_var = tk.StringVar(value="ALL")

        self.head = tk.Label(self, text="ASSETS", bg=theme["bg"],
                             fg=theme["cyan"], font=MONO_BOLD, anchor="w")
        self.head.pack(fill="x", padx=8, pady=(6, 2))

        bar = tk.Frame(self, bg=theme["bg"])
        bar.pack(fill="x", padx=8, pady=2)
        tk.Label(bar, text="FIND", bg=theme["bg"], fg=theme["dim"],
                 font=MONO_SMALL).pack(side="left")
        tk.Entry(bar, textvariable=self.search_var, width=16, font=MONO,
                 bg="#0a0c18", fg=theme["text"],
                 insertbackground=theme["cyan"]).pack(side="left", padx=6)
        tk.Label(bar, text="KIND", bg=theme["bg"], fg=theme["dim"],
                 font=MONO_SMALL).pack(side="left")
        self.kind_menu = tk.OptionMenu(bar, self.kind_var, *self._kinds)
        self.kind_menu.config(bg="#0a0c18", fg=theme["text"], font=MONO,
                              width=10, highlightthickness=0)
        try:
            self.kind_menu["menu"].config(bg="#0a0c18", fg=theme["text"],
                                         font=MONO)
        except Exception:  # noqa: BLE001 — headless shim has no menu theme
            pass
        self.kind_menu.pack(side="left", padx=6)

        body = tk.Frame(self, bg=theme["bg"])
        body.pack(fill="both", expand=True, padx=8, pady=4)
        self.scroll = tk.Scrollbar(body, orient=tk.VERTICAL)
        self.board = tk.Listbox(body, font=MONO, bg="#0a0c18",
                                fg=theme["text"], selectbackground=theme["cyan"],
                                selectforeground="#04060f", activestyle="none",
                                yscrollcommand=self.scroll.set)
        self.scroll.config(command=self.board.yview)
        self.scroll.pack(side="right", fill="y")
        self.board.pack(side="left", fill="both", expand=True)
        self.board.bind("<<ListboxSelect>>", self._on_pick)

        self.info = tk.Label(self, text="", bg=theme["bg"], fg=theme["dim"],
                             font=MONO_SMALL, anchor="w")
        self.info.pack(fill="x", padx=8, pady=(0, 6))

    @staticmethod
    def _format(row: Dict[str, Any], active: str,
                tracked: set) -> str:
        name = str(row.get("name", "?"))
        pay = row.get("payout")
        pay_s = f"{float(pay) * 100.0:.0f}%" if isinstance(pay, (int, float)) else DASH
        flag = "OPEN" if row.get("open") else "SHUT"
        px = row.get("price")
        px_s = f"{float(px):.5f}" if isinstance(px, (int, float)) else DASH
        mark = TRI if name == active else (DOT if name in tracked else IDLE)
        return AssetsPanel._ROW.format(mark=mark, name=name[:12], pay=pay_s,
                                       flag=flag, px=px_s)

    def _selected_name(self) -> str:
        sel = self.board.curselection()
        if sel and 0 <= int(sel[0]) < len(self._shown):
            return str(self._shown[int(sel[0])].get("name", ""))
        return ""

    def _rebuild_kind_menu(self) -> None:
        try:
            menu = self.kind_menu["menu"]
            menu.delete(0, "end")
            for kind in self._kinds:
                menu.add_command(label=kind,
                                 command=lambda v=kind: self.kind_var.set(v))
        except Exception:  # noqa: BLE001 — a stale menu never breaks the board
            pass

    def _refresh_info(self) -> None:
        name = self._selected_name()
        if not name:
            self.info.config(text="")
            return
        row = next((r for r in self._shown
                    if str(r.get("name")) == name), {})
        pay = row.get("payout")
        pay_s = f"{float(pay) * 100.0:.0f}%" if isinstance(pay, (int, float)) else DASH
        flag = "OPEN" if row.get("open") else "SHUT"
        px = row.get("price")
        px_s = f"{float(px):.5f}" if isinstance(px, (int, float)) else DASH
        kind = str(row.get("kind") or "OTHER")
        self.info.config(text=f"{name} {TRI} {kind} {TRI} payout {pay_s} "
                              f"{TRI} {flag} {TRI} {px_s}")

    def _on_pick(self, _event: Any = None) -> None:
        name = self._selected_name()
        if name:
            self._select_cb(name)
            self._refresh_info()

    def update_state(self, state: Dict[str, Any]) -> None:
        cat = state.get("catalog") or {}
        rows = [r for r in (cat.get("rows") or []) if r.get("name")]
        active = str(state.get("asset") or "")
        tracked = set(state.get("watch") or [])
        kinds = ["ALL", *sorted({str(r.get("kind") or "OTHER") for r in rows})]
        if kinds != self._kinds:
            self._kinds = kinds
            self._rebuild_kind_menu()
            if self.kind_var.get() not in self._kinds:
                self.kind_var.set("ALL")
        want = self.search_var.get().strip().lower()
        kind = self.kind_var.get()
        shown = [r for r in rows
                 if (kind == "ALL" or str(r.get("kind") or "OTHER") == kind)
                 and (not want or want in str(r.get("name", "")).lower())]
        lines = [self._format(r, active, tracked) for r in shown]
        if lines != self._lines:
            # Prices tick every poll, so the board rebuilds often — keep the
            # user's row selected across rebuilds by asset name.
            sel = self._selected_name()
            self.board.delete(0, tk.END)
            for line in lines:
                self.board.insert(tk.END, line)
            self._lines = lines
            self._shown = shown
            if sel:
                for i, r in enumerate(shown):
                    if str(r.get("name")) == sel:
                        try:
                            self.board.selection_clear(0, tk.END)
                            self.board.selection_set(i)
                        except Exception:  # noqa: BLE001 — cosmetic only
                            pass
                        break
        else:
            self._shown = shown
        n_open = sum(1 for r in rows if r.get("open"))
        live = "LIVE" if cat.get("live") else "STATIC"
        self.head.config(text=f"ASSETS {TRI} {len(shown)}/{len(rows)} shown "
                              f"{TRI} {n_open} open {TRI} {live}")
        self._refresh_info()


__all__ = [
    "AssetsPanel",
    "DashboardPanel",
    "TraderPanel",
    "StrategiesPanel",
    "RiskPanel",
    "RiskLabPanel",
    "SettingsPanel",
    "ConnectionPanel",
]
