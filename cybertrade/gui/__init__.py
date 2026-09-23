"""Desktop GUI package (Tkinter).  Import fails softly without a display."""

from __future__ import annotations

try:  # pragma: no cover - exercised on desktop hosts
    from .app import CybertradeApp, run_app
    from .theme import PALETTES, Theme
    GUI_AVAILABLE = True
except Exception:  # noqa: BLE001 - headless sandboxes lack tk
    GUI_AVAILABLE = False
    CybertradeApp = None  # type: ignore[assignment]
    run_app = None  # type: ignore[assignment]
    PALETTES = {}  # type: ignore[assignment]
    Theme = None  # type: ignore[assignment]

__all__ = ["GUI_AVAILABLE", "CybertradeApp", "run_app", "Theme", "PALETTES"]
