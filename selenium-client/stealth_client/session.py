"""
session.py — cookie persistence + Akamai warm-up (the refined session_manager.py).
===================================================================================

Refinements over the original snippet:
  • IP-aware. Snapshots are keyed by (proxy gateway, domain). When replaying
    across a DIFFERENT exit IP the IP-bound CF/Akamai cookies are stripped
    (reusing them on a foreign IP gets you hard-blocked — the same rule the
    backend's cookie_manager enforces). On the same IP they're kept.
  • TTL-bound, so we never replay near-expired clearance.
  • Robust add_cookie (sanitises Selenium's quirky cookie dicts; navigates to the
    domain first, as Selenium requires, and never lets one bad cookie kill the batch).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from typing import Optional
from urllib.parse import urlparse

from selenium.webdriver.common.action_chains import ActionChains

from . import config

logger = logging.getLogger("stealth_client.session")


def session_key(proxy: Optional[str], domain: str) -> str:
    """Stable, filesystem-safe key for a saved session: (exit network, domain)."""
    network = (proxy or "direct").strip().lower()
    raw = f"{network}|{(domain or '').strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cookie_path(key: str) -> str:
    os.makedirs(config.COOKIE_DIR, exist_ok=True)
    return os.path.join(config.COOKIE_DIR, f"{key}.json")


def save_cookies(driver, key: str, proxy: Optional[str] = None) -> int:
    """Snapshot the driver's cookies (incl. earned clearance) under ``key``."""
    try:
        cookies = driver.get_cookies()
    except Exception as e:
        logger.warning(f"[{key}] get_cookies failed: {e}")
        return 0
    payload = {"saved_at": time.time(), "proxy": proxy or "direct", "cookies": cookies}
    try:
        with open(_cookie_path(key), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        logger.warning(f"[{key}] write failed: {e}")
        return 0
    clearance = [c.get("name") for c in cookies if c.get("name") in config.IP_BOUND_COOKIES]
    logger.info(f"[{key}] saved {len(cookies)} cookies (clearance: {clearance or 'none'})")
    return len(cookies)


def _sanitize(cookie: dict) -> Optional[dict]:
    """Coerce a stored cookie into the shape Selenium's add_cookie accepts."""
    name = cookie.get("name")
    if not name:
        return None
    out = {"name": name, "value": cookie.get("value", "")}
    for k in ("path", "domain", "secure", "httpOnly"):
        if k in cookie and cookie[k] is not None:
            out[k] = cookie[k]
    # expiry must be an int if present
    exp = cookie.get("expiry", cookie.get("expires"))
    if isinstance(exp, (int, float)) and exp > 0:
        out["expiry"] = int(exp)
    ss = str(cookie.get("sameSite", "")).capitalize()
    if ss in ("Strict", "Lax", "None"):
        out["sameSite"] = ss
        if ss == "None":
            out["secure"] = True
    return out


def load_cookies(driver, homepage: str, key: str,
                 same_ip: bool = False,
                 max_age_s: Optional[int] = None) -> bool:
    """
    Inject a saved snapshot. Returns True if any cookie was added.

    Selenium can only set cookies for the domain it's currently on, so we
    navigate to ``homepage`` first. When ``same_ip`` is False the IP-bound
    CF/Akamai cookies are stripped (cross-IP replay would be rejected).
    """
    path = _cookie_path(key)
    if not os.path.isfile(path):
        return False
    ttl = config.COOKIE_TTL_S if max_age_s is None else max_age_s
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        logger.warning(f"[{key}] read failed: {e}")
        return False

    age = time.time() - float(payload.get("saved_at", 0))
    if age > ttl:
        logger.info(f"[{key}] snapshot {age:.0f}s old (> {ttl}s) — skipping")
        return False

    # Must be on the domain before add_cookie.
    try:
        driver.get(homepage)
    except Exception as e:
        logger.warning(f"[{key}] could not open {homepage} to seed cookies: {e}")
        return False

    added = 0
    for raw in payload.get("cookies", []):
        if not same_ip and raw.get("name") in config.IP_BOUND_COOKIES:
            continue  # cross-IP: never replay IP-bound clearance
        ck = _sanitize(raw)
        if not ck:
            continue
        try:
            driver.add_cookie(ck)
            added += 1
        except Exception:
            continue  # one bad cookie shouldn't kill the batch

    if added:
        logger.info(f"[{key}] injected {added} cookies (same_ip={same_ip}, age {age:.0f}s)")
    return added > 0


def warm_up_session(driver, homepage: str = config.HOMEPAGE,
                    settle_s: float = config.WARMUP_SETTLE_S):
    """
    Visit the homepage and let the Akamai/CF sensor scripts run so the browser
    earns its own clearance the legitimate way before we go anywhere deep.
    """
    logger.info(f"warm-up → {homepage}")
    driver.get(homepage)
    time.sleep(min(settle_s, 3.0))  # let sensor scripts post telemetry

    # A small human-like flourish.
    try:
        ActionChains(driver).move_by_offset(
            random.randint(80, 240), random.randint(80, 240)
        ).pause(random.uniform(0.2, 0.5)).perform()
        driver.execute_script("window.scrollBy(0, arguments[0]);", random.randint(200, 500))
    except Exception:
        pass

    time.sleep(max(0.0, settle_s - 3.0))
    return driver
