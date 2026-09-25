"""JSON encode/decode with tolerant defaults for internal dataclasses and enums."""

from __future__ import annotations

import dataclasses
import json
from enum import Enum
from typing import Any


def _default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def dumps(obj: Any, *, indent: int | None = None, sort_keys: bool = False) -> str:
    separators = (",", ":") if indent is None else None
    return json.dumps(
        obj, default=_default, indent=indent, sort_keys=sort_keys, separators=separators
    )


def loads(text: str | bytes, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*."""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def dig(data: Any, path: str, default: Any = None, sep: str = ".") -> Any:
    """Fetch a nested value: ``dig(payload, "data.balance")``."""
    cur = data
    for part in path.split(sep):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


__all__ = ["dumps", "loads", "deep_merge", "dig"]
