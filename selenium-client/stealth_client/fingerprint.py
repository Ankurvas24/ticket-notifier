"""
fingerprint.py — coherent UA + viewport + proxy bundle (the refined rotation.py).
=================================================================================

The original ``rotation.py`` picked a UA, a proxy and a viewport independently.
This version returns ONE coherent bundle and derives the matching
``sec-ch-ua-platform`` from the chosen UA, so every signal tells the same story
(mismatches are a classic bot tell). Proxy selection supports both a single
``PROXY_SERVER`` and a rotating ``PROXY_POOL`` — matching the backend.
"""

from __future__ import annotations

import random
import re
from typing import Optional

from . import config


def get_proxy_pool() -> list[str]:
    """Return the proxy ``host:port`` pool: PROXY_POOL, else PROXY_SERVER, else []."""
    if config.PROXY_POOL:
        servers = [s.strip() for s in re.split(r"[,\s]+", config.PROXY_POOL) if s.strip()]
        if servers:
            return servers
    return [config.PROXY_SERVER] if config.PROXY_SERVER else []


def pick_proxy_server() -> Optional[str]:
    """Pick one proxy ``host:port`` at random from the pool, or None."""
    pool = get_proxy_pool()
    return random.choice(pool) if pool else None


def has_proxy_auth() -> bool:
    """True when proxy credentials are configured (needs the auth extension)."""
    return bool(config.PROXY_USERNAME and config.PROXY_PASSWORD)


def platform_for(user_agent: str) -> str:
    """The ``sec-ch-ua-platform`` value implied by the UA's OS token."""
    ua = (user_agent or "").lower()
    if "windows" in ua:
        return "Windows"
    if "macintosh" in ua or "mac os x" in ua:
        return "macOS"
    if "linux" in ua:
        return "Linux"
    return "Windows"


def get_random_fingerprint() -> dict:
    """
    Return a coherent fingerprint:

        {
            "user_agent": str,
            "viewport":   (width, height),
            "proxy":      "host:port" | None,
            "platform":   "Windows" | "macOS" | "Linux",
        }
    """
    ua = random.choice(config.USER_AGENTS)
    return {
        "user_agent": ua,
        "viewport": random.choice(config.VIEWPORTS),
        "proxy": pick_proxy_server(),
        "platform": platform_for(ua),
    }
