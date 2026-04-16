"""
cookie_manager.py
─────────────────
Loads the user's own BookMyShow login session from bms_cookies.json
(in the project root) and injects it into a Playwright browser context
so the bot navigates *as the logged-in user*.

Supported input formats for bms_cookies.json
────────────────────────────────────────────
1. Raw Cookie-header string (semicolon-separated `name=value` pairs)
   — what you get if you copy the `Cookie:` request header from DevTools,
   or what the browser sends in a single `document.cookie` dump.

2. Cookie-Editor / EditThisCookie JSON array
   — objects with `name`, `value`, `domain`, `path`, `expirationDate`,
     `httpOnly`, `secure`, `sameSite`, etc.

3. A plain `{"name": "value", ...}` JSON object.

IP-binding safety
─────────────────
Cloudflare's `cf_clearance`, `__cf_bm`, and `_cfuvid` cookies are
cryptographically bound to the IP address that earned them. If we
inject the user's home-IP CF cookies into the bot (running on Decodo's
Jio residential proxy) Cloudflare will reject everything with
"Sorry, you have been blocked".

We therefore *strip* those cookies before injection and let the bot
earn its own fresh CF clearance on its proxy IP during warmup.
The user's BMS auth cookies (`bmsId`, `ud`, `G_AUTHUSER_H`, etc.)
ride on top of that fresh CF session — so BMS sees a logged-in user.
"""

import os
import json
import logging
from typing import List, Dict, Any

logger = logging.getLogger("ticketalert.cookie_manager")

# Path to the user-provided cookies file
COOKIES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bms_cookies.json",
)

# Cookies that are IP-cryptographically bound and MUST NOT be transferred
# across networks. If we inject these from the user's home IP into the
# bot's Jio proxy IP, Cloudflare will hard-block the session.
_IP_BOUND_COOKIES = {
    "cf_clearance",
    "__cf_bm",
    "_cfuvid",
    "_abck",     # Akamai — also IP/device bound
    "bm_sz",
    "bm_sv",
    "bm_mi",
    "ak_bmsc",
}

# Default domains to attach cookies to when no domain is specified
_DEFAULT_DOMAINS = [".bookmyshow.com", ".district.in"]


def _parse_cookie_header(raw: str) -> List[Dict[str, Any]]:
    """Parse a raw `Cookie:` header string into a list of dicts."""
    cookies: List[Dict[str, Any]] = []
    # The pasted file may or may not contain trailing whitespace/newlines
    raw = raw.strip()

    # Handle `Cookie: ` prefix if present
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()

    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        value = value.strip()
        # Strip surrounding quotes around value (BMS likes to quote bmsId)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            # Keep the quotes — BMS actually sends back the value *with*
            # the double-quotes (e.g. bmsId="1.77688621..."). If we strip
            # them the server rejects the session. Playwright handles
            # quoted values fine.
            pass
        if not name:
            continue
        cookies.append({"name": name, "value": value})
    return cookies


def _load_raw() -> List[Dict[str, Any]]:
    """
    Load the user's BMS login session from (in priority order):

    1. The `BMS_COOKIES_RAW` environment variable — used in production
       (Railway/Heroku/etc.) where the cookies file is gitignored and
       therefore not present in the deployed container. Paste the
       Cookie-header string or JSON array as the env var value.
    2. The `bms_cookies.json` file in the project root — used in local
       development. Same two formats accepted.

    Returns a list of {name, value, ...} dicts, or [] if nothing is set.
    """
    text = ""
    source = ""

    # 1. Env-var first (production)
    env_raw = os.environ.get("BMS_COOKIES_RAW", "").strip()
    if env_raw:
        text = env_raw
        source = "BMS_COOKIES_RAW env var"

    # 2. Fall back to the file on disk (local dev)
    if not text and os.path.isfile(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                text = f.read().strip()
            source = f"{os.path.basename(COOKIES_FILE)}"
        except Exception as e:
            logger.error(f"Failed to read {COOKIES_FILE}: {e}")
            return []

    if not text:
        return []

    # Try JSON first
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            parsed: List[Dict[str, Any]]
            if isinstance(data, list):
                parsed = data
            elif isinstance(data, dict):
                parsed = [{"name": k, "value": v} for k, v in data.items()]
            else:
                parsed = []
            logger.info(f"🔐 Loaded {len(parsed)} BMS cookies from {source} (JSON format)")
            return parsed
        except Exception as e:
            logger.warning(f"{source} looked like JSON but failed to parse: {e}")

    # Fall back to raw Cookie-header string
    parsed = _parse_cookie_header(text)
    logger.info(f"🔐 Loaded {len(parsed)} BMS cookies from {source} (Cookie-header format)")
    return parsed


def _to_playwright(raw_cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Translate raw cookie dicts to Playwright's strict format.

    Playwright rejects unknown keys (`hostOnly`, `session`, `storeId`, `id`),
    so we whitelist only the keys it accepts.
    """
    out: List[Dict[str, Any]] = []
    stripped: List[str] = []

    for c in raw_cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue

        # Strip IP-bound CF/Akamai cookies — let the bot earn fresh ones
        if name in _IP_BOUND_COOKIES:
            stripped.append(name)
            continue

        value = str(value)

        entry: Dict[str, Any] = {"name": str(name), "value": value}

        # Domain / URL — BMS cookies default to .bookmyshow.com
        if c.get("domain"):
            entry["domain"] = c["domain"]
        elif c.get("url"):
            entry["url"] = c["url"]
        else:
            entry["domain"] = ".bookmyshow.com"

        entry["path"] = c.get("path", "/")

        # Expiration
        exp = c.get("expires", c.get("expirationDate"))
        if exp is not None:
            try:
                entry["expires"] = float(exp)
            except Exception:
                pass

        if "httpOnly" in c:
            entry["httpOnly"] = bool(c["httpOnly"])
        if "secure" in c:
            entry["secure"] = bool(c["secure"])

        ss = (c.get("sameSite") or "").strip().lower()
        if ss in ("strict", "lax"):
            entry["sameSite"] = ss.capitalize()
        elif ss in ("none", "no_restriction"):
            entry["sameSite"] = "None"
            entry["secure"] = True  # SameSite=None requires Secure

        out.append(entry)

    # If no domain info at all on any cookie, duplicate each cookie for
    # both bookmyshow.com and district.in so it works on either site
    if out and all(("domain" in c and c["domain"] == ".bookmyshow.com") for c in out):
        # Already attached to BMS only — that's what the user pasted; leave it.
        pass

    if stripped:
        logger.info(
            f"🧹 Stripped {len(stripped)} IP-bound cookies "
            f"(CF/Akamai will be re-earned on bot's proxy IP): {stripped}"
        )

    return out


async def inject_cookies_if_exist(context, session_id: str = "scraper") -> bool:
    """
    Read bms_cookies.json (if present) and inject the user's BMS login
    session into the given Playwright context.

    Returns True if at least one cookie was injected.
    """
    raw = _load_raw()
    if not raw:
        return False

    cookies = _to_playwright(raw)
    if not cookies:
        logger.info(f"[{session_id}] bms_cookies.json had no usable cookies after filtering.")
        return False

    try:
        await context.add_cookies(cookies)
    except Exception as e:
        logger.error(f"[{session_id}] Failed to add cookies to context: {e}")
        # Try one-by-one so a single bad cookie doesn't kill the batch
        ok = 0
        for ck in cookies:
            try:
                await context.add_cookies([ck])
                ok += 1
            except Exception as ee:
                logger.warning(f"[{session_id}] Skipped cookie {ck.get('name')}: {ee}")
        if ok == 0:
            return False
        logger.info(f"[{session_id}] Injected {ok}/{len(cookies)} cookies (some skipped).")

    # Surface which auth-critical cookies made it through
    names = [c["name"] for c in cookies]
    auth_cookies = [n for n in names if n in (
        "bmsId", "ud", "userDetails", "G_AUTHUSER_H", "G_ENABLED_IDPS",
        "rgn", "preferences", "fav", "cohorts", "platform",
        "session", "sessionid", "sessionId", "_district_session",
    )]
    logger.info(
        f"[{session_id}] 💉 INJECTED {len(cookies)} user cookies from bms_cookies.json"
        f" — auth cookies present: {auth_cookies}"
    )
    if "bmsId" in names or "ud" in names:
        logger.info(f"[{session_id}] ✅ Bot will navigate as LOGGED-IN BMS user")
    else:
        logger.warning(
            f"[{session_id}] ⚠ No bmsId/ud in pasted cookies — bot may still hit login wall"
        )

    return True


def have_user_cookies() -> bool:
    """Quick check — returns True if bms_cookies.json is present and non-empty."""
    try:
        return os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 10
    except Exception:
        return False
