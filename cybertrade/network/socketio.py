"""Engine.IO v3 + Socket.IO packet codec (the dialect Quotex's gateway speaks).

Engine.IO v3 framing (text mode):
    ``<type><payload>``  e.g. ``2`` ping, ``3`` pong, ``4`` message
Socket.IO rides on Engine.IO messages:
    ``<eio=4><sio-type>[namespace,][ack-id,]JSON-data``
    e.g. ``40`` connect to "/", ``42["event",{"a":1}]`` event,
         ``43<id>[...]`` ack, ``44{...}`` error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple, Union

from ..constants import (
    EIO_CLOSE,
    EIO_MESSAGE,
    EIO_NOOP,
    EIO_OPEN,
    EIO_PING,
    EIO_PONG,
    SIO_ACK,
    SIO_BINARY_ACK,
    SIO_BINARY_EVENT,
    SIO_CONNECT,
    SIO_DISCONNECT,
    SIO_ERROR,
    SIO_EVENT,
)
from ..exceptions import ProtocolError
from ..utils.jsonx import dumps, loads


@dataclass
class EnginePacket:
    type: str                 # "0".."6"
    payload: str = ""
    binary: bytes = b""


@dataclass
class SocketPacket:
    type: str                 # "0".."6"
    namespace: str = "/"
    id: Optional[int] = None
    data: Any = None
    attachments: Optional[int] = None  # binary frames that follow (type "5"/"6")


@dataclass
class EngineIOHandshake:
    sid: str = ""
    ping_interval: float = 25.0
    ping_timeout: float = 5.0
    upgrades: List[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Engine.IO layer
# ---------------------------------------------------------------------------
def encode_engine(packet: EnginePacket) -> str:
    if packet.type == EIO_MESSAGE and packet.payload.startswith(tuple("0123456")):
        # message already contains its socket.io layer
        return packet.type + packet.payload
    return packet.type + packet.payload


def decode_engine(raw: str) -> EnginePacket:
    if not raw:
        raise ProtocolError("empty engine.io packet")
    ptype = raw[0]
    if ptype not in (EIO_OPEN, EIO_CLOSE, EIO_PING, EIO_PONG, EIO_MESSAGE, EIO_UPGRADE_STR, EIO_NOOP):
        raise ProtocolError(f"unknown engine.io type {ptype!r}")
    return EnginePacket(type=ptype, payload=raw[1:])


EIO_UPGRADE_STR = "5"


def parse_handshake(raw: str) -> EngineIOHandshake:
    """Parse the ``0{"sid":...}`` engine.io open packet."""
    if not raw.startswith(EIO_OPEN):
        raise ProtocolError(f"expected engine.io open packet, got {raw[:20]!r}")
    data = loads(raw[1:], default={})
    if not isinstance(data, dict) or "sid" not in data:
        raise ProtocolError("malformed engine.io handshake")
    return EngineIOHandshake(
        sid=str(data["sid"]),
        ping_interval=float(data.get("pingInterval", 25000)) / 1000.0
        if data.get("pingInterval", 25000) > 1000
        else float(data.get("pingInterval", 25.0)),
        ping_timeout=float(data.get("pingTimeout", 5000)) / 1000.0
        if data.get("pingTimeout", 5000) > 100
        else float(data.get("pingTimeout", 5.0)),
        upgrades=list(data.get("upgrades", [])),
        raw=data,
    )


def encode_ping() -> str:
    return EIO_PING


def encode_pong(payload: str = "") -> str:
    return EIO_PONG + payload


# ---------------------------------------------------------------------------
# Socket.IO layer
# ---------------------------------------------------------------------------
def encode_socket(packet: SocketPacket) -> str:
    """Encode a socket.io packet onto an engine.io MESSAGE payload string."""
    out = [packet.type]
    if packet.namespace and packet.namespace != "/":
        out.append(packet.namespace + ",")
    if packet.id is not None:
        out.append(str(packet.id))
    if packet.data is not None:
        out.append(dumps(packet.data, sort_keys=False))
    elif packet.type in (SIO_EVENT, SIO_ACK, SIO_ERROR):
        out.append("null")
    return "".join(out)


def decode_socket(payload: str) -> SocketPacket:
    """Decode the socket.io layer of an engine.io message payload."""
    if not payload:
        raise ProtocolError("empty socket.io packet")
    ptype = payload[0]
    if ptype not in (SIO_CONNECT, SIO_DISCONNECT, SIO_EVENT, SIO_ACK,
                     SIO_ERROR, SIO_BINARY_EVENT, SIO_BINARY_ACK):
        raise ProtocolError(f"unknown socket.io packet type {ptype!r}")
    rest = payload[1:]
    namespace = "/"
    ack_id: Optional[int] = None
    attachments: Optional[int] = None

    if ptype in (SIO_BINARY_EVENT, SIO_BINARY_ACK):
        # binary packets prefix the attachment count: `51-[...]` means one
        # binary frame follows the JSON envelope.
        dash = rest.find("-")
        if dash != -1 and rest[:dash].isdigit():
            attachments = int(rest[:dash])
            rest = rest[dash + 1 :]

    if rest.startswith("/"):
        idx = rest.find(",")
        if idx == -1:
            namespace = rest
            rest = ""
        else:
            namespace = rest[:idx]
            rest = rest[idx + 1 :]

    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        else:
            break
    if digits:
        ack_id = int(digits)
        rest = rest[len(digits) :]

    data: Any = None
    if rest:
        data = loads(rest, default=None)
        if data is None and rest.strip() not in ("", "null"):
            raise ProtocolError(f"bad socket.io json: {rest[:40]!r}")
    return SocketPacket(type=ptype, namespace=namespace, id=ack_id,
                            data=data, attachments=attachments)


def encode_event(name: str, *args: Any, ack_id: Optional[int] = None,
                 namespace: str = "/") -> str:
    """Full wire string for emitting a socket.io event over EIO v3."""
    payload = encode_socket(
        SocketPacket(type=SIO_EVENT, namespace=namespace, id=ack_id, data=[name, *args])
    )
    return EIO_MESSAGE + payload


def encode_connect(namespace: str = "/") -> str:
    return EIO_MESSAGE + encode_socket(SocketPacket(type=SIO_CONNECT, namespace=namespace))


def encode_disconnect(namespace: str = "/") -> str:
    return EIO_MESSAGE + encode_socket(SocketPacket(type=SIO_DISCONNECT, namespace=namespace))


def event_name(packet: SocketPacket) -> Optional[str]:
    """Extract (name, args) from an event packet."""
    if packet.type not in (SIO_EVENT, SIO_BINARY_EVENT):
        return None
    data = packet.data
    if isinstance(data, list) and data and isinstance(data[0], str):
        return data[0]
    return None


def event_args(packet: SocketPacket) -> List[Any]:
    if packet.type not in (SIO_EVENT, SIO_BINARY_EVENT):
        return []
    data = packet.data
    if isinstance(data, list):
        return list(data[1:])
    return []


@dataclass
class EngineIOSession:
    """Codec state machine for one connection: handles ping/pong bookkeeping."""

    handshake: Optional[EngineIOHandshake] = None
    sid: str = ""

    def on_raw(self, raw: str) -> Tuple[Optional[EnginePacket], Optional[SocketPacket]]:
        """Feed a raw websocket text frame. Returns packets to act on.

        Automatically answers nothing — the caller sends pongs via
        :meth:`pong` so it can use its own transport.
        """
        eng = decode_engine(raw)
        if eng.type == EIO_OPEN:
            self.handshake = parse_handshake(raw)
            self.sid = self.handshake.sid
            return eng, None
        if eng.type == EIO_PING:
            return eng, None
        if eng.type == EIO_PONG:
            return eng, None
        if eng.type == EIO_CLOSE:
            return eng, None
        if eng.type == EIO_MESSAGE:
            return eng, decode_socket(eng.payload)
        return eng, None


__all__ = [
    "EnginePacket",
    "SocketPacket",
    "EngineIOHandshake",
    "EngineIOSession",
    "encode_engine",
    "decode_engine",
    "parse_handshake",
    "encode_ping",
    "encode_pong",
    "encode_socket",
    "decode_socket",
    "encode_event",
    "encode_connect",
    "encode_disconnect",
    "event_name",
    "event_args",
]
