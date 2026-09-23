"""Type coercion and validation helpers used at every trust boundary."""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional, Sequence

from ..exceptions import ConfigError


def as_float(value: Any, name: str = "value", lo: Optional[float] = None,
             hi: Optional[float] = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be numeric, got {value!r}") from exc
    if math.isnan(out) or math.isinf(out):
        raise ConfigError(f"{name} must be finite, got {value!r}")
    if lo is not None and out < lo:
        raise ConfigError(f"{name} must be >= {lo}, got {out}")
    if hi is not None and out > hi:
        raise ConfigError(f"{name} must be <= {hi}, got {out}")
    return out


def as_int(value: Any, name: str = "value", lo: Optional[int] = None,
           hi: Optional[int] = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be integer, got {value!r}") from exc
    if lo is not None and out < lo:
        raise ConfigError(f"{name} must be >= {lo}, got {out}")
    if hi is not None and out > hi:
        raise ConfigError(f"{name} must be <= {hi}, got {out}")
    return out


def as_str(value: Any, name: str = "value", allow_empty: bool = False) -> str:
    if value is None:
        raise ConfigError(f"{name} must be a string, got None")
    out = str(value)
    if not allow_empty and not out.strip():
        raise ConfigError(f"{name} must be non-empty")
    return out


def as_bool(value: Any, name: str = "value") -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{name} must be boolean-like, got {value!r}")


def as_list(value: Any, name: str = "value", item_type: type | None = None) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ConfigError(f"{name} must be a list, got {type(value).__name__}")
    out = list(value)
    if item_type is not None:
        for i, item in enumerate(out):
            if not isinstance(item, item_type):
                raise ConfigError(
                    f"{name}[{i}] must be {item_type.__name__}, got {type(item).__name__}"
                )
    return out


def one_of(value: Any, allowed: Sequence[Any], name: str = "value") -> Any:
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {list(allowed)}, got {value!r}")
    return value


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


__all__ = [
    "as_float",
    "as_int",
    "as_str",
    "as_bool",
    "as_list",
    "one_of",
    "clamp01",
    "require",
]
