"""CYBERTRADE desktop terminal — the Tkinter application shell."""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from typing import Any, Dict, Optional

from ..config import AppConfig
from ..events import Topic, default_bus
from .boot import BootScreen
from .panels import (
    BacktestPanel,
    ConnectionPanel,
    DashboardPanel,
    RiskPanel,
    SettingsPanel,
    StrategiesPanel,
    TraderPanel,
)
from .theme import MONO_BOLD, MONO_SMALL, Theme

log = logging.getLogger("cybertrade.gui")


class CybertradeApp(tk.Tk):
    """Main window: tabbed neon console bound to a live TradingEngine."""

    def __init__(self, engine, config: Optional[AppConfig] = None) -> None:
        super().__init__()
        self.engine = engine
        self.config = config or AppConfig()
        self.theme = Theme(self.config.display.theme)
        self.title("CYBERTRADE // NEON PROTOCOL")
        self.geometry("1280x800")
        self.configure(bg=self.theme["bg"])
        self._ui_queue: "queue.Queue" = queue.Queue(maxsize=500)

        self._build_header()
        self._build_body()
        self._build_footer()

        # boot screen overlay
        self.boot = BootScreen(self, self.theme, on_done=self._drop_boot)
        self.boot.place(relx=0, rely=0, relwidth=1, relheight=1)

        # telemetry marshaling (bus threads -> Tk main loop)
        default_bus.subscribe(Topic.LOG, self._bus_log)
        self._poll()

    # -- chrome ------------------------------------------------------------
    def _build_header(self) -> None:
        t = self.theme
        head = tk.Frame(self, bg=t["bg2"], height=54)
        head.pack(fill="x")
        tk.Label(head, text="◈ CYBERTRADE", bg=t["bg2"], fg=t["cyan"],
                 font=("Impact", 18)).pack(side="left", padx=10)
        tk.Label(head, text="// NEON PROTOCOL — ALL-WEATHER OPS",
                 bg=t["bg2"], fg=t["dim"], font=MONO_SMALL).pack(side="left")
        self.state_led = tk.Label(head, text="● DISARMED", bg=t["bg2"],
                                  fg=t["yellow"], font=MONO_BOLD)
        self.state_led.pack(side="right", padx=14)
        self.clock_led = tk.Label(head, text="--:--:--", bg=t["bg2"],
                                  fg=t["yellow"], font=MONO_BOLD)
        self.clock_led.pack(side="right", padx=14)

    def _build_body(self) -> None:
        t = self.theme
        self.tabs = tk.Frame(self, bg=t["bg"])
        self.tabs.pack(fill="both", expand=True)

        self.tab_buttons = tk.Frame(self.tabs, bg=t["bg"])
        self.tab_buttons.pack(fill="x", padx=6, pady=4)
        self._tab_names = ("DASH", "TRADE", "STRAT", "RISK", "TEST", "LINK", "CFG")
        self._tab_btns: Dict[str, tk.Label] = {}
        for name in self._tab_names:
            lbl = tk.Label(self.tab_buttons, text=name, width=8, font=MONO_BOLD,
                           bg=t["bg2"], fg=t["dim"], cursor="hand2", bd=1, relief="groove")
            lbl.pack(side="left", padx=3)
            lbl.bind("<Button-1>", lambda e, n=name: self.show_tab(n))
            self._tab_btns[name] = lbl

        self.panes = tk.Frame(self.tabs, bg=t["bg"])
        self.panes.pack(fill="both", expand=True)

        self.dash = DashboardPanel(self.panes, t)
        self.trader = TraderPanel(self.panes, t, command_cb=self._command)
        self.strat = StrategiesPanel(self.panes, t)
        self.risk_p = RiskPanel(self.panes, t)
        self.bt_p = BacktestPanel(self.panes, t, run_cb=self._run_backtest,
                                 mc_cb=self._mc_summary)
        self.link = ConnectionPanel(self.panes, t, connect_cb=self._connect)
        self.cfg_p = SettingsPanel(self.panes, t, save_cb=self._save_config)

        self._panes = {
            "DASH": self.dash, "TRADE": self.trader, "STRAT": self.strat,
            "RISK": self.risk_p, "TEST": self.bt_p, "LINK": self.link,
            "CFG": self.cfg_p,
        }
        self.show_tab("DASH")

    def _build_footer(self) -> None:
        t = self.theme
        foot = tk.Frame(self, bg=t["bg2"], height=28)
        foot.pack(fill="x", side="bottom")
        tk.Label(
            foot,
            text="PAPER BY DEFAULT · UNOFFICIAL QUOTEX BRIDGE · "
                 "NO SYSTEM SURVIVES EVERY MARKET · SEE DISCLAIMER.md",
            bg=t["bg2"], fg=t["yellow"], font=MONO_SMALL,
        ).pack(side="left", padx=10)
        self.status_led = tk.Label(foot, text="◈ ready", bg=t["bg2"],
                                   fg=t["dim"], font=MONO_SMALL)
        self.status_led.pack(side="right", padx=10)

    def show_tab(self, name: str) -> None:
        for widget in self.panes.winfo_children():
            widget.pack_forget()
        self._panes[name].pack(fill="both", expand=True)
        for n, lbl in self._tab_btns.items():
            if n == name:
                lbl.config(bg=self.theme["cyan"], fg=self.theme["bg"], relief="sunken")
            else:
                lbl.config(bg=self.theme["bg2"], fg=self.theme["dim"], relief="groove")

    def _drop_boot(self) -> None:
        self.boot.place_forget()
        self.boot.destroy()

    # -- bridge to engine ---------------------------------------------------
    def _command(self, body: Dict[str, Any]) -> None:
        cmd = body.get("cmd")
        engine = self.engine
        try:
            if cmd == "arm":
                engine.arm(live=False)
            elif cmd == "disarm":
                engine.disarm()
            elif cmd == "kill":
                engine.kill("operator kill from desktop GUI")
            elif cmd == "news":
                engine.survivor.flag_news()
            elif cmd == "lockdown":
                engine.survivor.engage_lockdown("desktop GUI")
            elif cmd == "scenario":
                # Phase-2: hot-swap one asset's market regime
                feed = engine.feed
                setter = getattr(feed, "set_scenario", None)
                if not callable(setter):
                    raise RuntimeError("feed does not support scenario swaps")
                setter(body.get("asset", ""), body.get("scenario", "gbm"))
            elif cmd == "trade":
                from ..constants import Side
                from ..data.models import Signal

                side = Side.CALL if body.get("side") == "call" else Side.PUT
                sig = Signal(
                    asset=body.get("asset", engine.feed.assets[0]),
                    side=side,
                    confidence=0.8,
                    strategy="manual",
                    reason="manual GUI fire",
                    expiry_seconds=int(body.get("expiry", 60)),
                    price=engine.feed.last_price(body.get("asset", "")) or 0.0,
                )
                engine.inject_signal(sig)
            self.status_led.config(text=f"◈ {cmd} ok", fg=self.theme["green"])
        except Exception as exc:  # noqa: BLE001
            self.status_led.config(text=f"◈ {cmd} failed: {exc}", fg=self.theme["red"])

    def _mc_summary(self) -> str:
        """Phase-2: Monte Carlo risk-lab report from the settled-trade ledger."""
        from ..risk.montecarlo import simulate_from_records

        trades = self.engine.oms.ledger.trades
        if not trades:
            return "no settled trades yet — run the gauntlet or arm the engine first"
        report = simulate_from_records(
            trades,
            starting_balance=self.engine.config.risk.starting_balance,
            runs=300,
            horizon=200,
        )
        return report.summary_text()

    def _connect(self, body: Dict[str, Any]) -> None:
        mode = body.get("mode", "paper")
        if mode == "paper":
            self.link.set_status("paper venue ready", "green")
            return
        if mode == "dryrun":
            self.link.set_status("dry-run venue ready", "yellow")
            return
        ssid = body.get("ssid", "")
        if not ssid:
            self.link.set_status("ssid required (browser session)", "red")
            return
        try:
            from ..brokers.quotex import QuotexAPI, QuotexBroker

            api = QuotexAPI(demo=bool(body.get("demo", True)))
            api.set_ssid(ssid)
            api.connect()
            self.engine.broker.disconnect()
            self.engine.broker = QuotexBroker(api)
            self.engine.broker.connect()
            self.engine.oms.broker = self.engine.broker
            self.link.set_status("quotex session live (PRACTICE)" if api.demo
                                 else "quotex session live (REAL!)", "green" if api.demo else "red")
        except Exception as exc:  # noqa: BLE001
            self.link.set_status(f"connect failed: {exc}", "red")

    def _run_backtest(self) -> str:
        from ..backtest import Backtester, matrix_table

        bt = Backtester(self.config)
        results = bt.run_matrix(bars=350, seeds=(1,))
        return matrix_table(results)

    def _save_config(self, form: Dict[str, Dict[str, str]]) -> None:
        import dataclasses

        try:
            for section, values in form.items():
                target = getattr(self.config, section)
                for name, raw in values.items():
                    current = getattr(target, name, None)
                    if current is None:
                        continue
                    if isinstance(current, bool):
                        setattr(target, name, raw.lower() in ("1", "true", "yes"))
                    elif isinstance(current, int):
                        setattr(target, name, int(float(raw)))
                    elif isinstance(current, float):
                        setattr(target, name, float(raw))
                    else:
                        setattr(target, name, raw)
            self.config.validate()
            self.config.save()
            self.status_led.config(text="◈ config saved", fg=self.theme["green"])
        except Exception as exc:  # noqa: BLE001
            self.status_led.config(text=f"◈ config error: {exc}", fg=self.theme["red"])

    # -- telemetry loop ----------------------------------------------------
    def _bus_log(self, event) -> None:
        try:
            self._ui_queue.put_nowait(event.payload)
        except queue.Full:
            pass

    def _poll(self) -> None:
        while True:
            try:
                rec = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            self.risk_p.console.append(
                f"{rec.get('name', '')} │ {rec.get('msg', '')}",
                rec.get("level", "INFO"),
            )
        try:
            state = self.engine.snapshot()
            state["trades"] = [t.to_dict() for t in self.engine.oms.recent_trades(25)]
            state["strategies"] = self.engine.ensemble.describe()
            self.dash.update_state(state)
            self.trader.update_state(state)
            self.strat.update_state(state)
            self.risk_p.update_state(state)
            from ..utils import timex

            self.clock_led.config(text=timex.utc_now().strftime("%H:%M:%S"))
            engine_state = state.get("health", {}).get("engine_state", "?").upper()
            color = {
                "ARMED": "green", "LIVE": "red", "DISARMED": "yellow",
                "KILL": "red", "SHUTDOWN": "magenta",
            }.get(engine_state, "dim")
            self.state_led.config(text=f"● {engine_state}", fg=self.theme[color])
        except Exception:  # noqa: BLE001
            log.exception("gui poll failed")
        self.after(max(50, int(1000 / max(1, self.config.display.fps))), self._poll)


def run_app(engine, config: Optional[AppConfig] = None) -> None:
    app = CybertradeApp(engine, config)
    app.mainloop()


__all__ = ["CybertradeApp", "run_app"]
