"""Minimal HTTP/1.1 client with cookie jar (stdlib sockets + ssl).

Purpose-built for the Quotex website session flow (sign-in → cookie → WS
upgrade headers) and for health checks.  Not a general-purpose requests
replacement — deliberately small and auditable.
"""

from __future__ import annotations

import gzip
import json
import logging
import socket
import ssl
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse

from ..exceptions import NetworkError

log = logging.getLogger("cybertrade.http")

MAX_BODY = 32 * 1024 * 1024


@dataclass
class HttpResponse:
    status: int
    headers: Dict[str, str]
    body: bytes
    url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self, default=None):
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return default

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class CookieJar:
    cookies: Dict[str, str] = field(default_factory=dict)

    def set_from_header(self, value: str) -> None:
        for chunk in value.split(","):
            pair = chunk.split(";")[0].strip()
            if "=" in pair:
                name, val = pair.split("=", 1)
                self.cookies[name.strip()] = val.strip()

    def header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def update_from_response(self, headers: Dict[str, str]) -> None:
        # http.client folds multiple Set-Cookie into one; accept both shapes
        raw = headers.get("set-cookie", "")
        if raw:
            self.set_from_header(raw)

    def __bool__(self) -> bool:
        return bool(self.cookies)


class HttpClient:
    """One persistent connection per request (Connection: close semantics)."""

    def __init__(
        self,
        base_url: str = "",
        user_agent: str = "cybertrade/1.0",
        timeout: float = 15.0,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.default_headers = dict(headers or {})
        self.jar = CookieJar()
        self._lock = threading.RLock()

    # -- request -----------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        json_body=None,
        form: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        absolute: bool = False,
    ) -> HttpResponse:
        url = path if absolute else f"{self.base_url}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise NetworkError(f"unsupported scheme {parsed.scheme!r}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query

        body = b""
        content_type = ""
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        elif form is not None:
            body = urlencode(form).encode("utf-8")
            content_type = "application/x-www-form-urlencoded"

        req_headers = {
            "Host": parsed.netloc,
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Connection": "close",
            "Accept-Encoding": "gzip",
        }
        req_headers.update(self.default_headers)
        if content_type:
            req_headers["Content-Type"] = content_type
        if body:
            req_headers["Content-Length"] = str(len(body))
        with self._lock:
            if self.jar:
                req_headers["Cookie"] = self.jar.header()
        if headers:
            req_headers.update(headers)

        raw_req = [f"{method.upper()} {target} HTTP/1.1"]
        for k, v in req_headers.items():
            raw_req.append(f"{k}: {v}")
        raw_req.append("")
        raw_req.append("")
        request_bytes = "\r\n".join(raw_req).encode("iso-8859-1") + body

        try:
            with self._open_socket(parsed.scheme, host, port) as sock:
                sock.sendall(request_bytes)
                response_bytes = _recv_all(sock)
        except OSError as exc:
            raise NetworkError(f"request to {url} failed: {exc}") from exc

        response = _parse_response(response_bytes, url)
        with self._lock:
            self.jar.update_from_response(response.headers)
            # tolerate comma-folded multi-cookie headers
            for key, value in response.headers.items():
                if key == "set-cookie" and "," in value and "=" in value.split(",")[0]:
                    self.jar.set_from_header(value)
        return response

    def _open_socket(self, scheme: str, host: str, port: int):
        raw = socket.create_connection((host, port), timeout=self.timeout)
        if scheme == "https":
            ctx = ssl.create_default_context()
            return ctx.wrap_socket(raw, server_hostname=host)
        return raw

    # -- sugar -------------------------------------------------------------
    def get(self, path: str, **kw) -> HttpResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> HttpResponse:
        return self.request("POST", path, **kw)

    def ping(self) -> bool:
        try:
            resp = self.get("/", headers={"Accept": "text/html"})
            return resp.status > 0
        except NetworkError:
            return False


def _recv_all(sock: socket.socket) -> bytes:
    chunks = bytearray()
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        chunks += chunk
        if len(chunks) > MAX_BODY:
            raise NetworkError("response too large")
        # fast path: if headers say content-length, stop when satisfied
        if b"\r\n\r\n" in chunks:
            head, _, rest = bytes(chunks).partition(b"\r\n\r\n")
            headers = _headers_from(head)
            if headers.get("transfer-encoding", "").lower() == "chunked":
                if rest.endswith(b"0\r\n\r\n"):
                    break
            else:
                clen = headers.get("content-length")
                if clen is not None and clen.isdigit() and len(rest) >= int(clen):
                    break
    return bytes(chunks)


def _headers_from(head: bytes) -> Dict[str, str]:
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            value = v.strip()
            if key in headers:
                headers[key] += ", " + value
            else:
                headers[key] = value
    return headers


def _parse_response(raw: bytes, url: str) -> HttpResponse:
    head, sep, rest = raw.partition(b"\r\n\r\n")
    if not sep:
        raise NetworkError("malformed HTTP response")
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    status = 0
    if lines:
        parts = lines[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers = _headers_from(head)

    body = rest
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(rest)
    elif headers.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    return HttpResponse(status=status, headers=headers, body=body, url=url)


def _dechunk(data: bytes) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(data):
        nl = data.find(b"\r\n", pos)
        if nl == -1:
            break
        try:
            size = int(data[pos:nl].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        start = nl + 2
        out += data[start : start + size]
        pos = start + size + 2
    return bytes(out)


__all__ = ["HttpClient", "HttpResponse", "CookieJar"]
