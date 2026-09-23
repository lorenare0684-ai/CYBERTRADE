"""Operator state store — lockdown and deck decisions survive restart.

Phase-28: a crisis lockdown and per-strategy ON/OFF toggles are operator
decisions, not process memory. Atomic JSON (tmp + replace); missing or
corrupt files load as empty — a bad file never stops boot, and an engine
that never hears an operator never writes one.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

log = logging.getLogger("cybertrade.statestore")

VERSION = 1


def load_operator_state(path: str) -> Dict[str, Any]:
    """Read saved operator state; missing/corrupt -> {} (never raises)."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            log.warning("operator state at %s is not an object — ignored", path)
            return {}
        return data
    except (OSError, ValueError) as exc:
        log.warning("operator state unreadable (%s) — starting fresh", exc)
        return {}


def save_operator_state(path: str, data: Dict[str, Any]) -> bool:
    """Atomic write. False = no path or write failed (never raises)."""
    if not path:
        return False
    try:
        payload = dict(data)
        payload["version"] = VERSION
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        log.exception("operator state save failed: %s", path)
        return False


__all__ = ["load_operator_state", "save_operator_state", "VERSION"]
