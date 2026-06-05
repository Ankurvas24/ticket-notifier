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

import asyncio
import hashlib
import os
import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

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

        # Expiration — accepts float epoch seconds OR ISO 8601 strings
        exp = c.get("expires", c.get("expirationDate"))
        if exp is not None:
            try:
                entry["expires"] = float(exp)
            except (TypeError, ValueError):
                # Try ISO 8601 ("2025-06-30T12:34:56Z" / "...+00:00")
                if isinstance(exp, str):
                    try:
                        s = exp.strip()
                        if s.endswith("Z"):
                            s = s[:-1] + "+00:00"
                        dt = datetime.fromisoformat(s)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        entry["expires"] = dt.timestamp()
                    except Exception:
                        pass  # leave it unset; session cookie

        # Some exporters use 'httponly' / 'Http-Only' / 'HttpOnly'
        for key in ("httpOnly", "httponly", "http_only"):
            if key in c:
                entry["httpOnly"] = bool(c[key])
                break
        for key in ("secure", "Secure", "SECURE"):
            if key in c:
                entry["secure"] = bool(c[key])
                break

        # sameSite can appear under multiple casings depending on exporter
        ss_raw = (
            c.get("sameSite")
            or c.get("samesite")
            or c.get("same_site")
            or c.get("SameSite")
            or ""
        )
        ss = str(ss_raw).strip().lower()
        if ss in ("strict", "lax"):
            entry["sameSite"] = ss.capitalize()
        elif ss in ("none", "no_restriction", "unspecified"):
            entry["sameSite"] = "None"
            entry["secure"] = True  # SameSite=None requires Secure

        out.append(entry)

    # If every cookie was assigned the default .bookmyshow.com (because no
    # domain was specified in the source), clone them for .district.in so
    # the same injected session works on BOTH sites. This is a no-op if
    # the user explicitly mixed domains.
    if out:
        all_defaults = all(
            c.get("domain") == ".bookmyshow.com" and "url" not in c
            for c in out
        )
        if all_defaults:
            district_clones = []
            for c in out:
                clone = dict(c)
                clone["domain"] = ".district.in"
                district_clones.append(clone)
            out.extend(district_clones)

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
        f"[{session_id}] INJECTED {len(cookies)} user cookies from bms_cookies.json"
        f" - auth cookies present: {auth_cookies}"
    )
    if "bmsId" in names or "ud" in names:
        logger.info(f"[{session_id}] Bot will navigate as LOGGED-IN BMS user")
    else:
        logger.warning(
            f"[{session_id}] No bmsId/ud in pasted cookies - bot may still hit login wall"
        )

    return True


def have_user_cookies() -> bool:
    """Quick check - returns True if BMS cookies are available (env var OR file)."""
    try:
        # Check env var first (production / Railway)
        if os.environ.get("BMS_COOKIES_RAW", "").strip():
            return True
        # Fall back to file on disk (local dev)
        return os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 10
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════════
# BOT-EARNED COOKIE PERSISTENCE + WARM-UP
# ═════════════════════════════════════════════════════════════════════════════
#
# Everything above deals with the USER's BookMyShow auth cookies — we inject
# them and deliberately *strip* the IP-bound Cloudflare/Akamai cookies because
# those were earned on the user's home IP and won't transfer to the bot's proxy.
#
# This section is the mirror image: it PERSISTS the IP-bound clearance the bot
# *earns itself* (cf_clearance, _abck, bm_sz, ...) during warm-up, so a later
# run on the SAME exit IP can replay it and skip the bot-check handshake
# entirely. (This is the reference ``session_manager.py`` idea, made IP-aware
# and TTL-bound for our Playwright stack.)
#
# IP-BINDING CONTRACT — read before using
# ----------------------------------------
# Akamai/Cloudflare clearance is cryptographically bound to the exit IP that
# earned it. Replaying it from a different IP gets you hard-blocked. Therefore:
#   • The cache is keyed by (proxy server + domain) via earned_session_key().
#   • It is only SAFE to reuse when that key maps to a stable IP — i.e. a direct
#     connection (the server's own IP, ideal for the frequent scraper polls) or
#     a sticky/static residential session. With a rotating proxy, set a short
#     TTL or skip replay; a stale clearance simply forces a fresh handshake, it
#     never corrupts anything.

# Where earned-cookie snapshots live (override with EARNED_COOKIES_DIR).
EARNED_COOKIES_DIR = os.environ.get(
    "EARNED_COOKIES_DIR",
    os.path.join(os.path.expanduser("~"), ".ticketalert", "earned_cookies"),
)

# How long an earned snapshot stays valid. cf_clearance lives ~30 min; we use a
# conservative 20 min default so we never replay a hair's-breadth-from-expiry
# token. Override with EARNED_COOKIES_TTL_S.
try:
    EARNED_COOKIES_TTL_S = int(os.environ.get("EARNED_COOKIES_TTL_S", "1200"))
except ValueError:
    EARNED_COOKIES_TTL_S = 1200

# Playwright's add_cookies() only accepts this key set; anything else raises.
_PW_COOKIE_KEYS = {
    "name", "value", "url", "domain", "path",
    "expires", "httpOnly", "secure", "sameSite",
}


def _earned_dir() -> str:
    """Return the cache dir, creating it on first use."""
    try:
        os.makedirs(EARNED_COOKIES_DIR, exist_ok=True)
    except Exception as e:
        logger.warning(f"Could not create earned-cookie dir {EARNED_COOKIES_DIR}: {e}")
    return EARNED_COOKIES_DIR


def earned_session_key(proxy_server: Optional[str], domain: str) -> str:
    """
    Build a stable, filesystem-safe key for an earned-cookie snapshot.

    Keyed by the exit network (proxy server, or ``"direct"``) AND the domain so
    a BMS snapshot never bleeds into a District snapshot, and a snapshot earned
    on one proxy gateway is never replayed through another.
    """
    network = (proxy_server or "direct").strip().lower()
    raw = f"{network}|{(domain or '').strip().lower()}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return digest


def _earned_path(session_key: str) -> str:
    return os.path.join(_earned_dir(), f"{session_key}.json")


def _playwright_safe(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Whitelist keys + normalise sameSite so Playwright's add_cookies accepts them."""
    out: List[Dict[str, Any]] = []
    now = time.time()
    for c in cookies or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        # Drop already-expired cookies (expires <= 0 means a session cookie — keep).
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and 0 < exp < now:
            continue
        entry = {k: v for k, v in c.items() if k in _PW_COOKIE_KEYS}
        if "domain" not in entry and "url" not in entry:
            continue
        ss = str(entry.get("sameSite", "")).strip().lower()
        if ss in ("strict", "lax"):
            entry["sameSite"] = ss.capitalize()
        elif ss in ("none", "no_restriction"):
            entry["sameSite"] = "None"
            entry["secure"] = True  # SameSite=None requires Secure
        else:
            entry.pop("sameSite", None)
        out.append(entry)
    return out


async def save_earned_cookies(context, session_key: str,
                              proxy_server: Optional[str] = None) -> int:
    """
    Snapshot the current context's cookies (including the IP-bound CF/Akamai
    clearance) to the cache under ``session_key``. Returns the number saved.

    Call this AFTER the bot has cleared the bot-check (e.g. right after warm-up
    or once the cart is ready).
    """
    try:
        raw = await context.cookies()
    except Exception as e:
        logger.warning(f"[earned:{session_key}] could not read cookies: {e}")
        return 0
    if not raw:
        return 0

    payload = {
        "saved_at": time.time(),
        "proxy": proxy_server or "direct",
        "cookies": raw,
    }
    try:
        with open(_earned_path(session_key), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        logger.warning(f"[earned:{session_key}] write failed: {e}")
        return 0

    clearance = [c.get("name") for c in raw if c.get("name") in _IP_BOUND_COOKIES]
    logger.info(
        f"[earned:{session_key}] 💾 saved {len(raw)} cookies "
        f"(clearance present: {clearance or 'none'})"
    )
    return len(raw)


async def load_earned_cookies(context, session_key: str,
                              max_age_s: Optional[int] = None) -> bool:
    """
    Inject a previously-earned cookie snapshot into ``context`` if one exists
    and is still within TTL. Returns True if cookies were injected.

    Unlike ``inject_cookies_if_exist`` (which strips IP-bound cookies), this
    KEEPS them — the whole point is to replay the clearance on the same IP.
    """
    path = _earned_path(session_key)
    if not os.path.isfile(path):
        return False

    ttl = EARNED_COOKIES_TTL_S if max_age_s is None else max_age_s
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        logger.warning(f"[earned:{session_key}] read failed: {e}")
        return False

    age = time.time() - float(payload.get("saved_at", 0))
    if age > ttl:
        logger.info(
            f"[earned:{session_key}] snapshot is {age:.0f}s old (> {ttl}s TTL) "
            f"— skipping, bot will earn fresh clearance"
        )
        return False

    cookies = _playwright_safe(payload.get("cookies", []))
    if not cookies:
        return False

    try:
        await context.add_cookies(cookies)
    except Exception as e:
        logger.warning(f"[earned:{session_key}] batch add failed ({e}); retrying one-by-one")
        ok = 0
        for ck in cookies:
            try:
                await context.add_cookies([ck])
                ok += 1
            except Exception:
                continue
        if ok == 0:
            return False

    logger.info(
        f"[earned:{session_key}] ♻️  replayed {len(cookies)} earned cookies "
        f"(age {age:.0f}s) — hoping to skip the bot-check"
    )
    return True


async def warm_up(page, homepage: str, settle_s: float = 5.0,
                  human: bool = True) -> None:
    """
    Visit ``homepage`` and let the Akamai/Cloudflare sensor scripts run so the
    context earns its _abck/bm_sz/cf_clearance the legitimate way.

    Going straight to a deep ``/buytickets/ET...`` URL trips the
    "suspicious entry point" heuristic; warming up on the homepage first is what
    a real user's browser does. Does NOT persist anything — pair it with
    ``save_earned_cookies`` (or call ``warm_up_and_persist``).
    """
    try:
        await page.goto(homepage, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        logger.warning(f"warm_up goto failed for {homepage}: {e}")
        return

    # Let the sensor scripts execute and post their telemetry.
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass

    if human:
        # A tiny human-like flourish so the telemetry sees real interaction.
        try:
            await page.mouse.move(
                random.randint(200, 600), random.randint(150, 400),
                steps=random.randint(8, 18),
            )
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await page.evaluate("window.scrollBy(0, arguments[0])",
                                random.randint(200, 500))
        except Exception:
            pass

    await asyncio.sleep(max(0.0, settle_s))


async def warm_up_and_persist(context, page, homepage: str, session_key: str,
                              proxy_server: Optional[str] = None,
                              settle_s: float = 5.0) -> int:
    """Convenience: warm up on ``homepage`` then snapshot the earned cookies."""
    await warm_up(page, homepage, settle_s=settle_s)
    return await save_earned_cookies(context, session_key, proxy_server=proxy_server)


def clear_earned_cookies(session_key: Optional[str] = None) -> int:
    """
    Delete cached earned-cookie snapshots. Pass a ``session_key`` to clear one,
    or omit it to clear the whole cache. Returns the number of files removed.
    """
    removed = 0
    try:
        if session_key:
            p = _earned_path(session_key)
            if os.path.isfile(p):
                os.remove(p)
                removed = 1
        elif os.path.isdir(EARNED_COOKIES_DIR):
            for fn in os.listdir(EARNED_COOKIES_DIR):
                if fn.endswith(".json"):
                    os.remove(os.path.join(EARNED_COOKIES_DIR, fn))
                    removed += 1
    except Exception as e:
        logger.warning(f"clear_earned_cookies failed: {e}")
    return removed
