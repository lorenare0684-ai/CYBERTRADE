"""Phase-31 tests — resolution-aware GUI: buttons + information placement.

The desktop shell plans placement in pure ``gui.layout`` (no tkinter) and
the web twin mirrors the same breakpoints in CSS media queries: compact
≤1099/short, medium <1600, large <2560, wide ≥2560. Buttons wrap
(``tab_grid`` / fire rows), information stacks by priority, and secondary
labs leave first on compact — chart, fire control, account core and
positions never leave.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from cybertrade.config import AppConfig
from cybertrade.gui.layout import (
    HIDDEN_ON_COMPACT,
    LARGE_MAX_W,
    MEDIUM_MAX_W,
    PRIORITY,
    center_offset,
    classify,
    clamp_window,
    describe,
    plan,
    tab_grid,
    wrap_items,
)

ROOT = Path(__file__).resolve().parents[1]
APP_PY = (ROOT / "cybertrade" / "gui" / "app.py").read_text()
PANELS_PY = (ROOT / "cybertrade" / "gui" / "panels.py").read_text()
WIDGETS_PY = (ROOT / "cybertrade" / "gui" / "widgets.py").read_text()
CSS = (ROOT / "cybertrade" / "web" / "static" / "css" / "cyber.css").read_text()
JS = (ROOT / "cybertrade" / "web" / "static" / "js" / "app.js").read_text()
HTML = (ROOT / "cybertrade" / "web" / "static" / "index.html").read_text()


class TestClassify(unittest.TestCase):
    def test_breakpoint_boundaries(self):
        self.assertEqual(classify(1099, 800), "compact")
        self.assertEqual(classify(1100, 800), "medium")
        self.assertEqual(classify(1599, 900), "medium")
        self.assertEqual(classify(1600, 900), "large")
        self.assertEqual(classify(LARGE_MAX_W - 1, 900), "large")
        self.assertEqual(classify(2560, 1440), "wide")

    def test_short_height_forces_compact(self):
        self.assertEqual(classify(1920, 619), "compact")
        self.assertEqual(classify(1280, 600), "compact")
        self.assertEqual(classify(1280, 620), "medium")


class TestWindowGeometry(unittest.TestCase):
    def test_window_fits_screen_and_centers(self):
        for sw, sh in ((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)):
            w, h = clamp_window(sw, sh, sw, sh)
            self.assertLessEqual(w, sw)
            self.assertLessEqual(h, sh)
            x, y = center_offset(w, h, sw, sh)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, sw)
            self.assertLessEqual(y + h, sh)

    def test_tiny_screen_never_clips(self):
        w, h = clamp_window(800, 500, 800, 500)
        self.assertLessEqual(w, 800)
        self.assertLessEqual(h, 500)

    def test_plan_window_is_92ish_and_capped(self):
        p = plan(1920, 1080, screen_w=1920, screen_h=1080)
        self.assertEqual(p.window_w, int(1920 * 0.92))
        huge = plan(4000, 3000, screen_w=4000, screen_h=3000)
        self.assertLessEqual(huge.window_w, 2200)
        self.assertLessEqual(huge.window_h, 1400)

    def test_min_size_never_exceeds_window(self):
        p = plan(800, 500)
        self.assertLessEqual(p.min_w, p.window_w)
        self.assertLessEqual(p.min_h, p.window_h)


class TestMetrics(unittest.TestCase):
    def test_scale_clamped(self):
        self.assertGreaterEqual(plan(900, 600).scale, 0.8)
        self.assertLessEqual(plan(4000, 3000).scale, 1.6)
        self.assertAlmostEqual(plan(1920, 1080, dpi=2.0).scale, 1.6, places=3)
        # large class base 1.15 × dpi 1.5 = 1.725 → clamped to 1.6
        self.assertAlmostEqual(plan(1920, 1080, dpi=1.5).scale, 1.6, places=3)
        # medium (1366) base 1.0 × dpi 1.15 stays unclamped
        self.assertAlmostEqual(plan(1366, 768, dpi=1.15).scale, 1.15, places=3)

    def test_metrics_escalate_with_class(self):
        order = ["compact", "medium", "large", "wide"]
        sizes = [1024, 1366, 1920, 2560]
        meters, gauges, cols, blotters = [], [], [], []
        for w in sizes:
            p = plan(w, 900)
            self.assertIn(p.screen_class, order)
            meters.append(p.meter_width)
            gauges.append(p.gauge_size)
            cols.append(p.columns)
            blotters.append(p.blotter_rows)
        for series in (meters, gauges, blotters):
            self.assertEqual(series, sorted(series))
            self.assertLess(series[0], series[-1])
        self.assertEqual(cols, [1, 1, 2, 3])

    def test_stat_and_button_placements(self):
        c, m = plan(1024, 700), plan(1366, 768)
        self.assertEqual(c.stat_cols, 3)
        self.assertEqual(m.stat_cols, 4)
        self.assertEqual(c.fire_per_row, 3)      # fire buttons wrap tight
        self.assertEqual(m.fire_per_row, 5)
        self.assertLess(c.button_h, plan(2560, 1440).button_h)

    def test_compact_drops_lab_keeps_priority(self):
        c = plan(1024, 700)
        self.assertEqual(c.hidden_panels, HIDDEN_ON_COMPACT)
        self.assertEqual(set(c.hidden_panels), {"risklab", "equity"})
        self.assertTrue(c.body_scroll)
        self.assertNotIn("chart", c.hidden_panels)
        self.assertNotIn("fire", c.hidden_panels)
        self.assertNotIn("account", c.hidden_panels)
        self.assertNotIn("positions", c.hidden_panels)
        wide = plan(2560, 1440)
        self.assertEqual(wide.hidden_panels, ())
        self.assertFalse(wide.body_scroll)
        self.assertEqual(wide.priority, PRIORITY)
        self.assertEqual(PRIORITY[0], "chart")

    def test_describe_one_liner(self):
        text = describe(plan(1920, 1080))
        self.assertIn("large", text)
        self.assertIn("hidden=none", text)
        self.assertIn("compact", describe(plan(1024, 700)))


class TestWrapping(unittest.TestCase):
    def test_wrap_items_rows(self):
        self.assertEqual(wrap_items(list(range(10)), 4),
                         [(0, 1, 2, 3), (4, 5, 6, 7), (8, 9)])
        self.assertEqual(wrap_items([], 4), [])
        self.assertEqual(wrap_items([1], 0), [(1,)])   # per_row floor at 1

    def test_tab_grid_uses_plan(self):
        names = tuple(f"T{i}" for i in range(7))
        compact = tab_grid(names, plan(1024, 700))
        self.assertEqual(len(compact), 2)              # 4 + 3
        flat = [n for row in compact for n in row]
        self.assertEqual(flat, list(names))
        wide = tab_grid(names, plan(2560, 1440))
        self.assertEqual(len(wide), 1)                 # single row


class TestFitScreenConfig(unittest.TestCase):
    def test_default_on_and_roundtrip(self):
        cfg = AppConfig()
        self.assertTrue(cfg.display.fit_screen)
        cfg.display.fit_screen = False
        blob = json.loads(json.dumps(cfg.to_dict()))
        self.assertFalse(blob["display"]["fit_screen"])
        back = AppConfig.from_dict(blob)
        self.assertFalse(back.display.fit_screen)


class TestDesktopShellSource(unittest.TestCase):
    """The Tk shell cannot import headless — assert the wiring statically."""

    def test_plan_driven_geometry_and_reflow(self):
        self.assertIn("winfo_screenwidth", APP_PY)
        self.assertIn("<Configure>", APP_PY)
        self.assertIn("_apply_plan", APP_PY)
        self.assertIn("_reflow_tabs", APP_PY)
        self.assertIn("enable_dpi_awareness", APP_PY)
        # fixed geometry only survives behind the opt-out flag
        self.assertEqual(APP_PY.count('geometry("1280x800")'), 1)
        self.assertIn("fit_screen", APP_PY)

    def test_panels_expose_apply_layout(self):
        self.assertEqual(PANELS_PY.count("def apply_layout"), 5)
        self.assertIn("def _reflow", PANELS_PY)
        self.assertIn("self.fire_btns", PANELS_PY)

    def test_widgets_resizable(self):
        self.assertGreaterEqual(WIDGETS_PY.count("def resize"), 3)
        self.assertIn("def set_height", WIDGETS_PY)


class TestWebResponsiveAssets(unittest.TestCase):
    """The browser twin mirrors the same breakpoints in CSS/JS."""

    def test_explicit_information_placement(self):
        self.assertIn("grid-template-areas", CSS)
        self.assertIn("grid-area: pos", CSS)          # positions panel had NO rule pre-P31
        self.assertIn("grid-area: blotter", CSS)
        self.assertIn("grid-area: console", CSS)
        self.assertIn("grid-area: chart", CSS)
        self.assertIn("grid-area: side", CSS)
        self.assertIn("grid-area: intel", CSS)

    def test_breakpoints_mirror_layout_py(self):
        self.assertIn("@media (max-width: 1099px)", CSS)
        self.assertIn("@media (max-width: 760px)", CSS)
        self.assertIn("@media (min-width: 2560px)", CSS)
        self.assertIn("@media (max-height: 700px) and (min-width: 1100px)", CSS)
        self.assertIn("overflow: auto", CSS)           # compact scrolls

    def test_compact_button_targets_and_lab_hiding(self):
        self.assertIn(".risklab-panel, .equity-panel { display: none; }", CSS)
        self.assertIn("min-height: 44px", CSS)         # touch targets

    def test_canvas_fitting_wired(self):
        self.assertIn("ResizeObserver", JS)
        self.assertIn("function fitCanvas", JS)
        self.assertIn("devicePixelRatio", JS)
        self.assertIn("data-screen-class", JS.replace("dataset.screenClass",
                                                      "data-screen-class"))
        self.assertIn('viewport', HTML)


if __name__ == "__main__":
    unittest.main()
