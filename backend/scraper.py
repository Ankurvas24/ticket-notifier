"""
scraper.py — Fast availability checker with proxy circuit breaker.

Strategy (optimised for SPEED):
  1. BookMyShow API DIRECT (no proxy) — fastest, ~1-3s
  2. BookMyShow API via proxy (if proxy healthy) — fallback
  3. Requests DIRECT (no proxy) — lightweight HTML check ~2-4s
  4. Requests via proxy — if direct is blocked
  5. Playwright + stealth + proxy — ONLY when explicitly requested

The BMS API approach is critical: instead of rendering the full page and
parsing HTML for "Book Now" text, we hit BMS's own internal API that
returns event data as JSON.  This is 10x faster and immune to HTML
layout changes.

PROXY CIRCUIT BREAKER: If the proxy fails consecutively, it is
auto-disabled for a cooldown period to avoid wasting time on a
dead proxy.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from typing import Optional

import requests
from bs4 import BeautifulSoup

from . import cookie_manager

logger = logging.getLogger("ticketalert.scraper")

# ── Proxy config (same env vars as autocheckout.py) ──────────────────────────
PROXY_SERVER   = os.environ.get("PROXY_SERVER", "")
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")

# ── Proxy circuit breaker ────────────────────────────────────────────────────
_proxy_failures = 0
_proxy_disabled_until = 0.0
_PROXY_MAX_FAILURES = 2           # disable proxy after 2 consecutive failures
_PROXY_COOLDOWN_SECONDS = 300     # re-enable after 5 minutes


def _proxy_is_healthy() -> bool:
    """Check if proxy should be used (circuit breaker)."""
    global _proxy_failures, _proxy_disabled_until
    if _proxy_disabled_until > time.time():
        return False
    if _proxy_disabled_until > 0 and time.time() >= _proxy_disabled_until:
        # Cooldown expired — reset and retry
        _proxy_failures = 0
        _proxy_disabled_until = 0.0
    return True


def _proxy_success():
    """Record a successful proxy request."""
    global _proxy_failures, _proxy_disabled_until
    _proxy_failures = 0
    _proxy_disabled_until = 0.0


def _proxy_failure():
    """Record a proxy failure and potentially trip the circuit breaker."""
    global _proxy_failures, _proxy_disabled_until
    _proxy_failures += 1
    if _proxy_failures >= _PROXY_MAX_FAILURES:
        _proxy_disabled_until = time.time() + _PROXY_COOLDOWN_SECONDS
        logger.warning(
            f"Proxy circuit breaker TRIPPED — disabled for "
            f"{_PROXY_COOLDOWN_SECONDS}s after {_proxy_failures} consecutive failures"
        )


# ── User-Agent pool ──────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
]


def _get_requests_proxy() -> Optional[dict]:
    """Build requests-compatible proxy dict (only if circuit breaker allows)."""
    if not all([PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD]):
        return None
    if not _proxy_is_healthy():
        return None
    import urllib.parse
    safe_user = urllib.parse.quote(PROXY_USERNAME, safe='')
    safe_pass = urllib.parse.quote(PROXY_PASSWORD, safe='')
    proxy_url = f"http://{safe_user}:{safe_pass}@{PROXY_SERVER}"
    return {"http": proxy_url, "https": proxy_url}


def _get_playwright_proxy() -> Optional[dict]:
    """Build Playwright-compatible proxy dict (only if circuit breaker allows)."""
    if not all([PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD]):
        return None
    if not _proxy_is_healthy():
        return None
    return {
        "server":   f"http://{PROXY_SERVER}",
        "username": PROXY_USERNAME,
        "password": PROXY_PASSWORD,
    }


# ── Status keyword lists ────────────────────────────────────────────────────
SOLD_OUT_PHRASES = [
    "sold out", "housefull", "no tickets available",
    "currently unavailable", "not available", "tickets sold",
    "show is sold out", "all tickets sold", "event closed",
    "booking closed", "bookings closed", "sales closed",
    "sale closed", "event is closed", "currently not on sale",
]
UPCOMING_PHRASES = [
    "coming soon", "notify me", "sale starts",
    "goes on sale", "registration open", "sale opens",
    "ticket sales open", "sale will begin",
    # IPL / cricket pre-sale language
    "register interest", "bookings open on", "sale starts on",
    "join the waitlist", "get notified", "early access",
    "presale", "pre-sale", "sales opening",
    "stay tuned", "will be announced", "tbd",
    "opens in", "starts in",  # countdown timers
]
AVAILABLE_PHRASES = [
    "book now", "buy now", "buy tickets", "get tickets",
    "book tickets", "add to cart", "select seats",
    "choose seats", "filling fast",
]


# ═════════════════════════════════════════════════════════════════════════════
# §1  BOOKMYSHOW API CHECKER (fastest, no browser needed)
# ═════════════════════════════════════════════════════════════════════════════

def _extract_bms_event_code(url: str) -> Optional[str]:
    """
    Extract the ET code from a BookMyShow URL.
    e.g. .../kolkata-knight-riders-vs.../ET00493084 → ET00493084
    """
    m = re.search(r"(ET\d{6,12})", url)
    return m.group(1) if m else None


def _check_bms_api(url: str, use_proxy: bool = False) -> Optional[dict]:
    """
    Hit BookMyShow's internal event data API directly.
    This returns JSON with event status, pricing, and availability
    without needing to render the full page.

    Args:
        url: The event URL containing the ET code.
        use_proxy: If True, route through residential proxy.

    Returns a parsed result dict or None if the API call fails.
    """
    event_code = _extract_bms_event_code(url)
    if not event_code:
        return None

    # BMS serves event data via multiple API patterns. Try them in order.
    api_urls = [
        # Primary: event detail API (returns JSON with ShowDetails, pricing)
        f"https://in.bookmyshow.com/api/explore/v1/discover/event/{event_code}",
        # Alternative: serp/venue data
        f"https://in.bookmyshow.com/api/event-home/v1/event/{event_code}",
    ]

    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://in.bookmyshow.com/",
        "Origin": "https://in.bookmyshow.com",
        "DNT": "1",
    }

    proxy = _get_requests_proxy() if use_proxy else None
    # Tight timeout for speed — API should respond in <3s
    timeout = 5 if use_proxy else 4

    for api_url in api_urls:
        try:
            resp = requests.get(
                api_url,
                headers=headers,
                proxies=proxy,
                timeout=timeout,
                allow_redirects=True,
            )

            if resp.status_code != 200:
                logger.debug(f"BMS API {resp.status_code}: {api_url[:80]}")
                continue

            # If we got here with proxy, record success
            if use_proxy and proxy:
                _proxy_success()

            data = resp.json()

            # Extract event name
            name = ""
            if isinstance(data, dict):
                name = (
                    data.get("EventTitle", "")
                    or data.get("EventName", "")
                    or data.get("title", "")
                    or data.get("name", "")
                )
                # Try nested structures
                if not name:
                    for key in ["event", "data", "result"]:
                        nested = data.get(key, {})
                        if isinstance(nested, dict):
                            name = nested.get("EventTitle", "") or nested.get("name", "")
                            if name:
                                break

            # Extract price
            price = ""
            price_val = (
                data.get("MinPrice", "")
                or data.get("fmtdMinPrice", "")
                or data.get("price", "")
            )
            if price_val:
                price = f"₹{price_val} onwards"

            # Determine status from response content
            text = json.dumps(data).lower()

            for phrase in SOLD_OUT_PHRASES:
                if phrase in text:
                    logger.info(f"BMS API: {event_code} → sold_out")
                    return {"status": "sold_out", "name": name, "price": price}

            for phrase in UPCOMING_PHRASES:
                if phrase in text:
                    logger.info(f"BMS API: {event_code} → upcoming")
                    return {"status": "upcoming", "name": name, "price": price}

            # Check for positive signals
            has_shows = False
            if isinstance(data, dict):
                # Check various indicators that tickets exist and are bookable
                show_details = data.get("ShowDetails", data.get("childEvents", []))
                if show_details:
                    has_shows = True
                if data.get("BookMyShow", "") or data.get("isBookable"):
                    has_shows = True
                if data.get("MinPrice") or data.get("fmtdMinPrice"):
                    has_shows = True

            for phrase in AVAILABLE_PHRASES:
                if phrase in text:
                    logger.info(f"BMS API: {event_code} → available (phrase: {phrase})")
                    return {"status": "available", "name": name, "price": price}

            if has_shows:
                logger.info(f"BMS API: {event_code} → available (has shows/pricing)")
                return {"status": "available", "name": name, "price": price}

            logger.info(f"BMS API: {event_code} → unknown (no clear signals)")
            return None  # inconclusive — let other methods try

        except requests.exceptions.ProxyError as e:
            err_str = str(e)
            if "407" in err_str or "authentication required" in err_str.lower():
                logger.error(
                    "🚫 PROXY 407 AUTH FAILURE — Decodo credentials are wrong/"
                    "expired or bandwidth exhausted. Set PROXY_USERNAME/"
                    "PROXY_PASSWORD correctly or unset all PROXY_* env vars to "
                    "bypass the proxy. Tripping circuit breaker."
                )
                # Trip the breaker hard — not just one failure
                if use_proxy:
                    _proxy_failure()
                    _proxy_failure()
                    _proxy_failure()
            else:
                logger.warning(f"BMS API proxy error: {e}")
                if use_proxy:
                    _proxy_failure()
            return None
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            logger.debug(f"BMS API failed for {api_url[:60]}: {e}")
            continue

    return None


# ═════════════════════════════════════════════════════════════════════════════
# §2  REQUESTS-BASED HTML CHECKER
# ═════════════════════════════════════════════════════════════════════════════

def _html_was_redirected(html: str, url: str) -> bool:
    """
    Return True if the HTML looks like the site bounced us to a generic
    landing page (homepage / /cinemas / /movies / /explore) instead of
    serving the event page. Prevents false-positive "available" alerts
    when BMS silently redirects blocked bots.
    """
    if not html:
        return True
    lowered = html.lower()
    # Canonical URL check: if page declares its canonical URL is /cinemas etc.
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
                  lowered)
    if m:
        canonical = m.group(1)
        for bad in ("/cinemas", "/movies", "/home", "/explore", "/offers"):
            # If canonical points to a generic landing page and our URL was
            # event-specific (/sports, /events, /ET...), it's a redirect.
            if canonical.rstrip("/").endswith(bad) and any(
                k in url.lower() for k in ("/sports/", "/events/", "/et0")
            ):
                return True
    # Heuristic: event pages contain an ET-code somewhere in the HTML.
    # If we expected an ET code and none is present, we likely got bounced.
    expected_et = re.search(r"(ET\d{6,12})", url)
    if expected_et and expected_et.group(1).lower() not in lowered:
        # Be lenient: small HTML (<20KB) with no ET code is a clear bounce.
        # Larger pages might just have the code in JS-only props.
        if len(html) < 20_000:
            return True
    return False


def _parse_html(html: str, url: str) -> dict:
    """Extract availability status from raw HTML."""
    # Reject redirect/bounce pages to prevent false-positive alerts
    if _html_was_redirected(html, url):
        logger.info(f"HTML looks redirected/bounced for {url[:60]} — returning unknown")
        return {"status": "unknown", "name": "", "price": "",
                "error": "redirected_to_landing"}

    # ── RAW HTML REGEX FAST-PATH ──────────────────────────────────────
    # Before BS4 parsing, do a case-insensitive regex scan of raw HTML
    # for high-confidence signals. This catches cases where a button's
    # text is inside nested DOM that BS4.get_text() may strip weirdly.
    lowered_html = html.lower()
    # Sold-out takes priority over Book Now (don't alert if seat map is over)
    if re.search(r"\bsold\s*out\b", lowered_html) and \
       not re.search(r"\bbook\s*now\b", lowered_html):
        logger.info(f"🎯 Raw-HTML match: SOLD OUT for {url[:60]}")
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        nm = m.group(1).split("|")[0].strip() if m else ""
        return {"status": "sold_out", "name": nm, "price": ""}
    # Book Now / Buy Now — strong positive signal in raw HTML
    if (re.search(r"\bbook\s*now\b", lowered_html)
            or re.search(r"\bbuy\s*tickets\b", lowered_html)
            or re.search(r"\bget\s*tickets\b", lowered_html)):
        logger.info(f"🎯 Raw-HTML match: BOOK NOW visible for {url[:60]}")
        # Extract title
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        nm = m.group(1).split("|")[0].strip() if m else ""
        # Extract price (₹XXXX onwards)
        pm = re.search(r"₹\s*[\d,]+\s*onwards", html)
        pr = pm.group(0) if pm else ""
        return {"status": "available", "name": nm, "price": pr}

    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True).lower()

    # ── Detect Cloudflare/Akamai bot-challenge pages ──────────────────────
    # Titles like "Just a moment...", "Access denied", "One moment, please..."
    # indicate we were served the bot-check, NOT the event page. We must
    # return "unknown" (not parse this as the event) so the UI shows the
    # real blocked state instead of overwriting the watcher name with junk.
    BOT_CHALLENGE_TITLES = (
        "just a moment", "just a moment...", "checking your browser",
        "attention required", "access denied", "one moment, please",
        "cloudflare", "please enable cookies", "request unsuccessful",
        "bot detection", "security check", "ddos protection",
    )

    # Extract event name from <title>
    name = ""
    title_tag = soup.find("title")
    raw_title = ""
    if title_tag:
        raw_title = title_tag.get_text(strip=True)
        if any(marker in raw_title.lower() for marker in BOT_CHALLENGE_TITLES):
            logger.warning(
                f"🚫 Bot-challenge page detected for {url[:60]} "
                f"(title: {raw_title!r}) — proxy/IP needs fixing"
            )
            return {"status": "unknown", "name": "",  # don't overwrite watcher
                    "price": "", "error": "bot_challenge",
                    "detail": f"Cloudflare/Akamai served challenge page: {raw_title[:80]}"}
        # Remove " - BookMyShow" suffix and similar
        name = re.split(r"\s*[\|–—]\s*", raw_title)[0].strip()
        # Final safety: if somehow the name still ends up looking like junk,
        # drop it rather than corrupt the watcher card
        if name.lower() in {"just a moment", "home", "", "bookmyshow",
                            "district", "loading"}:
            name = ""

    # Extract price
    price = ""
    price_candidates = soup.find_all(
        attrs={"class": lambda c: c and any(
            k in " ".join(c).lower()
            for k in ["price", "amount", "cost", "ticket-price", "fare"]
        )}
    )
    if price_candidates:
        price = price_candidates[0].get_text(strip=True)[:60]

    # Also check for "₹XXXX onwards" pattern in text
    if not price:
        price_match = re.search(r"₹[\d,]+\s*onwards", text)
        if price_match:
            price = price_match.group(0)

    # Check sold out first
    for phrase in SOLD_OUT_PHRASES:
        if phrase in text:
            return {"status": "sold_out", "name": name, "price": price}

    # Check upcoming
    for phrase in UPCOMING_PHRASES:
        if phrase in text:
            return {"status": "upcoming", "name": name, "price": price}

    # Check buttons for availability
    buttons = soup.find_all(["button", "a"])
    for btn in buttons:
        btn_text = btn.get_text(strip=True).lower()
        classes = btn.get("class") or []
        class_text = " ".join(classes).lower() if isinstance(classes, list) else str(classes).lower()
        aria_disabled = str(btn.get("aria-disabled", "")).lower()
        disabled = (
            btn.get("disabled") is not None
            or "disabled" in class_text
            or "inactive" in class_text
            or "closed" in class_text
            or aria_disabled == "true"
        )

        if any(phrase in btn_text for phrase in SOLD_OUT_PHRASES):
            return {"status": "sold_out", "name": name, "price": price}

        for phrase in AVAILABLE_PHRASES:
            if phrase in btn_text and not disabled:
                return {"status": "available", "name": name, "price": price}

    # Conservative fallback: only report "available" from raw text when
    # we see at least TWO available phrases AND we're clearly on the event
    # page (ET code present). Single-phrase matches caused the famous
    # false-positive alerts from generic landing pages.
    et_match = re.search(r"ET\d{6,12}", url)
    hits = [p for p in AVAILABLE_PHRASES if p in text]
    if len(hits) >= 2 and (not et_match or et_match.group(0).lower() in text):
        return {"status": "available", "name": name, "price": price}

    # ── DIAGNOSTIC: log what we saw so user knows WHY "unknown" ──────────
    # Print sample text + which phrases DID match (if any), so when the
    # scraper says "unknown" you can see if BMS is showing some state we
    # haven't wired up (like "Stay tuned", "Sale soon", etc.)
    matched = {
        "available": [p for p in AVAILABLE_PHRASES if p in text],
        "sold_out":  [p for p in SOLD_OUT_PHRASES  if p in text],
        "upcoming":  [p for p in UPCOMING_PHRASES  if p in text],
    }
    # Grab a 400-char snippet of interesting text
    interesting_keywords = ("book", "ticket", "buy", "sold", "coming",
                            "sale", "notify", "register", "soon", "starts",
                            "opens", "queue", "wait")
    snippet_hits = []
    for line in text.split(". "):
        if any(k in line for k in interesting_keywords):
            snippet_hits.append(line.strip()[:120])
            if len(snippet_hits) >= 4:
                break
    snippet = " || ".join(snippet_hits)[:400]
    logger.info(
        f"🔎 Parse result UNKNOWN | title={name!r} | "
        f"matched={matched} | snippet={snippet!r}"
    )

    return {"status": "unknown", "name": name, "price": price}


def _fetch_with_requests(url: str, use_proxy: bool = False) -> Optional[str]:
    """Fetch page HTML via requests, optionally through proxy."""
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "DNT": "1",
        "Connection": "keep-alive",
        "Referer": "https://www.google.com/",
    }
    proxy = _get_requests_proxy() if use_proxy else None
    timeout = 8 if use_proxy else 6
    try:
        resp = requests.get(
            url, headers=headers, timeout=timeout,
            allow_redirects=True, proxies=proxy,
        )
        resp.raise_for_status()
        if use_proxy and proxy:
            _proxy_success()
        return resp.text
    except requests.exceptions.ProxyError as e:
        err_str = str(e)
        if "407" in err_str or "authentication required" in err_str.lower():
            logger.error(
                "🚫 PROXY 407 on HTML fetch — Decodo auth failing. "
                "Check PROXY_USERNAME/PROXY_PASSWORD in Railway env vars."
            )
            if use_proxy:
                _proxy_failure(); _proxy_failure(); _proxy_failure()
        else:
            logger.warning(f"Requests proxy error for {url[:60]}: {e}")
            if use_proxy:
                _proxy_failure()
        return None
    except Exception as e:
        logger.warning(f"Requests fetch failed for {url[:80]}: {e}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# §3  PLAYWRIGHT CHECKER (with proxy + stealth)
# ═════════════════════════════════════════════════════════════════════════════

async def _fetch_with_playwright(url: str) -> Optional[str]:
    """
    Open URL in stealth Chromium with residential proxy.
    Returns page HTML after JS rendering.
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.warning("Playwright not installed — skipping browser check")
        return None

    # Import stealth patcher (playwright-stealth v2.x)
    stealth_async = None
    try:
        from playwright_stealth import stealth_async
    except ImportError:
        pass

    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)
    proxy = _get_playwright_proxy()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

        ctx_kwargs = {
            "user_agent":  ua,
            "viewport":    vp,
            "locale":      "en-IN",
            "timezone_id": "Asia/Kolkata",
            "extra_http_headers": {
                "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "DNT": "1",
            },
        }
        if proxy:
            ctx_kwargs["proxy"] = proxy

        context = await browser.new_context(**ctx_kwargs)
        
        # Inject user cookies to bypass Akamai
        await cookie_manager.inject_cookies_if_exist(context, "scraper")
        
        page = await context.new_page()

        # ── Apply stealth patches to page ─────────────────────────
        stealth_applied = False
        if stealth_async:
            try:
                await stealth_async(page)
                stealth_applied = True
            except Exception as e:
                logger.warning(f"Failed to apply stealth: {e}")

        if not stealth_applied:
            await page.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                try{delete navigator.__proto__.webdriver}catch(e){}
                Object.defineProperty(navigator,'plugins',{
                    get:()=>[{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',
                    description:'Portable Document Format',length:1},
                    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                    description:'',length:1}]
                });
                Object.defineProperty(navigator,'languages',{
                    get:()=>['en-IN','en-US','en','hi']
                });
                window.chrome={runtime:{connect:()=>{},sendMessage:()=>{}},
                    loadTimes:()=>({}),csi:()=>({})};
            """)

        try:
            # Small random delay before navigation
            await asyncio.sleep(random.uniform(0.5, 2.0))

            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except PWTimeout:
                pass

            # ── HUMAN BEHAVIOR — mouse moves before interacting ─────
            try:
                await page.mouse.move(
                    random.randint(100, 400), random.randint(150, 300),
                    steps=random.randint(8, 15),
                )
                await asyncio.sleep(random.uniform(0.3, 0.7))
                await page.mouse.move(
                    random.randint(500, 900), random.randint(300, 600),
                    steps=random.randint(8, 15),
                )
            except Exception:
                pass

            # Quick scroll to trigger lazy-loaded content
            try:
                height = await page.evaluate("document.body.scrollHeight")
                for i in range(1, 4):
                    target_y = int((height / 3) * i)
                    await page.evaluate(f"window.scrollTo(0, {target_y})")
                    await asyncio.sleep(random.uniform(0.2, 0.5))
            except Exception:
                pass

            await asyncio.sleep(random.uniform(0.3, 0.8))

            # ── WAIT FOR BOOK NOW / SOLD OUT BUTTON TO RENDER ────────
            # BMS renders the price sidebar + Book Now button via JS
            # AFTER initial page load. If we grab HTML before it renders,
            # we miss the signal. Wait up to 8s for ANY availability
            # marker to appear. If none appear in time, we proceed with
            # what we have and return "unknown".
            try:
                await page.wait_for_selector(
                    "button:has-text('Book Now'), "
                    "button:has-text('Buy Tickets'), "
                    "button:has-text('Get Tickets'), "
                    "button:has-text('Sold Out'), "
                    "[class*='book-button'], "
                    "text=/book now/i, "
                    "text=/sold out/i, "
                    "text=/coming soon/i, "
                    "text=/notify me/i",
                    timeout=8_000,
                )
            except PWTimeout:
                pass
            except Exception:
                pass

            # ── DETECT REDIRECT TO LANDING PAGE ─────────────────────
            # If we navigated to /cinemas /movies /home etc., bail out —
            # don't report "available" based on generic landing HTML.
            final_url = page.url.lower()
            for junk in ("/cinemas", "/movies", "/home", "/explore",
                         "/offers", "/search"):
                if final_url.rstrip("/").endswith(junk):
                    logger.warning(
                        f"Playwright bounced to landing ({final_url}) — "
                        f"treating as UNKNOWN, not available"
                    )
                    return None

            html = await page.content()

            # ── VERBOSE DIAGNOSTIC — shows WHY unknown happens ──────
            # Log the final URL, title, and a snippet of visible text so
            # the user can see exactly what BMS is serving (pre-sale page,
            # Cloudflare challenge, event info with no book button, etc.)
            try:
                final_title = await page.title()
                body_text = (await page.evaluate(
                    "document.body ? document.body.innerText.slice(0, 800) : ''"
                ) or "").replace("\n", " | ")[:500]
                logger.info(
                    f"🔎 Playwright saw: url={page.url[:100]!r} "
                    f"title={final_title[:80]!r} "
                    f"body-head={body_text[:300]!r}"
                )
            except Exception:
                pass

            if proxy:
                _proxy_success()
            return html

        except PWTimeout:
            logger.warning(f"Playwright timeout on {url[:80]}")
            return None
        except Exception as e:
            logger.warning(f"Playwright error on {url[:80]}: {e}")
            if proxy:
                _proxy_failure()
            return None
        finally:
            await context.close()
            await browser.close()


# ═════════════════════════════════════════════════════════════════════════════
# §4  PUBLIC ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def check_url_availability(url: str, use_browser: bool = True) -> dict:
    """
    Check ticket availability for a URL.

    SPEED-OPTIMISED strategy (tries fastest methods first):
      1. BMS API DIRECT (no proxy) — ~1-3s
      2. BMS API via proxy (if healthy) — ~2-5s
      3. Requests DIRECT (no proxy) — ~2-4s HTML check
      4. Requests via proxy — ~3-8s HTML check
      5. Playwright (only if use_browser=True and above all fail)

    No retries for API checks (they either work or don't).
    Single retry for HTML checks with short backoff.
    """
    # ── Clean URL: BMS's ?data=groupPage parameter makes them serve a
    # stripped-down JSON-ish page instead of the real event HTML with
    # the Book Now sidebar. Strip it so Playwright gets the real page.
    if "?data=" in url:
        url = url.split("?data=")[0]
        logger.info(f"Stripped ?data= param from URL for scraping: {url[:80]}")

    is_bms = "bookmyshow.com" in url.lower()
    start_time = time.time()

    # ── Strategy 1: BMS API DIRECT (fastest — no proxy overhead) ─────────
    if is_bms:
        try:
            result = _check_bms_api(url, use_proxy=False)
            if result and result["status"] != "unknown":
                elapsed = time.time() - start_time
                logger.info(
                    f"BMS API (direct): {result['status']} for {url[:60]} "
                    f"in {elapsed:.1f}s"
                )
                return result
        except Exception as e:
            logger.debug(f"BMS API (direct) failed: {e}")

    # ── Strategy 2: BMS API via proxy (if proxy is healthy) ──────────────
    if is_bms and _proxy_is_healthy():
        try:
            result = _check_bms_api(url, use_proxy=True)
            if result and result["status"] != "unknown":
                elapsed = time.time() - start_time
                logger.info(
                    f"BMS API (proxy): {result['status']} for {url[:60]} "
                    f"in {elapsed:.1f}s"
                )
                return result
        except Exception as e:
            logger.debug(f"BMS API (proxy) failed: {e}")

    # ── Strategy 3: Direct HTML fetch (no proxy) ─────────────────────────
    try:
        html = _fetch_with_requests(url, use_proxy=False)
        if html:
            result = _parse_html(html, url)
            if result["status"] != "unknown":
                elapsed = time.time() - start_time
                logger.info(
                    f"Scraper (direct): {result['status']} for {url[:60]} "
                    f"in {elapsed:.1f}s"
                )
                return result
            # Got HTML but status unknown — still useful, save it
            logger.info(f"Scraper (direct): unknown status for {url[:60]}")
    except Exception as e:
        logger.debug(f"Direct requests failed: {e}")

    # ── Strategy 4: HTML fetch via proxy (if healthy) ────────────────────
    # Keep the best result seen so far — we'll use it only if all later
    # strategies also fail. This way Playwright gets a chance even when
    # requests-via-proxy returns HTML with "unknown" status (static HTML
    # often lacks the JS-rendered Book Now button).
    best_proxy_result = None
    if _proxy_is_healthy():
        try:
            html = _fetch_with_requests(url, use_proxy=True)
            if html:
                result = _parse_html(html, url)
                elapsed = time.time() - start_time
                logger.info(
                    f"Scraper (proxy): {result['status']} for {url[:60]} "
                    f"in {elapsed:.1f}s"
                )
                # Only return immediately if we got a CONFIDENT status.
                # Unknown means static HTML didn't render cleanly — fall
                # through to Playwright which executes JS.
                if result["status"] != "unknown":
                    return result
                best_proxy_result = result
        except Exception as e:
            logger.debug(f"Proxy requests failed: {e}")

    # ── Strategy 5: Playwright (heavy, slow — last resort) ───────────────
    # Retry once on bot-challenge / unknown (CF sometimes needs a second
    # attempt after the proxy IP warms up and earns clearance).
    if use_browser:
        _pw_max_attempts = 2
        for _pw_attempt in range(1, _pw_max_attempts + 1):
            try:
                try:
                    html = asyncio.run(_fetch_with_playwright(url))
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    html = loop.run_until_complete(_fetch_with_playwright(url))
                    loop.close()

                if html:
                    result = _parse_html(html, url)
                    elapsed = time.time() - start_time

                    # If we got a bot-challenge or unknown on the first attempt,
                    # retry once — CF often lets the second request through after
                    # the proxy IP has earned cf_clearance from the first visit.
                    if (_pw_attempt < _pw_max_attempts
                            and result.get("status") in ("unknown",)
                            and result.get("error") in ("bot_challenge", None)):
                        logger.info(
                            f"Playwright attempt {_pw_attempt}: got {result.get('status')}"
                            f"/{result.get('error')} — retrying after short delay"
                        )
                        time.sleep(random.uniform(2.0, 4.0))
                        continue

                    logger.info(
                        f"Scraper (playwright, attempt {_pw_attempt}): "
                        f"{result['status']} for {url[:60]} in {elapsed:.1f}s"
                    )
                    # If Playwright returned "unknown" with no specific error,
                    # and the proxy is dead, the real reason is usually the
                    # bot-check. Flag it so the UI shows the real cause.
                    if (result.get("status") == "unknown"
                            and not result.get("error")
                            and not _proxy_is_healthy()):
                        result["error"] = "bot_challenge"
                        result["detail"] = (
                            "Proxy is down (407/dead). Railway's datacenter IP "
                            "is being bot-challenged by BookMyShow/Akamai. Fix "
                            "PROXY_USERNAME/PROXY_PASSWORD or use a residential "
                            "proxy."
                        )
                    return result
            except Exception as e:
                logger.warning(f"Playwright failed (attempt {_pw_attempt}): {e}")
                if _pw_attempt < _pw_max_attempts:
                    time.sleep(random.uniform(1.5, 3.0))
                    continue
                break

    # ── If Playwright failed but requests-via-proxy gave us SOMETHING,
    #    use that rather than saying "error". Still an "unknown" but at
    #    least we got real HTML (so we know the name, etc.).
    if best_proxy_result is not None:
        logger.info(
            f"Falling back to requests-via-proxy result (Playwright failed)"
        )
        return best_proxy_result

    # ── All methods exhausted ────────────────────────────────────────────
    elapsed = time.time() - start_time
    logger.error(f"All methods failed for {url[:60]} in {elapsed:.1f}s")
    err = "All fetch methods failed"
    detail = ""
    if not _proxy_is_healthy():
        err = "bot_challenge"
        detail = "Proxy is down and BMS/Akamai blocks datacenter IPs."
    return {
        "status": "error", "name": "", "price": "",
        "error": err, "detail": detail,
    }
