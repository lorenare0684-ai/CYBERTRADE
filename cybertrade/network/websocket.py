"""RFC 6455 WebSocket client built on the standard library.

Implements the client half of the WebSocket protocol: opening handshake,
masked frames, fragmentation, ping/pong, and clean close.  No third-party
dependencies — sockets + ssl + base64 + struct only.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import socket
import ssl
import struct
import threading
import time
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

from ..exceptions import NetworkError, ProtocolError

log = logging.getLogger("cybertrade.websocket")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_RECV_CHUNK = 4096


def _mask_payload(payload: bytes, key: bytes) -> bytes:
    """XOR-mask a frame payload with the 4-byte masking key."""
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def encode_frame(
    payload: bytes,
    opcode: int = OP_TEXT,
    *,
    fin: bool = True,
    mask: bool = True,
) -> bytes:
    """Serialize one WebSocket frame (client side: masked by default)."""
    header = bytearray()
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    header.append(first)
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header += struct.pack(">H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", length)
    if mask:
        key = os.urandom(4)
        header += key
        payload = _mask_payload(payload, key)
    return bytes(header) + payload


def host_header_value(scheme: str, host: str, port: int) -> str:
    """Host header value with browser parity (RFC 7230 §5.4).

    Browsers omit the default port; sending ``ws2.qxbroker.com:443`` is a
    needless fingerprint and some front-ends match Host exactly.
    """
    default = (scheme == "wss" and port == 443) or (scheme == "ws" and port == 80)
    return host if default else f"{host}:{port}"


class Frame:
    __slots__ = ("fin", "opcode", "payload")

    def __init__(self, fin: bool, opcode: int, payload: bytes) -> None:
        self.fin = fin
        self.opcode = opcode
        self.payload = payload

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Frame fin={self.fin} op={self.opcode} len={len(self.payload)}>"


class WebSocketConnection:
    """Synchronous, thread-safe WebSocket client connection."""

    def __init__(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        cookie: str = "",
        origin: str = "",
        timeout: float = 15.0,
        subprotocols: Tuple[str, ...] = (),
    ) -> None:
        self.url = url
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise NetworkError(f"unsupported websocket scheme: {parsed.scheme}")
        self.scheme = parsed.scheme
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or (443 if self.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.headers = dict(headers or {})
        self.cookie = cookie
        self.origin = origin
        self.timeout = timeout
        self.subprotocols = subprotocols

        self.sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self.closed = True
        self.close_code: Optional[int] = None
        self.close_reason = ""

    # -- handshake ---------------------------------------------------------
    def connect(self) -> None:
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
            if self.scheme == "wss":
                ctx = ssl.create_default_context()
                self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
            else:
                self.sock = raw
        except OSError as exc:
            raise NetworkError(f"tcp connect failed {self.host}:{self.port}: {exc}") from exc

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {host_header_value(self.scheme, self.host, self.port)}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.cookie:
            lines.append(f"Cookie: {self.cookie}")
        if self.origin:
            lines.append(f"Origin: {self.origin}")
        if self.subprotocols:
            lines.append("Sec-WebSocket-Protocol: " + ", ".join(self.subprotocols))
        for name, value in self.headers.items():
            lines.append(f"{name}: {value}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

        try:
            self.sock.sendall(request)
            raw_response = self._read_http_response()
        except OSError as exc:
            self.sock.close()
            raise NetworkError(f"handshake send/recv failed: {exc}") from exc

        status, resp_headers, _ = _parse_http(raw_response)
        if status != 101:
            self.sock.close()
            raise NetworkError(f"handshake rejected: HTTP {status}")
        accept = resp_headers.get("sec-websocket-accept", "")
        expected = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected:
            self.sock.close()
            raise ProtocolError("Sec-WebSocket-Accept mismatch (proxy interference?)")
        self.closed = False
        self.sock.settimeout(self.timeout)
        log.debug("websocket connected %s", self.url)

    def _read_http_response(self) -> bytes:
        assert self.sock is not None
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(_RECV_CHUNK)
            if not chunk:
                raise NetworkError("connection closed during handshake")
            data += chunk
            if len(data) > 64 * 1024:
                raise ProtocolError("oversized handshake response")
        head, _, rest = bytes(data).partition(b"\r\n\r\n")
        # The venue routinely flushes the Engine.IO open frame in the same
        # TCP segment as the 101 headers. Those trailing bytes belong to the
        # frame reader — dropping them ate the handshake on every connect.
        self._buf += rest
        return head + b"\r\n\r\n"

    # -- frames ------------------------------------------------------------
    def send(self, data: str | bytes, opcode: Optional[int] = None) -> None:
        if self.closed or self.sock is None:
            raise NetworkError("websocket is closed")
        if isinstance(data, str):
            payload = data.encode("utf-8")
            opcode = OP_TEXT if opcode is None else opcode
        else:
            payload = data
            opcode = OP_BINARY if opcode is None else opcode
        frame = encode_frame(payload, opcode, mask=True)
        with self._send_lock:
            try:
                self.sock.sendall(frame)
            except OSError as exc:
                self.closed = True
                raise NetworkError(f"send failed: {exc}") from exc

    def ping(self, payload: bytes = b"") -> None:
        self.send(payload, OP_PING)

    def recv_frame(self) -> Frame:
        """Read exactly one frame from the wire."""
        header = self._read_exact(2)
        b1, b2 = header[0], header[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if length > 64 * 1024 * 1024:
            raise ProtocolError("frame too large")
        key = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = _mask_payload(payload, key)
        return Frame(fin, opcode, payload)

    def recv_message(self) -> Tuple[int, bytes]:
        """Read one full message (handles continuation frames and control)."""
        while True:
            frame = self.recv_frame()
            if frame.opcode == OP_PING:
                try:
                    self.send(frame.payload, OP_PONG)
                except NetworkError:
                    pass
                continue
            if frame.opcode == OP_PONG:
                continue
            if frame.opcode == OP_CLOSE:
                code, reason = 1000, b""
                if len(frame.payload) >= 2:
                    code = struct.unpack(">H", frame.payload[:2])[0]
                    reason = frame.payload[2:]
                self.close_code = code
                self.close_reason = reason.decode("utf-8", errors="replace")
                self.closed = True
                raise NetworkError(f"websocket closed by peer ({code})")
            # data frame — assemble fragments
            opcode = frame.opcode
            payload = bytearray(frame.payload)
            while not frame.fin:
                frame = self.recv_frame()
                if frame.opcode in (OP_PING, OP_PONG):
                    continue
                if frame.opcode == OP_CLOSE:
                    self.closed = True
                    raise NetworkError("websocket closed mid-fragment")
                payload += frame.payload
            return opcode, bytes(payload)

    def recv_text(self) -> str:
        opcode, payload = self.recv_message()
        return payload.decode("utf-8", errors="replace")

    def _read_exact(self, n: int) -> bytes:
        assert self.sock is not None
        with self._recv_lock:
            while len(self._buf) < n:
                try:
                    chunk = self.sock.recv(_RECV_CHUNK)
                except socket.timeout as exc:
                    raise NetworkError("recv timeout") from exc
                except OSError as exc:
                    self.closed = True
                    raise NetworkError(f"recv failed: {exc}") from exc
                if not chunk:
                    self.closed = True
                    raise NetworkError("connection closed")
                self._buf += chunk
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    # -- lifecycle ---------------------------------------------------------
    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed or self.sock is None:
            return
        try:
            payload = struct.pack(">H", code) + reason.encode("utf-8")
            self.sock.sendall(encode_frame(payload, OP_CLOSE, mask=True))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.closed = True

    def settimeout(self, seconds: float) -> None:
        self.timeout = seconds
        if self.sock is not None:
            self.sock.settimeout(seconds)

    def __enter__(self) -> "WebSocketConnection":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _parse_http(raw: bytes) -> Tuple[int, dict, bytes]:
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    status = 0
    headers: dict = {}
    if lines:
        parts = lines[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
    return status, headers, rest


__all__ = [
    "WebSocketConnection",
    "Frame",
    "encode_frame",
    "host_header_value",
    "OP_TEXT",
    "OP_BINARY",
    "OP_CLOSE",
    "OP_PING",
    "OP_PONG",
]
