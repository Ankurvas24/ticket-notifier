"""
config.py — env-driven configuration for the Selenium stealth client.
=====================================================================

Everything tweakable lives here and is sourced from environment variables so
nothing sensitive (proxy creds, target URLs) is ever hard-coded. Defaults are
aligned to the project's BookMyShow target.

Shared with the backend's ``fingerprint.py`` *by convention* — the UA pool,
viewport pool and IP-bound-cookie set are kept identical so the Selenium client
and the Playwright bot present the same fingerprint surface.
"""

from __future__ import annotations

import logging
import os


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ── Targets ───────────────────────────────────────────────────────────────────
# Warm up on the homepage (so Akamai sets its sensor cookies the legitimate way)
# then navigate to TARGET_URL. Defaults keep everything on BookMyShow.
HOMEPAGE = os.environ.get("HOMEPAGE", "https://in.bookmyshow.com")
TARGET_URL = os.environ.get("TARGET_URL", HOMEPAGE)

# ── Headless ──────────────────────────────────────────────────────────────────
# The reference snippet forced non-headless because many WAFs flag headless
# Chrome. We keep that default but make it overridable for server/cron use
# (HEADLESS=1 uses the modern "--headless=new" mode, which is far less detectable
# than the legacy headless and supports extensions / proxy-auth).
HEADLESS = _env_bool("HEADLESS", False)

# ── Proxy (mirrors backend env vars) ──────────────────────────────────────────
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()
PROXY_POOL = os.environ.get("PROXY_POOL", "").strip()
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "").strip()

# ── Cookie persistence ────────────────────────────────────────────────────────
COOKIE_DIR = os.environ.get(
    "SELENIUM_COOKIE_DIR",
    os.path.join(os.path.expanduser("~"), ".ticketalert", "selenium_cookies"),
)
# How long a saved session stays replayable (Akamai/CF clearance ~30 min).
try:
    COOKIE_TTL_S = int(os.environ.get("SELENIUM_COOKIE_TTL_S", "1200"))
except ValueError:
    COOKIE_TTL_S = 1200

# ── Timing ────────────────────────────────────────────────────────────────────
try:
    WARMUP_SETTLE_S = float(os.environ.get("WARMUP_SETTLE_S", "5"))
except ValueError:
    WARMUP_SETTLE_S = 5.0
PAGE_LOAD_TIMEOUT_S = int(os.environ.get("PAGE_LOAD_TIMEOUT_S", "45"))

# ── Locale ────────────────────────────────────────────────────────────────────
LOCALE = "en-IN"
LANGUAGES = ["en-US", "en"]  # selenium-stealth expects navigator.languages

# ── User-Agent pool ───────────────────────────────────────────────────────────
# Conservative Chrome major that matches what current Chrome ships; mismatched
# UAs are the #1 Akamai bot signal. (Selenium drives real Chrome, so we keep a
# realistic, current major rather than the truncated strings in the snippet.)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# ── Viewport pool ─────────────────────────────────────────────────────────────
VIEWPORTS = [
    (1920, 1080),
    (1440, 900),
    (1366, 768),
    (1536, 864),
]

# ── IP-bound cookies (mirrors backend/cookie_manager.py) ──────────────────────
# These CF/Akamai cookies are cryptographically bound to the exit IP that earned
# them. Reuse them ONLY on the same IP; strip them when replaying across IPs.
IP_BOUND_COOKIES = {
    "cf_clearance", "__cf_bm", "_cfuvid",
    "_abck", "bm_sz", "bm_sv", "bm_mi", "ak_bmsc",
}


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure and return the package logger (idempotent)."""
    logger = logging.getLogger("stealth_client")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger
