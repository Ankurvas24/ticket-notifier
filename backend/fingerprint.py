"""
fingerprint.py — Single source of truth for browser fingerprints + proxy pool.
================================================================================

Both ``scraper.py`` and ``autocheckout.py`` used to carry their own *copies* of
the User-Agent list, the viewport list, and the ``_build_ua_pool`` helper. That
duplication drifted over time (the two ``USER_AGENTS`` lists were already
diverging) and is exactly the kind of thing Akamai punishes — if the scraper
and the checkout bot present different fingerprints for the same "user", the
session looks synthetic.

This module unifies all of that into ONE place and adds the one capability the
old code lacked: **rotating across a pool of proxies** (the idea from the
reference ``rotation.py`` snippet, translated to our Playwright stack).

A "fingerprint" here is a *coherent* bundle — the User-Agent OS, the
``sec-ch-ua-platform`` Client Hint, and the ``sec-ch-ua`` brand string all tell
the same story. Picking these independently (as the old inline code risked)
produces contradictions like a Windows UA advertising ``"macOS"`` platform,
which is an instant bot tell.

Environment variables
----------------------
PROXY_SERVER    Single proxy ``host:port`` (existing behaviour, still works).
PROXY_POOL      Comma-separated list of ``host:port`` entries. When set, a
                server is chosen at random per fingerprint. Falls back to
                PROXY_SERVER when unset/empty.
PROXY_USERNAME  Shared credentials applied to whichever server is chosen.
PROXY_PASSWORD  (Residential providers use one cred set across all gateways.)
"""

from __future__ import annotations

import os
import random
import re
from typing import Optional, TypedDict

# ── Locale / timezone — single story across scraper + checkout ───────────────
LOCALE = "en-IN"
TIMEZONE_ID = "Asia/Kolkata"
ACCEPT_LANGUAGE = "en-IN,en;q=0.9,hi;q=0.8"

# ── User-Agent pool ──────────────────────────────────────────────────────────
# Conservative Chrome major version that matches the Chromium actually shipped
# with current Playwright releases. A UA claiming Chrome 131 while running
# Chromium 125 is the #1 signal Akamai uses to flag automated traffic, so we
# also rebuild this pool at runtime from ``browser.version`` (see build_ua_pool).
_CHROME_FALLBACK_VERSION = "125.0.0.0"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# ── Viewport pool ─────────────────────────────────────────────────────────────
# Common real-world desktop resolutions. Stored as Playwright-style dicts; a
# (w, h) tuple accessor is provided for Selenium's set_window_size().
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
]


class Fingerprint(TypedDict):
    """A coherent browser fingerprint — every field tells the same story."""

    user_agent: str
    viewport: dict          # {"width": int, "height": int}
    proxy_server: Optional[str]  # "host:port" or None
    locale: str
    timezone_id: str
    accept_language: str
    platform: str           # sec-ch-ua-platform value, e.g. '"Windows"'
    sec_ch_ua: str          # sec-ch-ua brand string matching the UA major


def build_ua_pool(browser_version: str) -> list[str]:
    """
    Build a UA pool whose Chrome major version matches the Chromium that the
    browser engine just launched, preventing the UA-vs-runtime mismatch block.

    Accepts strings like ``"131.0.6778.33"`` or ``"125.0"``. Falls back to the
    static :data:`USER_AGENTS` list if the version can't be parsed.
    """
    try:
        major = int(str(browser_version).split(".")[0])
    except (ValueError, AttributeError, IndexError):
        return list(USER_AGENTS)
    v = f"{major}.0.0.0"
    return [
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36",
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36",
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36",
    ]


def chrome_major(user_agent: str) -> str:
    """Extract the Chrome major version from a UA string (best-effort)."""
    m = re.search(r"Chrome/(\d+)", user_agent or "")
    return m.group(1) if m else _CHROME_FALLBACK_VERSION.split(".")[0]


def platform_for(user_agent: str) -> str:
    """Return the ``sec-ch-ua-platform`` value implied by the UA's OS token."""
    ua = (user_agent or "").lower()
    if "windows" in ua:
        return '"Windows"'
    if "macintosh" in ua or "mac os x" in ua:
        return '"macOS"'
    if "android" in ua:
        return '"Android"'
    if "linux" in ua:
        return '"Linux"'
    return '"Windows"'


def sec_ch_ua_for(user_agent: str) -> str:
    """Build a ``sec-ch-ua`` brand string whose major matches the UA exactly."""
    major = chrome_major(user_agent)
    return (
        f'"Chromium";v="{major}", '
        f'"Google Chrome";v="{major}", '
        f'"Not=A?Brand";v="99"'
    )


# ── Proxy pool ────────────────────────────────────────────────────────────────

def get_proxy_pool() -> list[str]:
    """
    Return the list of proxy ``host:port`` servers available for rotation.

    Priority:
      1. ``PROXY_POOL`` — comma- or whitespace-separated list (rotation source).
      2. ``PROXY_SERVER`` — the single legacy server (treated as a 1-entry pool).
      3. ``[]`` — no proxy configured (direct connection).
    """
    pool_raw = os.environ.get("PROXY_POOL", "").strip()
    if pool_raw:
        servers = [s.strip() for s in re.split(r"[,\s]+", pool_raw) if s.strip()]
        if servers:
            return servers
    single = os.environ.get("PROXY_SERVER", "").strip()
    return [single] if single else []


def pick_proxy_server() -> Optional[str]:
    """Pick one proxy ``host:port`` from the pool at random, or None."""
    pool = get_proxy_pool()
    return random.choice(pool) if pool else None


def proxy_credentials() -> tuple[str, str]:
    """Return (username, password) shared across all pool servers."""
    return (
        os.environ.get("PROXY_USERNAME", ""),
        os.environ.get("PROXY_PASSWORD", ""),
    )


def has_proxy() -> bool:
    """True when a usable proxy (server + credentials) is configured."""
    user, pw = proxy_credentials()
    return bool(get_proxy_pool() and user and pw)


def playwright_proxy(server: Optional[str] = None) -> Optional[dict]:
    """
    Build a Playwright proxy dict for ``server`` (or a freshly picked one).
    Returns None when no proxy/credentials are configured.
    """
    server = server or pick_proxy_server()
    user, pw = proxy_credentials()
    if not (server and user and pw):
        return None
    return {"server": f"http://{server}", "username": user, "password": pw}


def requests_proxies(server: Optional[str] = None) -> Optional[dict]:
    """
    Build a ``requests``-compatible proxies dict for ``server`` (or a picked one).
    URL-encodes credentials so special characters don't corrupt the proxy URL.
    """
    import urllib.parse

    server = server or pick_proxy_server()
    user, pw = proxy_credentials()
    if not (server and user and pw):
        return None
    safe_user = urllib.parse.quote(user, safe="")
    safe_pass = urllib.parse.quote(pw, safe="")
    url = f"http://{safe_user}:{safe_pass}@{server}"
    return {"http": url, "https": url}


# ── The unified fingerprint factory ───────────────────────────────────────────

def get_random_fingerprint(browser_version: Optional[str] = None) -> Fingerprint:
    """
    Produce a single coherent fingerprint.

    Args:
        browser_version: If provided (e.g. ``browser.version`` from a launched
            Chromium), the UA pool is rebuilt so the advertised Chrome major
            matches the runtime. Otherwise the static pool is used.

    The returned UA, ``platform`` and ``sec_ch_ua`` are guaranteed consistent
    with one another; ``proxy_server`` is drawn from the configured pool.
    """
    ua_pool = build_ua_pool(browser_version) if browser_version else USER_AGENTS
    ua = random.choice(ua_pool)
    return Fingerprint(
        user_agent=ua,
        viewport=random.choice(VIEWPORTS),
        proxy_server=pick_proxy_server(),
        locale=LOCALE,
        timezone_id=TIMEZONE_ID,
        accept_language=ACCEPT_LANGUAGE,
        platform=platform_for(ua),
        sec_ch_ua=sec_ch_ua_for(ua),
    )
