"""Chrome-assisted Quotex pairing — the human solves the CAPTCHA, we take the cookie.

Quotex fronts Cloudflare: headless logins get challenged. The operator opens
a real Chrome with a **persistent profile**, logs in ONCE by hand (CAPTCHA
included), and this module detects the resulting session over the
Chrome DevTools Protocol on localhost — no Playwright, no CAPTCHA bypass,
standard library only (urllib + our own WebSocket client). The captured
session lands in ``cfg.qx_session_path`` (mode 0600) and feeds
``QuotexAPI.set_ssid`` for the websocket wire described in
``docs/QUOTEX_PROTOCOL.md``.

Mechanism:
  1. Chrome opens qxbroker.com with ``--user-data-dir`` (profile persists).
  2. The operator logs in and solves the CAPTCHA themselves.
  3. We poll DevTools (``Storage.getCookies`` + the trade page's
     ``window.settings.token``) until a session appears.
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
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ...compat import harden_path, windows_long_path

log = logging.getLogger("cybertrade.pairing")

TRADE_URL = "https://qxbroker.com/en/trade"
SESSION_COOKIE = "session"
# Priority order: the live site has been seen issuing its session under
# several of these names, and polling for exactly one is how logins went
# undetected. Case-insensitive; first hit wins.
SESSION_COOKIE_NAMES = ("session", "ssid", "qx_session", "sessionid", "PHPSESSID")
SESSION_DOMAIN = "qxbroker.com"
# One venue, several front doors — the operator may land on any of them.
SESSION_DOMAINS = ("qxbroker.com", "quotex.com", "quotex.io")
# Second source behind the cookie: the trade page authorizes its own
# websocket with this token, so a login that leaves no readable cookie
# still leaves a detectable session.
TOKEN_EXPRESSION = (
    "(function(){try{var s=window.settings||{};"
    "return s.token||s.session||s.ssid||null;}catch(e){return null;}})()"
)


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
        f"Chrome DevTools never answered on 127.0.0.1:{port} ({last}) — "
        "if Chrome is already running, close every window first: a second "
        "copy cannot take over the profile's DevTools port"
    )


def devtools_targets(
    port: int,
    fetch: Optional[Callable[..., Any]] = None,
) -> List[Dict[str, Any]]:
    """List DevTools page targets; non-list/empty replies → []."""
    fetch = fetch or fetch_json
    try:
        data = fetch(f"http://127.0.0.1:{port}/json/list", timeout=2.0)
    except Exception:  # noqa: BLE001 — chrome still starting
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, dict)]


def devtools_page_ws(
    port: int,
    fetch: Optional[Callable[..., Any]] = None,
) -> str:
    """Page-level DevTools socket for the Quotex tab, else ``""``.

    Cookie reads must run in the page's context: the browser-level socket
    answers ``Storage.getCookies`` from the default storage partition,
    which on some Chrome builds holds nothing at all — pairing then polls
    forever for a cookie the operator already earned.
    """
    targets = devtools_targets(port, fetch=fetch)
    fallback = ""
    for target in targets:
        if target.get("type") not in (None, "page"):
            continue
        ws = str(target.get("webSocketDebuggerUrl") or "")
        if not ws:
            continue
        url = str(target.get("url") or "")
        if any(dom in url for dom in SESSION_DOMAINS):
            return ws
        fallback = fallback or ws
    return fallback


def _resolve_ws(
    port: int,
    fetch: Optional[Callable[..., Any]],
    budget: float,
) -> Tuple[str, str]:
    """Page socket when a tab is visible, else the browser socket.

    Returns ``(ws_url, kind)`` where kind is ``\"page\"`` or ``\"browser\"``
    (progress callbacks report it — cookie reads on a browser socket see
    less than reads in the page's own context).
    """
    try:
        page = devtools_page_ws(port, fetch=fetch)
    except Exception:  # noqa: BLE001 — fall through to the browser socket
        page = ""
    if page:
        return page, "page"
    return devtools_browser_ws(port, fetch=fetch, deadline=budget), "browser"


def _is_venue_domain(domain: str) -> bool:
    domain = (domain or "").lower().lstrip(".")
    return any(
        domain == venue or domain.endswith("." + venue)
        for venue in SESSION_DOMAINS
    )


def extract_session(
    cookies: Iterable[Dict[str, Any]], token: str = ""
) -> Optional[Dict[str, str]]:
    """Pick the Quotex session out of a cookie jar (+ optional page token).

    Cookie-first: ``session`` / ``ssid`` / ``qx_session`` in that order,
    then legacy ``sessionid`` / ``PHPSESSID`` (matched case-insensitively).
    The header carries *every* venue cookie, not just the session one: the
    websocket handshake must look like the paired Chrome tab's, and that
    tab sends its Cloudflare clearance (``__cf_bm``/``_cfuvid``) along with
    the session. A lone session cookie reads as a bare script.

    When no session cookie exists but the trade page exposed
    ``window.settings.token``, that token *is* the session (``source`` says
    which one won, for the operator wondering what got captured).
    """
    jar: Dict[str, Tuple[str, str]] = {}
    for c in cookies or []:
        name = str(c.get("name") or "")
        c_domain = str(c.get("domain") or "")
        value = str(c.get("value") or "")
        if not name or not value or not _is_venue_domain(c_domain):
            continue
        jar.setdefault(name, (value, c_domain))
    hit = ""
    ssid = ""
    domain = ""
    for candidate in SESSION_COOKIE_NAMES:
        if candidate in jar:
            hit = candidate
            ssid, domain = jar[candidate]
            break
    if not ssid:
        lowered = {name.lower(): name for name in jar}
        for candidate in SESSION_COOKIE_NAMES:
            actual = lowered.get(candidate.lower())
            if actual is not None:
                hit = actual
                ssid, domain = jar[actual]
                break
    if ssid:
        ordered = [f"{hit}={ssid}"]
        ordered.extend(f"{k}={v}" for k, (v, _d) in jar.items() if k != hit)
        return {"ssid": ssid, "cookies": "; ".join(ordered),
                "domain": domain, "source": "cookie"}
    token = (token or "").strip()
    if token:
        header = "; ".join(f"{k}={v}" for k, (v, _d) in jar.items())
        first_domain = next((d for _v, d in jar.values()), "")
        return {"ssid": token, "cookies": header,
                "domain": first_domain, "source": "token"}
    return None


def cdp_cookies(ws_url: str, timeout: float = 5.0,
                ws_factory: Optional[Callable[..., Any]] = None) -> List[Dict[str, Any]]:
    """One DevTools round-trip: Storage.getCookies (Network fallback)."""
    if ws_factory is None:
        from ...network.websocket import WebSocketConnection

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


def cdp_page_token(ws_url: str, timeout: float = 5.0,
                   ws_factory: Optional[Callable[..., Any]] = None) -> str:
    """Read the trade page's session token (``window.settings.token``).

    Second source behind the session cookie: the page authorizes its own
    websocket with this token, so a login that leaves no readable cookie
    still leaves a detectable session. Anything unexpected (browser-level
    socket, page not loaded yet, error response) means "no token" — never
    an exception, since this is a best-effort fallback inside a poll loop.
    """
    if ws_factory is None:
        from ...network.websocket import WebSocketConnection

        def ws_factory(url, **kw):  # noqa: E306 — local default
            return WebSocketConnection(url, **kw)
    try:
        ws = ws_factory(ws_url, timeout=timeout)
        ws.connect()
    except Exception:  # noqa: BLE001 — no page, no token
        return ""
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": TOKEN_EXPRESSION,
                                       "returnByValue": True}}))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                raw = ws.recv_text()
            except Exception:  # noqa: BLE001 — socket timeout → no token yet
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") != 1 or not isinstance(msg.get("result"), dict):
                continue
            inner = msg["result"].get("result") or {}
            value = inner.get("value") if isinstance(inner, dict) else None
            return value if isinstance(value, str) and value else ""
        return ""
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
    page_token: Optional[Callable[[str], str]] = None,
    on_poll: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, str]:
    """Poll Chrome until the human's login lands a Quotex session.

    Detection is cookie-first (``session`` / ``ssid`` / ``qx_session`` and
    legacy ``sessionid``) with the trade page's ``window.settings.token``
    as fallback — whichever the login actually leaves behind. ``on_poll``
    fires each round with ``{waited, cookies, target, token_seen}`` (cookie
    *names* only, never values) so every surface can show live progress
    instead of a spinner that might be lying.
    """
    cdp = cdp or cdp_cookies
    if page_token is None:
        page_token = cdp_page_token
    end = time.monotonic() + timeout
    start = time.monotonic()
    last_err = ""
    while time.monotonic() < end:
        try:
            ws, kind = _resolve_ws(
                port, fetch=fetch,
                budget=min(10.0, max(0.5, end - time.monotonic())),
            )
            cookies = cdp(ws) or []
            sess = extract_session(cookies)
            token_seen = False
            if sess is None:
                try:
                    token = page_token(ws) or ""
                except Exception:  # noqa: BLE001 — token is best-effort
                    token = ""
                token_seen = bool(token)
                if token:
                    sess = extract_session(cookies, token=token)
            if on_poll is not None:
                try:
                    on_poll({
                        "waited": time.monotonic() - start,
                        "cookies": sorted({str(c.get("name") or "")
                                           for c in cookies if c.get("name")}),
                        "target": kind,
                        "token_seen": token_seen,
                    })
                except Exception:  # noqa: BLE001 — progress never breaks a poll
                    pass
            if sess:
                log.info("quotex session detected via %s (%s target)",
                         sess.get("source", "cookie"), kind)
                return sess
        except Exception as exc:  # noqa: BLE001 — keep polling
            last_err = str(exc)
        time.sleep(poll)
    raise TimeoutError(
        f"no Quotex session after {timeout:.0f}s — finish the login and CAPTCHA "
        f"in the Chrome window this opened ({last_err or 'session never appeared'})"
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
        with open(windows_long_path(tmp), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        os.replace(windows_long_path(tmp), windows_long_path(path))
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
    on_poll: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, str]:
    """Launch Chrome, wait for the operator's login, persist the session."""
    launcher = launcher or launch_chrome
    waiter = waiter or wait_for_session
    launcher(url, profile_dir, port, chrome)
    sess = waiter(port, timeout=timeout, on_poll=on_poll)
    if not save_session(session_path, sess):
        raise OSError(f"could not write session file {session_path!r}")
    return sess


__all__ = [
    "TRADE_URL", "SESSION_COOKIE", "SESSION_COOKIE_NAMES", "SESSION_DOMAIN",
    "SESSION_DOMAINS", "TOKEN_EXPRESSION",
    "find_chrome", "chrome_argv",
    "launch_chrome", "devtools_browser_ws", "devtools_targets",
    "devtools_page_ws", "extract_session",
    "cdp_cookies", "cdp_page_token", "wait_for_session", "save_session",
    "load_session", "pair_session",
]
