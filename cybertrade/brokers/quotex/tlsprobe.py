"""Transport shootout: stdlib TLS vs Chrome-impersonated TLS on the venue wire.

Decisive diagnostic for a starving wire.  The venue answers some
connections with ``s_authorization`` and then sends no data; the prime
suspect is handshake gating on the TLS/JA3 fingerprint (pyquotex moved
to curl_cffi ``impersonate="chrome"`` for exactly these anti-blocking
reasons).  This probe runs the SAME Socket.IO script — ``40``, auth,
bootstrap, canary trio, tick — over both transports back-to-back and
reports which leg the venue streams to:

- chrome streams, stdlib starves → TLS-gated (transport switch needed);
- both stream → the starve is intermittent/session-side;
- both starve → not TLS (account, session, or venue-side gating).

The chrome leg needs the optional ``curl_cffi`` package (``python -m
pip install curl_cffi``); without it the probe still runs the stdlib
leg and says how to complete the comparison.  Nothing here trades,
switches purses, or writes state — it only listens.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import constants as C
from .protocol import (
    build_chart_notification,
    build_depth_follow,
    build_drawing_load,
    build_indicator_list,
    build_instruments,
    build_pending_list,
    build_subscribe_candles,
    build_tick,
)
from .sniff import classify_s2c

log = logging.getLogger("cybertrade.qx.tlsprobe")

CANARY_ASSET = "EURUSD_otc"
CANARY_TF = 60

# Dispatched/socket event names that prove the venue streams data.
DATA_EVENTS = frozenset({
    "quotes", "tick", "quote", "candle", "candle-generated",
    "candles", "history/load", "history/list/v2",
    "balance", "balanceUpdate",
    *C.INSTRUMENT_EVENTS,
})


def script_frames() -> List[str]:
    """The exact connect burst both legs send (order matters)."""
    return [
        build_indicator_list(),
        build_drawing_load(),
        build_pending_list(),
        build_chart_notification(),
        build_instruments(),
        build_subscribe_candles(CANARY_ASSET, CANARY_TF),
        build_chart_notification(CANARY_ASSET),
        build_depth_follow(CANARY_ASSET),
        build_tick(),
    ]


def is_data_label(label: str) -> bool:
    """True when a classified S2C frame proves data flows."""
    if label.startswith("quote batch"):
        return True
    if label.startswith("bare dict {"):
        keys = label[len("bare dict {"):]
        return any(k in keys for k in ("Balance", "balance", "candles",
                                       "deals", "quotes"))
    if label.startswith('event "') and label.endswith('"'):
        return label[len('event "'):-1] in DATA_EVENTS
    return False


def summarize_leg(events: Dict[str, int]) -> Optional[str]:
    """First data label seen, or None when the leg starved."""
    for label in events:
        if is_data_label(label):
            return label
    return None


def run_stdlib(ssid: str, cookies: str, ws_url: str, origin: str,
               user_agent: str, is_demo: bool, seconds: float,
               socket_factory: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Run the script over our stdlib websocket client."""
    from .client import QuotexSocket

    result: Dict[str, Any] = {"connected": False, "authorized": False,
                              "events": {}, "error": ""}
    counts: Dict[str, int] = {}
    result["events"] = counts

    def collect(name: str, args: List[Any]) -> None:
        counts[name] = counts.get(name, 0) + 1

    factory = socket_factory or QuotexSocket
    sock = factory(ssid, ws_url=ws_url, cookies=cookies,
                   user_agent=user_agent, origin=origin, is_demo=is_demo,
                   on_event=collect)
    try:
        sock.connect(authorize=True)
        result["connected"] = True
        for frame in script_frames():
            try:
                sock.send(frame)
            except Exception as exc:  # noqa: BLE001 — record, keep listening
                result["error"] = f"send failed: {exc}"
                break
        time.sleep(max(0.5, seconds))
    except Exception as exc:  # noqa: BLE001 — the report carries the failure
        result["error"] = str(exc) or exc.__class__.__name__
    finally:
        try:
            sock.disconnect()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
    result["authorized"] = "s_authorization" in counts
    return result


def run_chrome(ssid: str, cookies: str, ws_url: str, origin: str,
               user_agent: str, is_demo: bool, seconds: float) -> Dict[str, Any]:
    """Run the script over curl_cffi with Chrome TLS impersonation.

    Strictly sequential on one thread (the sync client is not
    thread-safe for concurrent send/recv): the whole script goes out,
    then a lone receiver drains the wire until the deadline.  Server
    pings are noted but not answered — the window is shorter than any
    ping timeout, so no answer is needed to stay connected for it.
    """
    result: Dict[str, Any] = {"connected": False, "authorized": False,
                              "events": {}, "error": "",
                              "available": True}
    try:
        from curl_cffi import Session
    except ImportError:
        result["available"] = False
        result["error"] = "curl_cffi not installed"
        return result
    counts: Dict[str, int] = {}
    result["events"] = counts
    headers = {
        "User-Agent": user_agent,
        "Origin": origin,
        "Referer": f"{origin}/en/trade",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if cookies:
        headers["Cookie"] = cookies
    try:
        from .protocol import build_authorization

        with Session() as session:
            with session.ws_connect(ws_url, impersonate="chrome",
                                    headers=headers, timeout=10) as ws:
                result["connected"] = True
                inbox: "queue.Queue[Any]" = queue.Queue()

                def recver() -> None:
                    while True:
                        try:
                            inbox.put(ws.recv())
                        except Exception as exc:  # noqa: BLE001 — transport end
                            inbox.put(exc)
                            return

                thread = threading.Thread(target=recver, daemon=True,
                                          name="tlsprobe-recv")
                thread.start()
                # EIO open must arrive first; without it there is no wire.
                first = _next_frame(inbox, timeout=10.0)
                if isinstance(first, Exception):
                    raise first
                if not str(first).startswith("0{"):
                    raise RuntimeError(f"no engine.io open (got {str(first)[:40]!r})")
                ws.send_str("40")
                ws.send_str(build_authorization(ssid, is_demo=is_demo))
                for frame in script_frames():
                    ws.send_str(frame)
                deadline = time.time() + max(0.5, seconds)
                while time.time() < deadline:
                    frame = _next_frame(inbox, timeout=0.25)
                    if frame is None or isinstance(frame, Exception):
                        continue
                    text = frame.decode("utf-8", errors="replace") \
                        if isinstance(frame, bytes) else str(frame)
                    if text == "2":  # engine ping inside the window: noted only
                        counts["EIO ping"] = counts.get("EIO ping", 0) + 1
                        continue
                    label = classify_s2c(text)
                    counts[label] = counts.get(label, 0) + 1
    except Exception as exc:  # noqa: BLE001 — the report carries the failure
        result["error"] = str(exc) or exc.__class__.__name__
    result["authorized"] = any(
        label == 'event "s_authorization"' for label in counts
    )
    return result


def _next_frame(inbox: "queue.Queue[Any]", timeout: float) -> Any:
    try:
        return inbox.get(timeout=timeout)
    except queue.Empty:
        return None


def _leg_report(title: str, result: Dict[str, Any]) -> List[str]:
    out = [f"  ── {title} ──"]
    if not result.get("available", True):
        out.append("  (skipped — python -m pip install curl_cffi to run it)")
        return out
    out.append(f"  connected : {'yes' if result['connected'] else 'NO'}")
    out.append(f"  authorized: {'yes' if result['authorized'] else 'NO'}")
    events = result.get("events", {})
    if events:
        for label in sorted(events):
            out.append(f"  {events[label]:5d}× {label}")
    else:
        out.append("  (no frames at all)")
    first_data = summarize_leg(events)
    out.append(f"  data      : {'YES — ' + first_data if first_data else 'NO'}")
    if result.get("error"):
        out.append(f"  error     : {result['error']}")
    return out


def run_probe(ssid: str, cookies: str, ws_url: str, origin: str,
              user_agent: str, is_demo: bool, seconds: float = 12.0,
              socket_factory: Optional[Callable[..., Any]] = None,
              chrome_runner: Optional[Callable[..., Dict[str, Any]]] = None) -> str:
    """Run both legs back-to-back and render the verdict report."""
    stdlib = run_stdlib(ssid, cookies, ws_url, origin, user_agent, is_demo,
                        seconds, socket_factory=socket_factory)
    chrome_fn = chrome_runner or run_chrome
    chrome = chrome_fn(ssid, cookies, ws_url, origin, user_agent, is_demo,
                       seconds)
    out = ["  ── TLS transport shootout ────────────────────"]
    out.extend(_leg_report("stdlib (python TLS)", stdlib))
    out.extend(_leg_report("chrome (curl_cffi)", chrome))
    stdlib_data = summarize_leg(stdlib.get("events", {})) is not None
    chrome_data = (chrome.get("available", True)
                   and summarize_leg(chrome.get("events", {})) is not None)
    out.append("  ── verdict ──")
    if chrome_data and not stdlib_data:
        out.append("  TLS-GATED: the venue streams to browser TLS only — the "
                   "transport needs the Chrome handshake.")
    elif stdlib_data and chrome_data:
        out.append("  both legs stream — the starve is intermittent or "
                   "session-side, not the transport.")
    elif stdlib_data and not chrome.get("available", True):
        out.append("  stdlib streams; install curl_cffi and re-run to "
                   "complete the comparison.")
    elif not stdlib_data and not chrome.get("available", True):
        out.append("  stdlib starved; install curl_cffi and re-run — the "
                   "chrome leg is the decisive half.")
    elif not stdlib_data and not chrome_data:
        out.append("  both legs starved — NOT TLS (account, session, or "
                   "venue-side gating).")
    else:
        out.append("  stdlib streams but chrome does not — unexpected; "
                   "re-run to confirm.")
    return "\n".join(out)


__all__ = [
    "CANARY_ASSET", "DATA_EVENTS", "script_frames", "is_data_label",
    "summarize_leg", "run_stdlib", "run_chrome", "run_probe",
]
