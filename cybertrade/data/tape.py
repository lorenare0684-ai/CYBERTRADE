"""Tape recorder: append-only JSONL forensics of everything the bot saw.

Ticks, signals, settlements, alerts, and risk decisions land in
``data/tapes/YYYY-MM-DD.jsonl`` so any session can be replayed offline after
a blow-up (or a lucky streak).  Recording is bounded (bytes per file, files
per day) and failure-proof — a full disk must never stop trading.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from ..events import Event, Topic, default_bus

log = logging.getLogger("cybertrade.tape")

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FILES_PER_DAY = 8


def _record(kind: str, payload: Any) -> Dict[str, Any]:
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    elif hasattr(payload, "__dict__") and not isinstance(payload, dict):
        try:
            payload = {
                k: (v.value if hasattr(v, "value") else v)
                for k, v in vars(payload).items()
                if not k.startswith("_")
            }
        except Exception:  # noqa: BLE001
            payload = {"repr": repr(payload)[:200]}
    return {"ts": time.time(), "kind": kind, "payload": payload}


class TapeRecorder:
    """Bus subscriber writing a bounded daily JSONL tape."""

    def __init__(self, directory: str = "data/tapes", enabled: bool = True,
                 max_file_bytes: int = MAX_FILE_BYTES) -> None:
        self.directory = Path(directory).expanduser()
        self.enabled = enabled
        self.max_file_bytes = max_file_bytes
        self._lock = threading.RLock()
        self._fh = None
        self._day = ""
        self._part = 0
        self._written = 0
        self._dropped = 0
        self._subscribed = False

    # -- wiring ------------------------------------------------------------
    def attach(self, bus=default_bus) -> None:
        if self._subscribed or not self.enabled:
            return
        for topic in (Topic.TICK, Topic.SIGNAL, Topic.SETTLE, Topic.ALERT,
                      Topic.RISK, Topic.ORDER_SUBMIT, Topic.KILL, Topic.SCENARIO):
            bus.subscribe(topic, self._on_event)
        self._subscribed = True

    def detach(self, bus=default_bus) -> None:
        if not self._subscribed:
            return
        for topic in (Topic.TICK, Topic.SIGNAL, Topic.SETTLE, Topic.ALERT,
                      Topic.RISK, Topic.ORDER_SUBMIT, Topic.KILL, Topic.SCENARIO):
            bus.unsubscribe(topic, self._on_event)
        self._subscribed = False
        self.close()

    def _on_event(self, event: Event) -> None:
        self.write(event.topic, event.payload)

    # -- io ----------------------------------------------------------------
    def write(self, kind: str, payload: Any) -> None:
        if not self.enabled:
            return
        try:
            rec = _record(str(kind), payload)
            line = json.dumps(rec, default=str)[:65000]
            with self._lock:
                fh = self._ensure_file()
                if fh is None:
                    self._dropped += 1
                    return
                fh.write(line + "\n")
                fh.flush()
                self._written += 1
        except Exception:  # noqa: BLE001 — forensics never break trading
            self._dropped += 1

    def _ensure_file(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self._fh is not None and day == self._day:
            if self._fh.tell() < self.max_file_bytes:
                return self._fh
            self._close_locked()
        if self._fh is None:
            self._day = day
            self._part = 0
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                return None
            while self._part < MAX_FILES_PER_DAY:
                path = self.directory / f"{day}.{self._part:02d}.jsonl"
                if not path.exists() or path.stat().st_size < self.max_file_bytes:
                    self._fh = open(path, "a", encoding="utf-8")
                    return self._fh
                self._part += 1
            return None  # day quota exhausted
        return self._fh

    def _close_locked(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def stats(self) -> Dict[str, int]:
        return {"written": self._written, "dropped": self._dropped}


def replay(path: str, kinds: Optional[List[str]] = None) -> Iterator[Dict[str, Any]]:
    """Stream records back from a tape file (optionally filtered by kind)."""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if kinds and rec.get("kind") not in kinds:
                continue
            yield rec


def latest_tape(directory: str = "data/tapes") -> Optional[str]:
    d = Path(directory).expanduser()
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"))
    return str(files[-1]) if files else None


__all__ = ["TapeRecorder", "replay", "latest_tape", "MAX_FILE_BYTES"]
