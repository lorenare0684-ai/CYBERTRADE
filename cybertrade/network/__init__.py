"""Network subpackage: WebSocket, Engine.IO/Socket.IO codec, HTTP client."""

from __future__ import annotations

from .http_client import CookieJar, HttpClient, HttpResponse
from .socketio import (
    EngineIOHandshake,
    EngineIOSession,
    EnginePacket,
    SocketPacket,
    decode_engine,
    decode_socket,
    encode_connect,
    encode_disconnect,
    encode_engine,
    encode_event,
    encode_ping,
    encode_pong,
    encode_socket,
    event_args,
    event_name,
    parse_handshake,
)
from .websocket import Frame, WebSocketConnection, encode_frame

__all__ = [
    "HttpClient",
    "HttpResponse",
    "CookieJar",
    "WebSocketConnection",
    "Frame",
    "encode_frame",
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
