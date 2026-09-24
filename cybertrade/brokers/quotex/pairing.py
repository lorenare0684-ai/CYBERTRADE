"""Chrome-assisted Quotex pairing — the human solves the CAPTCHA, we take the cookie.

Quotex fronts Cloudflare: headless logins get challenged. The operator opens
a real Chrome with a **persistent profile**, logs in ONCE by hand (CAPTCHA
included), and this module detects the resulting `sessionid` cookie over the
Chrome DevTools Protocol on localhost — no Playwright, no CAPTCHA bypass,
standard library only (urllib + our own WebSocket client). The captured
session lands in ``cfg.qx_session_path`` (mode 0600) and feeds
``QuotexAPI.set_ssid`` for the websocket wire described in
``docs/QUOTEX_PROTOCOL.md``.

Mechanism:
  1. Chrome opens qxbroker.com with ``--user-data-dir`` (profile persists).
  2. The operator logs in and solves the CAPTCHA themselves.
  3. We poll DevTools (``Storage.getCookies``) until `sessionid` appears.
  4. Session saved; the engine's live modes load it and refuse synthetic data.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from ...compat import harden_path

log = logging.getLogger("cybertrade.pairing")

TRADE_URL = "https://qxbroker.com/en/trade"
SESSION_COOKIE = "sessionid"
SESSION_DOMAIN = "qxbroker.com"


def find_chrome(explicit: str = "") -> str:
    """Locate a Chrome/Chromium binary. ``CYBERTRADE_CHROME`` overrides."""
    explicit = explicit or os.environ.get("CYBERTRADE_CHROME", "")
    if explicit:
        return explicit
    system = platform.system()
    candidates: List[str] = []
    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif system == "Windows":
        for base in (os.environ.get("PROGRAMFILES", ""),
                     os.environ.get("PROGRAMFILES(X86)", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                candidates.append(
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
                )
    else:
        candidates = [
            "google-chrome", "google-chrome-stable",
            "chromium", "chromium-browser", "chrome",
        ]
    for cand in candidates:
        found = cand if os.path.isabs(cand) and os.path.exists(cand) else shutil.which(cand)
        if found:
            return found
    raise FileNotFoundError(
        "Chrome/Chromium not found — install Google Chrome or set "
        "CYBERTRADE_CHROME=/path/to/chrome"
    )


def chrome_argv(url: str, profile_dir: str, port: int, chrome: str) -> List[str]:
    """Chrome argv: persistent profile + DevTools bound to localhost."""
    return [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        url,
    ]


def launch_chrome(url: str, profile_dir: str, port: int,
                  chrome: str = "") -> "subprocess.Popen":
    binary = find_chrome(chrome)
    # Chrome resolves a *relative* --user-data-dir against its own working
    # directory, which on Windows is not reliably ours. A relative path can
    # therefore make Chrome open a throwaway profile somewhere else: the
    # operator logs in, and pairing then polls DevTools for a cookie that was
    # never written to the profile we are watching. Absolute, always.
    profile_dir = os.path.abspath(profile_dir)
    os.makedirs(profile_dir, exist_ok=True)
    argv = chrome_argv(url, profile_dir, port, binary)
    log.info("launching chrome: %s", " ".join(argv))
    kwargs: Dict[str, Any] = {}
    if platform.system() != "Windows":
        kwargs["start_new_session"] = True  # window outlives the command
    return subprocess.Popen(argv, **kwargs)  # noqa: S603 — operator's own binary


def fetch_json(url: str, timeout: float = 3.0,
               opener: Optional[Callable[..., Any]] = None) -> Any:
    import urllib.request

    if opener is None:
        opener = urllib.request.urlopen  # noqa: S310 — fixed localhost URLs
    with opener(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def devtools_browser_ws(
    port: int,
    fetch: Optional[Callable[..., Any]] = None,
    deadline: float = 20.0,
) -> str:
    """Poll ``/json/version`` until Chrome's DevTools browser socket appears."""
    fetch = fetch or fetch_json
    end = time.monotonic() + deadline
    last: Exception = RuntimeError("devtools not up")
    while time.monotonic() < end:
        try:
            data = fetch(f"http://127.0.0.1:{port}/json/version", timeout=2.0)
            ws = (data or {}).get("webSocketDebuggerUrl") or ""
            if ws:
                return ws
        except Exception as exc:  # noqa: BLE001 — chrome still starting
            last = exc
            time.sleep(0.3)
    raise TimeoutError(
        f"Chrome DevTools never answered on 127.0.0.1:{port} ({last})"
    )


def extract_session(cookies: Iterable[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Pick the Quotex ``sessionid`` cookie (value → ssid + cookie header)."""
    best: Optional[Dict[str, str]] = None
    for c in cookies:
        name = str(c.get("name") or "")
        domain = str(c.get("domain") or "")
        value = str(c.get("value") or "")
        if not value or name != SESSION_COOKIE:
            continue
        if SESSION_DOMAIN not in domain:
            continue
        best = {"ssid": value, "cookies": f"{name}={value}", "domain": domain}
    return best


def cdp_cookies(ws_url: str, timeout: float = 5.0,
                ws_factory: Optional[Callable[..., Any]] = None) -> List[Dict[str, Any]]:
    """One DevTools round-trip: Storage.getCookies (Network fallback)."""
    if ws_factory is None:
        from ..network.websocket import WebSocketConnection

        def ws_factory(url, **kw):  # noqa: E306 — local default
            return WebSocketConnection(url, **kw)
    ws = ws_factory(ws_url, timeout=timeout)
    ws.connect()
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        ws.send(json.dumps({"id": 2, "method": "Network.getAllCookies"}))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                raw = ws.recv_text()
            except Exception:  # noqa: BLE001 — socket timeout → give this poll up
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") in (1, 2) and isinstance(msg.get("result"), dict):
                cookies = msg["result"].get("cookies")
                if isinstance(cookies, list):
                    return cookies
        return []
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def wait_for_session(
    port: int,
    timeout: float = 240.0,
    fetch: Optional[Callable[..., Any]] = None,
    cdp: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    poll: float = 1.5,
) -> Dict[str, str]:
    """Poll Chrome until the human's login lands the ``sessionid`` cookie."""
    cdp = cdp or cdp_cookies
    end = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < end:
        try:
            ws = devtools_browser_ws(
                port, fetch=fetch,
                deadline=min(10.0, max(0.5, end - time.monotonic())),
            )
            sess = extract_session(cdp(ws) or [])
            if sess:
                return sess
        except Exception as exc:  # noqa: BLE001 — keep polling
            last_err = str(exc)
        time.sleep(poll)
    raise TimeoutError(
        f"no Quotex session after {timeout:.0f}s — finish the login and CAPTCHA "
        f"in Chrome ({last_err or 'cookie never appeared'})"
    )


def save_session(path: str, data: Dict[str, str]) -> bool:
    """Atomic 0600 write of the paired session (never raises on bad path)."""
    if not path:
        return False
    try:
        payload = {"ssid": data.get("ssid", ""),
                   "cookies": data.get("cookies", ""),
                   "domain": data.get("domain", SESSION_DOMAIN),
                   "captured_at": time.time()}
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
        # 0600 on POSIX; on Windows this is best-effort and says so, because
        # a silent no-op would read as "private" when it is not.
        harden_path(path, 0o600)
        return True
    except OSError:
        log.exception("session save failed: %s", path)
        return False


def load_session(path: str) -> Dict[str, str]:
    """Read a paired session; missing/corrupt -> {} (never raises)."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("ssid"):
            return {
                "ssid": str(data.get("ssid") or ""),
                "cookies": str(data.get("cookies") or ""),
                "domain": str(data.get("domain") or SESSION_DOMAIN),
            }
        return {}
    except (OSError, ValueError) as exc:
        log.warning("session file unreadable (%s)", exc)
        return {}


def pair_session(
    *,
    session_path: str,
    profile_dir: str,
    port: int = 9333,
    chrome: str = "",
    timeout: float = 240.0,
    url: str = TRADE_URL,
    launcher: Optional[Callable[..., Any]] = None,
    waiter: Optional[Callable[..., Dict[str, str]]] = None,
) -> Dict[str, str]:
    """Launch Chrome, wait for the operator's login, persist the session."""
    launcher = launcher or launch_chrome
    waiter = waiter or wait_for_session
    launcher(url, profile_dir, port, chrome)
    sess = waiter(port, timeout=timeout)
    if not save_session(session_path, sess):
        raise OSError(f"could not write session file {session_path!r}")
    return sess


__all__ = [
    "TRADE_URL", "SESSION_COOKIE", "find_chrome", "chrome_argv",
    "launch_chrome", "devtools_browser_ws", "extract_session",
    "cdp_cookies", "wait_for_session", "save_session", "load_session",
    "pair_session",
]
