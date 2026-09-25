"""Capture the browser tab's own Quotex wire over DevTools (ground truth).

When the Python wire starves, the decisive question is "what does the
working tab do differently?" — message order, timing, exact Socket.IO
strings.  This module attaches to the running paired Chrome (the same
DevTools port pairing uses), enables the Network domain on the trade
page target, and records websocket frames for a bounded window.

No session, no credentials, no venue account needed: it only observes
the tab the operator already opened.  Reads ``Network.webSocket*``
events — available on every desktop Chrome build.  Distinct page HTTP
URLs are collected too (capped): if the tab fetches anything over
plain HTTP, that shows up as well.  Only URLs and frame payloads are
kept — never cookies or headers.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .pairing import devtools_page_ws

log = logging.getLogger("cybertrade.qx.sniff")

_WS_METHODS = {
    "Network.webSocketCreated",
    "Network.webSocketClosed",
    "Network.webSocketFrameSent",
    "Network.webSocketFrameReceived",
}


def sniff_frames(
    port: int,
    duration: float = 20.0,
    fetch: Optional[Callable[..., Any]] = None,
    ws_factory: Optional[Callable[..., Any]] = None,
) -> List[Dict[str, Any]]:
    """Record trade-tab websocket + HTTP traffic for ``duration`` seconds.

    Requires the paired Chrome to be running with a visible tab (start
    pairing first) — the browser-level target sees no page traffic, so a
    missing page socket is an error, not a fallback.
    """
    ws_url = devtools_page_ws(port, fetch=fetch)
    if not ws_url:
        raise RuntimeError(
            f"no trade tab on DevTools port {port} — start pairing (or open "
            f"the trade page in the paired Chrome) and run sniff again"
        )
    if ws_factory is None:
        from ...network.websocket import WebSocketConnection

        def ws_factory(url, **kw):  # noqa: E306 — local default
            return WebSocketConnection(url, **kw)
    ws = ws_factory(ws_url, timeout=2.0)
    ws.connect()
    frames: List[Dict[str, Any]] = []
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        start = time.monotonic()
        end = start + max(1.0, float(duration))
        while time.monotonic() < end:
            try:
                raw = ws.recv_text()
            except Exception:  # noqa: BLE001 — read timeout → check deadline
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            method = msg.get("method", "")
            if method not in _WS_METHODS and method != "Network.requestWillBeSent":
                continue
            params = msg.get("params", {}) or {}
            frames.append({"t": time.monotonic() - start,
                           "method": method, "params": params})
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    return frames


def _payload(params: Dict[str, Any]) -> str:
    resp = params.get("response", {}) or {}
    data = resp.get("payloadData", "")
    if resp.get("opcode") == 2 and isinstance(data, str):
        # CDP base64-encodes binary frame payloads.
        try:
            raw = base64.b64decode(data)
            return raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — fall through to raw text
            pass
    return str(data)


def _classify_s2c(payload: str) -> str:
    text = payload.strip()
    if text.startswith("0{"):
        return "EIO open"
    if text == "40":
        return "SIO connect"
    if text.startswith("44"):
        return f"error {text[2:100]}"
    if text.startswith("451-"):
        rest = text[4:120].replace('"', "")
        return f"binary placeholder {rest}"
    if text.startswith("42["):
        try:
            name = (json.loads(text[2:]) or ["?"])[0]
        except ValueError:
            name = "?"
        return f'event "{name}"'
    if text.startswith("[["):
        try:
            rows = json.loads(text)
            return f"quote batch ({len(rows)} rows)"
        except ValueError:
            return "quote batch (?)"
    if text.startswith("{"):
        try:
            keys = ",".join(sorted(json.loads(text).keys())[:6])
        except ValueError:
            keys = "?"
        return f"bare dict {{{keys}}}"
    if text in ("2", "3"):
        return "EIO ping/pong"
    return text[:60] or "(empty)"


def summarize(frames: List[Dict[str, Any]]) -> str:
    """Turn captured CDP traffic into a printable wire report."""
    out = ["  ── trade-tab wire ──────────────────────────────"]
    if not frames:
        out.append("  (no websocket or HTTP traffic captured — is the tab on "
                   "the trade page and logged in?)")
        return "\n".join(out)
    c2s: Dict[str, int] = {}
    s2c: Dict[str, int] = {}
    urls: List[str] = []
    shown = 0
    for frame in frames:
        method = frame.get("method", "")
        if method == "Network.requestWillBeSent":
            req = (frame.get("params", {}) or {}).get("request", {}) or {}
            url = str(req.get("url", ""))
            if url and url not in urls and len(urls) < 40:
                urls.append(f"{req.get('method', 'GET')} {url[:130]}")
            continue
        if method == "Network.webSocketCreated":
            url = str((frame.get("params", {}) or {}).get("url", ""))[:130]
            out.append(f"  +{frame['t']:6.2f}s socket open {url}")
            continue
        if method == "Network.webSocketClosed":
            out.append(f"  +{frame['t']:6.2f}s socket closed")
            continue
        sent = method == "Network.webSocketFrameSent"
        payload = _payload(frame.get("params", {}) or {})
        if sent:
            if payload.startswith("42["):
                try:
                    name = (json.loads(payload[2:]) or ["?"])[0]
                except ValueError:
                    name = "?"
                label = f'C2S event "{name}"'
            elif payload in ("2", "3"):
                label = "C2S EIO ping/pong"
            else:
                label = f"C2S {payload[:60]}"
            c2s[label] = c2s.get(label, 0) + 1
            detail = payload[2:150] if payload.startswith("42[") else payload[:120]
        else:
            label = f"S2C {_classify_s2c(payload)}"
            s2c[label] = s2c.get(label, 0) + 1
            detail = payload[:150]
        if shown < 60:
            shown += 1
            out.append(f"  +{frame['t']:6.2f}s {label} {detail}")
    if shown >= 60:
        out.append(f"  … ({len(frames) - shown} more frames, counts below)")
    out.append("  ── C2S (tab → venue) ─────────────────────────")
    for label in sorted(c2s):
        out.append(f"  {c2s[label]:5d}× {label}")
    out.append("  ── S2C (venue → tab) ─────────────────────────")
    for label in sorted(s2c):
        out.append(f"  {s2c[label]:5d}× {label}")
    if urls:
        out.append("  ── page HTTP ───────────────────────────────")
        out.extend(f"  {u}" for u in urls)
    return "\n".join(out)


def sniff_and_report(port: int, duration: float = 20.0,
                     fetch: Optional[Callable[..., Any]] = None,
                     ws_factory: Optional[Callable[..., Any]] = None) -> str:
    """Capture the tab wire and render the report (CLI entry point)."""
    return summarize(sniff_frames(port, duration, fetch, ws_factory))


__all__ = ["sniff_frames", "summarize", "sniff_and_report"]
