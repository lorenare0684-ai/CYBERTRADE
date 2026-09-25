"""Atomic state files and strict recovery codecs (stdlib only).

Operator preferences retain the Phase-28 lenient read contract. Financial
continuity is different: only a *missing* file means a fresh account; corrupt,
non-finite, or unknown-version data raises StateError, never resets risk.
Writes use private, unique same-directory files, fsync, then atomic replace.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from .compat import windows_long_path

log = logging.getLogger("cybertrade.statestore")
VERSION = 1
CONTINUITY_VERSION = 1
MAX_STATE_BYTES = 8_000_000


class StateError(ValueError):
    """Recovery requires operator attention; do not overwrite the evidence."""


def finite(value: Any, name: str, minimum: float = 0.0) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise StateError(f"invalid {name}")
    return float(value)


def integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise StateError(f"invalid {name}")
    return value


def text(value: Any, name: str, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 4096 or (required and not value):
        raise StateError(f"invalid {name}")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StateError(f"duplicate state field: {key}")
        result[key] = value
    return result


def _bad_constant(value):
    raise StateError(f"non-finite JSON number: {value}")


def _json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise StateError("non-finite JSON exponent")
    return number


def _read(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise StateError("state file too large")
    data = json.loads(raw, object_pairs_hook=_object, parse_constant=_bad_constant,
                      parse_float=_json_float)
    if not isinstance(data, dict):
        raise StateError("state must be a JSON object")
    return data


def load_state(path: str) -> Dict[str, Any]:
    """Lenient reads for preferences/heartbeats, NOT financial recovery."""
    if not path:
        return {}
    try:
        return _read(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        log.warning("state unreadable: %s", path)
        return {}


def save_state(path: str, data: Dict[str, Any], *, durable: bool = True) -> bool:
    """Private atomic JSON replacement; failed serialization leaves old data intact."""
    if not path:
        return False
    tmp = None
    try:
        raw = json.dumps(dict(data), sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
            raise StateError("state file too large")
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
            fh.flush()
            if durable:
                os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
        if durable and os.name == "posix":
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return True
    except (OSError, ValueError, TypeError):
        log.exception("state save failed: %s", path)
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def load_operator_state(path: str) -> Dict[str, Any]:
    return load_state(path)


def save_operator_state(path: str, data: Dict[str, Any]) -> bool:
    return save_state(path, {**data, "version": VERSION})


def load_continuity(path: str) -> Dict[str, Any]:
    """Missing -> {}; anything present but invalid -> fail closed."""
    if not path:
        return {}
    try:
        data = _read(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError) as exc:
        raise StateError(f"continuity unreadable: {path}") from exc
    if type(data.get("version")) is not int or data["version"] != CONTINUITY_VERSION:
        raise StateError("unsupported continuity version")
    finite(data.get("saved_ts"), "saved_ts")
    return data


def save_continuity(path: str, payload: Dict[str, Any]) -> bool:
    return save_state(path, {**payload, "version": CONTINUITY_VERSION,
                             "saved_ts": time.time()})


class StateLease:
    """One writer per state file. OS locks are released even after SIGKILL.

    The .lock inode is deliberately never unlinked (avoids split lock races).
    An old PID in the file is informational, not proof of ownership.
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.realpath(os.path.expanduser(path)) + ".lock"
        self._fh = None

    def acquire(self) -> None:
        if self._fh is not None:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(windows_long_path(self.path),
                     os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    fh.write(b" ")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0)
            fh.write(str(os.getpid()).encode("ascii"))
            fh.truncate()
            fh.flush()
        except OSError as exc:
            fh.close()
            raise StateError(f"state already in use or not lockable: {self.path}") from exc
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


# -- no generated IDs, guessed strikes, or dropped corrupt rows on recovery --
def pack_position(pos: Any) -> Dict[str, Any]:
    row = asdict(pos)
    row["fill"]["side"] = pos.side.value
    return row


def unpack_position(data: Any) -> Optional[Any]:
    """Invalid -> None; callers must reject the WHOLE book if any row is bad."""
    from .constants import Side
    from .data.models import Fill, Position
    try:
        raw = data["fill"]
        side = Side(raw["side"])
        if not side.is_trade:
            raise StateError("position side must be call/put")
        fill = Fill(
            id=text(raw["id"], "fill.id", True),
            order_id=text(raw["order_id"], "order_id", True),
            asset=text(raw["asset"], "asset", True), side=side,
            price=finite(raw["price"], "strike", 1e-12),
            amount=finite(raw["amount"], "stake", 1e-12),
            payout=finite(raw["payout"], "payout"),
            ts=finite(raw["ts"], "fill.ts"),
            fee=finite(raw["fee"], "fee"),
            slippage=finite(raw["slippage"], "slippage"),
            broker_id=text(raw["broker_id"], "broker_id"),
        )
        expiry = finite(data["expiry_ts"], "expiry_ts", fill.ts)
        if fill.payout > 1 or expiry <= fill.ts:
            raise StateError("invalid payout/expiry")
        return Position(id=text(data["id"], "position.id", True), fill=fill,
                        expiry_ts=expiry, strategy=text(data["strategy"], "strategy"),
                        label=text(data["label"], "label"))
    except (KeyError, TypeError, ValueError):
        return None


def pack_order(order: Any) -> Dict[str, Any]:
    row = asdict(order)
    for name in ("side", "order_type", "status"):
        row[name] = getattr(order, name).value
    # Persist execution attribution, not arbitrary plugin/session metadata.
    row["meta"] = {key: value for key, value in row["meta"].items()
                   if key in ("cluster", "regime", "confidence", "votes", "slippage_bps")}
    return row


def unpack_order(row: Any) -> Any:
    from .constants import OrderStatus, OrderType, Side
    from .data.models import Order
    try:
        if not isinstance(row, dict) or not isinstance(row.get("meta"), dict):
            raise StateError("invalid order metadata")
        order = Order(
            id=text(row["id"], "order.id", True),
            asset=text(row["asset"], "asset", True), side=Side(row["side"]),
            amount=finite(row["amount"], "stake", 1e-12),
            order_type=OrderType(row["order_type"]),
            expiry_seconds=integer(row["expiry_seconds"], "expiry_seconds"),
            payout=finite(row["payout"], "payout"),
            limit_price=finite(row["limit_price"], "limit_price"),
            ts=finite(row["ts"], "order.ts"), status=OrderStatus(row["status"]),
            broker_id=text(row["broker_id"], "broker_id"),
            tag=text(row["tag"], "tag"), strategy=text(row["strategy"], "strategy"),
            meta=dict(row["meta"]),
        )
        if (not order.side.is_trade or order.expiry_seconds <= 0 or order.payout > 1
                or order.order_type is not OrderType.BINARY):
            raise StateError("invalid order")
        text(order.meta.get("cluster", ""), "cluster")
        text(order.meta.get("regime", ""), "regime")
        finite(order.meta.get("confidence", 0.0), "confidence")
        votes = order.meta.get("votes", [])
        if not isinstance(votes, list) or any(not isinstance(v, dict) for v in votes):
            raise StateError("invalid votes")
        return order
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("invalid saved order") from exc


def pack_settlement(settlement: Any) -> Dict[str, Any]:
    row = asdict(settlement)
    row["side"] = settlement.side.value
    return row


def unpack_settlement(row: Any) -> Any:
    from .constants import Side
    from .data.models import Settlement
    try:
        data = dict(row)
        for name in ("id", "fill_id", "order_id", "asset"):
            text(data[name], name, True)
        for name in ("strike", "expiry_price", "stake", "payout", "salvage", "ts"):
            finite(data[name], name)
        for name in ("won", "refunded"):
            if type(data[name]) is not bool:
                raise StateError(f"invalid {name}")
        data["side"] = Side(data["side"])
        if not data["side"].is_trade or data["stake"] <= 0 or data["payout"] > 1:
            raise StateError("invalid settlement")
        return Settlement(**data)
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("invalid saved settlement") from exc


def write_heartbeat(path: str, *, ts: Optional[float] = None, state: str = "",
                    cycle: int = 0, run_id: str = "",
                    extra: Optional[Dict[str, Any]] = None) -> bool:
    payload = dict(extra or {})
    payload.update(version=VERSION, ts=float(time.time() if ts is None else ts),
                   state=state, cycle=cycle, pid=os.getpid(), run_id=run_id)
    return save_state(path, payload, durable=False)


def read_heartbeat(path: str) -> Dict[str, Any]:
    return load_state(path)


def heartbeat_age(payload: Dict[str, Any], now: Optional[float] = None) -> Optional[float]:
    """Diagnostic age (supervision uses advancing sequences + monotonic time)."""
    try:
        ts = finite(payload.get("ts"), "heartbeat.ts", 1e-12)
        now = time.time() if now is None else finite(now, "now")
        return max(0.0, now - ts)
    except (AttributeError, StateError):
        return None
