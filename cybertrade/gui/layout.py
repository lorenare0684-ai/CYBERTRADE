"""Resolution-aware layout planner for the desktop terminal (Phase-31).

Pure stdlib — **no tkinter import** — so headless sandboxes can compute and
test the same placement decisions the Tk shell applies. The web twin mirrors
these breakpoints in CSS media queries (``cyber.css`` Phase-31 block):
``compact ≤1099 / short``, ``medium <1600``, ``large <2560``, ``wide ≥2560``.

The plan answers the questions a trading HUD actually has:

* **Where does the window go?** → 92% of the screen, capped, centered.
* **How many columns / how wide are the widgets?** → per-class metric table
  (stat cells, meters, gauges, buttons, blotter rows, console lines).
* **How do buttons wrap?** → ``tab_grid`` / ``wrap_items`` row breaking.
* **What gets dropped when space runs out?** → ``hidden_panels`` (the Monte
  Carlo lab + equity trace leave first; chart, fire control, account core
  and positions never leave).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

# Breakpoints (CSS media queries mirror these numbers exactly).
COMPACT_MAX_W = 1100
COMPACT_MIN_H = 620
MEDIUM_MAX_W = 1600
LARGE_MAX_W = 2560

# Window sizing
WINDOW_FILL = 0.92          # fraction of the screen to occupy
WINDOW_CAP = (2200, 1400)   # never open absurdly large on 4K+
MIN_WINDOW = (960, 600)     # preferred minimum (clamped to the screen)

# Information priority — lowest priority is hidden first on compact screens.
PRIORITY: Tuple[str, ...] = (
    "chart", "fire", "account", "positions", "blotter",
    "deck", "intel", "console", "lab",
)
HIDDEN_ON_COMPACT: Tuple[str, ...] = ("risklab", "equity")


@dataclass(frozen=True)
class LayoutPlan:
    """Everything the shell needs to place buttons and information."""

    width: int
    height: int
    screen_class: str                 # compact | medium | large | wide
    font_scale: float                 # class base (0.85–1.35)
    scale: float                      # font_scale × dpi, clamped 0.8–1.6
    columns: int                      # dashboard column emphasis (1–3)

    window_w: int
    window_h: int
    offset_x: int
    offset_y: int
    min_w: int
    min_h: int

    stat_cols: int
    ladder_per_row: int
    tab_per_row: int
    fire_per_row: int

    meter_width: int
    gauge_size: int
    button_w: int
    button_h: int

    blotter_rows: int
    console_lines: int
    header_h: int

    body_scroll: bool = False         # compact: let the page/stack scroll
    hidden_panels: Tuple[str, ...] = ()
    priority: Tuple[str, ...] = field(default=PRIORITY)


# Per-class metrics: compact → medium → large → wide
_SPECS: Dict[str, Dict[str, float]] = {
    "compact": dict(font=0.85, columns=1, stat_cols=3, ladder=3, tabs=4,
                    fire=3, meter=220, gauge=90, btn_w=100, btn_h=28,
                    blotter=8, console=10, header=42),
    "medium":  dict(font=1.00, columns=1, stat_cols=4, ladder=5, tabs=6,
                    fire=5, meter=340, gauge=130, btn_w=130, btn_h=34,
                    blotter=12, console=20, header=54),
    "large":   dict(font=1.15, columns=2, stat_cols=5, ladder=5, tabs=8,
                    fire=5, meter=420, gauge=150, btn_w=150, btn_h=36,
                    blotter=14, console=24, header=60),
    "wide":    dict(font=1.35, columns=3, stat_cols=6, ladder=5, tabs=99,
                    fire=5, meter=480, gauge=160, btn_w=160, btn_h=38,
                    blotter=18, console=28, header=66),
}


def classify(width: int, height: int) -> str:
    """Map a pixel size onto a screen class (height can force compact)."""
    if width < COMPACT_MAX_W or height < COMPACT_MIN_H:
        return "compact"
    if width < MEDIUM_MAX_W:
        return "medium"
    if width < LARGE_MAX_W:
        return "large"
    return "wide"


def clamp_window(width: int, height: int, screen_w: int, screen_h: int) -> Tuple[int, int]:
    """92% of the screen, capped, never larger than the screen itself."""
    w = int(screen_w * WINDOW_FILL)
    h = int(screen_h * WINDOW_FILL)
    w = min(w, WINDOW_CAP[0])
    h = min(h, WINDOW_CAP[1])
    # Prefer the minimum viable window, but never exceed the screen.
    w = max(min(MIN_WINDOW[0], screen_w), min(w, screen_w))
    h = max(min(MIN_WINDOW[1], screen_h), min(h, screen_h))
    # honour a requested aspect (width/height) when it materially differs
    # from the screen aspect and fits inside the cap (±2px rounding ignored)
    if width > 0 and height > 0 and screen_w >= MIN_WINDOW[0]:
        aspect_w = int(h * (width / height))
        if (
            abs(aspect_w - w) > 2
            and MIN_WINDOW[0] <= aspect_w <= min(WINDOW_CAP[0], screen_w)
        ):
            w = aspect_w
    return w, h


def center_offset(window_w: int, window_h: int, screen_w: int, screen_h: int) -> Tuple[int, int]:
    """Center a window on the screen (never negative)."""
    return max(0, (screen_w - window_w) // 2), max(0, (screen_h - window_h) // 2)


def wrap_items(items: Sequence, per_row: int) -> List[Tuple]:
    """Break a sequence into rows of ``per_row`` (last row may be short)."""
    per = max(1, int(per_row))
    return [tuple(items[i:i + per]) for i in range(0, len(items), per)]


def tab_grid(names: Sequence[str], plan: LayoutPlan) -> List[Tuple[str, ...]]:
    """Rows of tab labels for this plan (how the tab bar wraps)."""
    return wrap_items(names, plan.tab_per_row)


def plan(
    width: int,
    height: int,
    *,
    dpi: float = 1.0,
    screen_w: int = None,   # type: ignore[assignment]
    screen_h: int = None,   # type: ignore[assignment]
) -> LayoutPlan:
    """Compute the full placement plan for a window/screen of this size.

    ``screen_w``/``screen_h`` default to ``width``/``height`` (kiosk-style:
    window == screen). The desktop shell passes real screen dimensions when
    choosing the initial geometry, and content dimensions on reflow.
    """
    width, height = int(width), int(height)
    screen_w = int(screen_w) if screen_w else width
    screen_h = int(screen_h) if screen_h else height
    cls = classify(width, height)
    spec = _SPECS[cls]

    font_scale = float(spec["font"])
    scale = max(0.8, min(1.6, font_scale * max(0.5, float(dpi))))

    win_w, win_h = clamp_window(width, height, screen_w, screen_h)
    off_x, off_y = center_offset(win_w, win_h, screen_w, screen_h)
    min_w = min(MIN_WINDOW[0], win_w)
    min_h = min(MIN_WINDOW[1], win_h)

    hidden: Tuple[str, ...] = HIDDEN_ON_COMPACT if cls == "compact" else ()
    body_scroll = cls == "compact"

    return LayoutPlan(
        width=width,
        height=height,
        screen_class=cls,
        font_scale=round(font_scale, 3),
        scale=round(scale, 3),
        columns=int(spec["columns"]),
        window_w=win_w,
        window_h=win_h,
        offset_x=off_x,
        offset_y=off_y,
        min_w=int(min_w),
        min_h=int(min_h),
        stat_cols=int(spec["stat_cols"]),
        ladder_per_row=int(spec["ladder"]),
        tab_per_row=int(spec["tabs"]),
        fire_per_row=int(spec["fire"]),
        meter_width=int(spec["meter"]),
        gauge_size=int(spec["gauge"]),
        button_w=int(spec["btn_w"]),
        button_h=int(spec["btn_h"]),
        blotter_rows=int(spec["blotter"]),
        console_lines=int(spec["console"]),
        header_h=int(spec["header"]),
        body_scroll=body_scroll,
        hidden_panels=hidden,
    )


def describe(plan: LayoutPlan) -> str:
    """One-line operator summary (status bar / logs)."""
    hidden = ",".join(plan.hidden_panels) or "none"
    return (
        f"{plan.screen_class} {plan.width}x{plan.height} · "
        f"cols={plan.columns} scale={plan.scale} · "
        f"meters={plan.meter_width} gauges={plan.gauge_size} · "
        f"blotter={plan.blotter_rows} hidden={hidden}"
    )


__all__ = [
    "LayoutPlan",
    "PRIORITY",
    "HIDDEN_ON_COMPACT",
    "classify",
    "clamp_window",
    "center_offset",
    "wrap_items",
    "tab_grid",
    "plan",
    "describe",
    "COMPACT_MAX_W",
    "COMPACT_MIN_H",
    "MEDIUM_MAX_W",
    "LARGE_MAX_W",
]
