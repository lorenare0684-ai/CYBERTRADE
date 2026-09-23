"""Cyberpunk visual language for the Tkinter terminal: palette, fonts, paint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

# --- core palettes ----------------------------------------------------------
PALETTES: Dict[str, Dict[str, str]] = {
    "neon_abyss": {
        "bg": "#05060d",
        "bg2": "#0a0c18",
        "panel": "#0a0e1c",
        "cyan": "#00fff9",
        "magenta": "#ff2bd6",
        "yellow": "#ffe600",
        "green": "#39ff5e",
        "red": "#ff3860",
        "dim": "#4a5680",
        "text": "#cfe3ff",
    },
    "magenta_hell": {
        "bg": "#0d0308",
        "bg2": "#170512",
        "panel": "#15040f",
        "cyan": "#ff2bd6",
        "magenta": "#ff71ce",
        "yellow": "#ffb800",
        "green": "#05ffa1",
        "red": "#ff3860",
        "dim": "#7a4a6a",
        "text": "#ffd9f2",
    },
    "ghost_cyan": {
        "bg": "#02090c",
        "bg2": "#04141a",
        "panel": "#04141a",
        "cyan": "#7dfcff",
        "magenta": "#00b8ff",
        "yellow": "#d8ff00",
        "green": "#7dffb2",
        "red": "#ff5470",
        "dim": "#3d6b73",
        "text": "#d5fbff",
    },
}

MONO = ("Consolas", 10)
MONO_SMALL = ("Consolas", 8)
MONO_BOLD = ("Consolas", 10, "bold")
DISPLAY = ("Impact", 14)
DISPLAY_SM = ("Impact", 11)
BIG_NUM = ("Consolas", 18, "bold")


@dataclass
class Theme:
    name: str = "neon_abyss"

    def __getitem__(self, key: str) -> str:
        return PALETTES[self.name][key]

    @property
    def colors(self) -> Dict[str, str]:
        return PALETTES[self.name]

    def set(self, name: str) -> None:
        if name not in PALETTES:
            raise ValueError(f"unknown theme {name!r}")
        self.name = name


def hex_to_rgb(color: str) -> Tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_to_hex(rgb: Tuple[float, float, float]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(v))) for v in rgb))


def glow(color: str, intensity: float = 0.45) -> str:
    """Blend a neon color toward white — cheap 'glow' for canvas text."""
    r, g, b = hex_to_rgb(color)
    blend = lambda c: c + (255 - c) * max(0.0, min(1.0, intensity))
    return rgb_to_hex((blend(r), blend(g), blend(b)))


def dim(color: str, amount: float = 0.5) -> str:
    """Blend a color toward black."""
    r, g, b = hex_to_rgb(color)
    return rgb_to_hex((r * (1 - amount), g * (1 - amount), b * (1 - amount)))


def draw_scanlines(canvas, width: int, height: int, color: str = "#000000", step: int = 3) -> None:
    """Draw CRT scanlines as a low layer (call before content)."""
    for y in range(0, height, step):
        canvas.create_line(0, y, width, y, fill=color, dash=(1, 2))


def draw_grid(canvas, width: int, height: int, color: str, step: int = 32) -> None:
    for x in range(0, width, step):
        canvas.create_line(x, 0, x, height, fill=color)
    for y in range(0, height, step):
        canvas.create_line(0, y, width, y, fill=color)


__all__ = [
    "PALETTES",
    "Theme",
    "MONO",
    "MONO_SMALL",
    "MONO_BOLD",
    "DISPLAY",
    "DISPLAY_SM",
    "BIG_NUM",
    "hex_to_rgb",
    "rgb_to_hex",
    "glow",
    "dim",
    "draw_scanlines",
    "draw_grid",
]
