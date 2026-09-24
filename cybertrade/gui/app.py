"""CYBERTRADE desktop terminal — the Tkinter application shell.

Phase-31: **resolution-aware**. The window opens at 92% of the real screen
(centered, capped, never larger than the display), Tk font scaling follows
the layout plan's class, and a debounced ``<Configure>`` handler re-flows
button rows, stat cells, meters, gauges, and table heights whenever the
window crosses a breakpoint — same numbers the web twin uses in CSS
(``gui/layout.py`` is pure stdlib and shared by tests).
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from typing import Any, Dict, Optional, Tuple

from ..config import AppConfig
from ..events import Topic, default_bus
from . import layout as layout_mod
from .boot import BootScreen
from .pairing import pairing_form, run_pairing
from .panels import (
    ConnectionPanel,
    DashboardPanel,
    RiskLabPanel,
    RiskPanel,
    SettingsPanel,
    StrategiesPanel,
    TraderPanel,
)
from .theme import MONO_BOLD, MONO_SMALL, Theme, set_display_options

log = logging.getLogger("cybertrade.gui")

REFLOW_DEBOUNCE_MS = 250
REFLOW_BUCKET_PX = 40   # ignore sub-breakpoint jitter


def enable_dpi_awareness() -> None:
    """Best-effort OS DPI awareness (Windows) before Tk starts."""
    try:  # pragma: no cover - Windows only
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # per-monitor v1
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 - non-Windows / restricted
        pass


class CybertradeApp(tk.Tk):
    """Main window: tabbed neon console bound to a live TradingEngine."""

    def report_callback_exception(self, exc, val, tb):  # noqa: N802
        """Never let a failed callback disappear.

        Tk's default handler writes the traceback to stderr and carries on.
        On Windows the console is a child of the double-clicked .bat, so it
        closes the instant the process exits -- and even while it is open it
        scrolls away behind the window. A button that silently stops working
        is indistinguishable from a button that was never wired up.

        Log it where the operator can actually find it: the log file, and the
        GUI's own risk console, which is on screen.
        """
        log.exception("gui callback failed", exc_info=(exc, val, tb))
        console = getattr(self, "risk_p", None)
        if console is not None:
            try:
                console.console.append(
                    f"gui error: {val}", "ERROR")
            except Exception:  # noqa: BLE001 - reporting must not re-raise
                pass

    def __init__(self, engine, config: Optional[AppConfig] = None) -> None:
        super().__init__()
        self.engine = engine
        self.config = config or AppConfig()
        self.theme = Theme(self.config.display.theme)
        # display.glow / scanlines / show_grid are honoured here, once, rather
        # than being accepted and ignored.
        set_display_options(
            glow=self.config.display.glow,
            scanlines=self.config.display.scanlines,
            grid=self.config.display.show_grid,
        )
        self.title("CYBERTRADE // NEON PROTOCOL")
        self.configure(bg=self.theme["bg"])
        self._ui_queue: "queue.Queue" = queue.Queue(maxsize=500)

        self.fit_screen = bool(getattr(self.config.display, "fit_screen", True))
        self._screen: Tuple[int, int] = (
            self.winfo_screenwidth(), self.winfo_screenheight(),
        )
        dpi = self._dpi_scale()

        if self.fit_screen:
            # Initial geometry: plan against the real screen (window == 92%).
            p = layout_mod.plan(
                self._screen[0], self._screen[1],
                dpi=dpi, screen_w=self._screen[0], screen_h=self._screen[1],
            )
            self.geometry(
                f"{p.window_w}x{p.window_h}+{p.offset_x}+{p.offset_y}"
            )
            self.minsize(p.min_w, p.min_h)
        else:
            # Operator opted out: classic fixed shell.
            self.geometry("1280x800")
            self.minsize(960, 600)
            p = layout_mod.plan(1280, 800, dpi=dpi)

        self._plan = p
        self._apply_font_scale(p)
        self._last_size: Tuple[int, int] = (p.width, p.height)
        self._reflow_job = None

        self._build_header()
        self._build_body()
        self._build_footer()
        self._apply_plan()

        if self.fit_screen:
            self.bind("<Configure>", self._on_configure)

        # boot screen overlay
        self.boot = BootScreen(self, self.theme, on_done=self._drop_boot,
                               animate=self.config.display.animate)
        self.boot.place(relx=0, rely=0, relwidth=1, relheight=1)

        # telemetry marshaling (bus threads -> Tk main loop)
        default_bus.subscribe(Topic.LOG, self._bus_log)
        self._poll()
        log.info("gui shell %s", layout_mod.describe(self._plan))

    # -- resolution awareness ---------------------------------------------
    def _dpi_scale(self) -> float:
        try:
            scaling = float(self.tk.call("tk", "scaling"))
            return max(0.5, scaling * 72.0 / 96.0)
        except Exception:  # noqa: BLE001
            return 1.0

    def _apply_font_scale(self, plan) -> None:
        """Scale point fonts globally via Tk's scaling factor."""
        try:
            self.tk.call("tk", "scaling", (96.0 / 72.0) * plan.font_scale)
        except Exception:  # noqa: BLE001
            pass

    def _on_configure(self, event) -> None:
        if event.widget is not self:
            return
        w, h = int(event.width), int(event.height)
        lw, lh = self._last_size
        if abs(w - lw) < REFLOW_BUCKET_PX and abs(h - lh) < REFLOW_BUCKET_PX:
            return
        if self._reflow_job is not None:
            try:
                self.after_cancel(self._reflow_job)
            except Exception:  # noqa: BLE001
                pass
        self._reflow_job = self.after(REFLOW_DEBOUNCE_MS, self._do_reflow, w, h)

    def _do_reflow(self, w: int, h: int) -> None:
        self._reflow_job = None
        self._last_size = (w, h)
        # Re-plan from the CONTENT size; the operator owns the window bounds.
        self._plan = layout_mod.plan(
            w, h, dpi=self._dpi_scale(),
            screen_w=self._screen[0], screen_h=self._screen[1],
        )
        self._apply_font_scale(self._plan)
        self._reflow_tabs()
        self._apply_plan()
        log.info("gui reflow %s", layout_mod.describe(self._plan))

    def _apply_plan(self) -> None:
        plan = self._plan
        for pane in self._panes.values():
            fn = getattr(pane, "apply_layout", None)
            if callable(fn):
                try:
                    fn(plan)
                except Exception:  # noqa: BLE001 — one panel never blocks rest
                    log.exception("apply_layout failed on %s", type(pane).__name__)

    def _reflow_tabs(self) -> None:
        """Tab buttons wrap into rows per the plan (compact wraps 4/row)."""
        for frame in getattr(self.tab_buttons, "_reflow_frames", []):
            try:
                frame.destroy()
            except Exception:  # noqa: BLE001
                pass
        for lbl in self._tab_btns.values():
            try:
                lbl.pack_forget()
            except Exception:  # noqa: BLE001
                pass
        frames = []
        row = None
        for i, name in enumerate(self._tab_names):
            if i % max(1, self._plan.tab_per_row) == 0:
                row = tk.Frame(self.tab_buttons, bg=self.theme["bg"])
                row.pack(fill="x")
                frames.append(row)
            self._tab_btns[name].pack(in_=row, side="left", padx=3, pady=2)
        self.tab_buttons._reflow_frames = frames

    # -- chrome ------------------------------------------------------------
    def _build_header(self) -> None:
        t = self.theme
        head = tk.Frame(self, bg=t["bg2"], height=self._plan.header_h)
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
        self.recovery_led = tk.Label(
            self, text="RECOVERY · checking…", bg=t["bg2"], fg=t["cyan"],
            font=MONO_SMALL, anchor="w", justify="left", padx=10, pady=4,
            wraplength=800,
        )
        self.recovery_led.pack(fill="x")  # visible above every tab, even compact

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
        self.bt_p = RiskLabPanel(self.panes, t, run_cb=self._mc_summary,
                                 mc_cb=self._mc_summary)
        self.link = ConnectionPanel(self.panes, t, connect_cb=self._connect,
                                    login_cb=self._login)
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
            text="LIVE ONLY · REAL ORDER FLOW · UNOFFICIAL QUOTEX BRIDGE · "
                 "NO SYSTEM SURVIVES EVERY MARKET · SEE DISCLAIMER.md",
            bg=t["bg2"], fg=t["yellow"], font=MONO_SMALL,
        ).pack(side="left", padx=10)
        self.status_led = tk.Label(foot, text="◈ ready", bg=t["bg2"],
                                   fg=t["dim"], font=MONO_SMALL)
        self.status_led.pack(side="right", padx=10)
        self.layout_led = tk.Label(
            foot, text=layout_mod.describe(self._plan),
            bg=t["bg2"], fg=t["cyan"], font=MONO_SMALL,
        )
        self.layout_led.pack(side="right", padx=10)

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
                engine.arm()
            elif cmd == "disarm":
                engine.disarm()
            elif cmd == "kill":
                engine.kill("operator kill from desktop GUI")
            elif cmd == "news":
                engine.survivor.flag_news()
            elif cmd == "lockdown":
                engine.survivor.engage_lockdown("desktop GUI")
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
            return "no settled trades yet — trade live first; the risk lab never invents samples"
        report = simulate_from_records(
            trades,
            starting_balance=self.engine.config.risk.starting_balance,
            runs=300,
            horizon=200,
        )
        return report.summary_text()

    def _connect(self, body: Dict[str, Any]) -> None:
        """Re-pair the venue session in place — Quotex is the only venue."""
        ssid = body.get("ssid", "")
        if not ssid:
            self.link.set_status("ssid required (browser session)", "red")
            return
        try:
            from ..brokers.quotex import QuotexAPI, QuotexBroker

            demo = body.get("demo")
            if demo is None:
                self.link.set_status("pick a purse: PRACTICE or REAL", "yellow")
                return
            api = QuotexAPI(demo=bool(demo))
            api.set_ssid(ssid)
            api.connect()
            self.engine.broker.disconnect()
            self.engine.broker = QuotexBroker(api, allow_orders=True)
            self.engine.broker.connect()
            self.engine.oms.broker = self.engine.broker
            self.link.set_status("quotex session live (PRACTICE)" if api.demo
                                 else "quotex session live (REAL!)", "green" if api.demo else "red")
        except Exception as exc:  # noqa: BLE001
            self.link.set_status(f"connect failed: {exc}", "red")

    def _login(self, body: Dict[str, Any]) -> None:
        """Chrome-assisted Quotex pairing — the CAPTCHA stays the human's job.

        ``pair_session`` blocks for as long as the operator takes to log in,
        so it runs on a worker thread (``gui.pairing.run_pairing``) and the
        result is marshalled back onto the Tk thread with ``after`` — a frozen
        UI during a 240s wait would look exactly like a hang.
        """
        purse = body.get("demo")
        if purse is None:
            self.link.set_busy(False)
            self.link.set_status("pick a purse: PRACTICE or REAL", "yellow")
            return
        ok, error, values = pairing_form(
            profile=body.get("profile", ""),
            port=body.get("port"),
            timeout=body.get("timeout"),
        )
        if not ok:
            self.link.set_busy(False)
            self.link.set_status(error, "yellow")
            return
        run_pairing(
            session_path=self.config.qx_session_path,
            profile=values["profile"],
            port=values["port"],
            timeout=values["timeout"],
            on_done=lambda sess: self.after(
                0, lambda: self._login_done(sess, purse)),
            on_error=lambda exc: self.after(
                0, lambda: self._login_failed(exc)),
        )

    def _login_done(self, sess: Dict[str, str], purse: bool) -> None:
        """Cookie captured — adopt it and wire the live session in place."""
        ssid = str(sess.get("ssid", ""))
        self.link.set_busy(False)
        if not ssid:
            self.link.set_status("Chrome closed without a sessionid cookie", "red")
            return
        self.link.adopt_session(ssid, "session captured — connecting…")
        self._connect({"mode": "quotex", "ssid": ssid, "demo": purse})

    def _login_failed(self, exc: Exception) -> None:
        self.link.set_busy(False)
        reason = str(exc) or exc.__class__.__name__
        self.link.set_status(f"pairing failed: {reason}", "red")
        try:
            # surface it in the operator's own alert feed, not just the label
            from ..bot.alerts import Alert

            self.engine.alerts.fire(
                Alert(kind="session", title="chrome pairing failed",
                      body=reason, severity="warn"))
        except Exception:  # noqa: BLE001 — the status line already says it
            log.debug("alert feed unavailable", exc_info=True)

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
            recovery = state.get("continuity", {})
            label = "RECOVERY OFF · ephemeral engine"
            if recovery.get("enabled"):
                label = ("RECOVERY HOLD · " + recovery.get("reason", "review required")
                         if recovery.get("blocked") else
                         "RECOVERY " + ("RESTORED" if recovery.get("restored") else "ACTIVE")
                         + " · risk governor + book checkpointed")
            self.recovery_led.config(
                text=label, wraplength=max(200, self.winfo_width() - 32),
                fg=self.theme["yellow" if recovery.get("blocked") else "cyan"],
            )
        except Exception:  # noqa: BLE001
            log.exception("gui poll failed")
        self.after(max(50, int(1000 / max(1, self.config.display.fps))), self._poll)


def run_app(engine, config: Optional[AppConfig] = None) -> None:
    enable_dpi_awareness()
    app = CybertradeApp(engine, config)
    app.mainloop()


__all__ = ["CybertradeApp", "run_app", "enable_dpi_awareness"]
