"""
autocheckout.py — Cart-Only Booking Engine
==========================================

Architecture Overview
---------------------
Cart-only mode: always picks the CHEAPEST price tier and MAX AVAILABLE seats,
adds them to cart, and returns the cart/checkout link to the user. The user
completes payment manually — we never touch OTP or cards.

Key Design Decisions:
  1. **Dedicated asyncio event loop in a background daemon thread.**
     Flask/Gunicorn is WSGI (synchronous). Playwright is async. We spin up ONE
     persistent background thread that owns its own asyncio loop and processes
     booking jobs from a queue.

  2. **Residential proxy with sticky sessions.**
     Akamai Bot Manager fingerprints datacenter IPs instantly. We route ALL
     Playwright traffic through a residential proxy whose username includes
     a per-session UUID — guarantees exit IP stays constant and prevents
     ``_abck`` / ``bm_sz`` cookie invalidation mid-cart.

  3. **Network interception instead of blind waits.**
     When a seat is clicked, an XHR fires to acquire a pessimistic lock.
     We use ``page.expect_response()`` to wait for the backend lock API.
     This eliminates the redirect-loop caused by navigating too early.

  4. **District.in queue/waiting-room handler.**
     District puts high-demand events behind a queue. We detect the queue page
     via known selectors ("you are in line", "position in queue", etc.) and
     poll patiently (up to 10 minutes) until we're through, then proceed.

  5. **playwright-stealth v2 for fingerprint masking.**
     Patches ``navigator.webdriver``, plugin arrays, WebGL renderer strings,
     Chrome runtime objects, and dozens of other Akamai signals.

Environment Variables (set in Railway dashboard):
  PROXY_SERVER        — e.g. ``gate.smartproxy.com:7000``
  PROXY_USERNAME      — e.g. ``sp1234user``
  PROXY_PASSWORD      — e.g. ``secretpass``
"""

# ── stdlib ───────────────────────────────────────────────────────────────────
import asyncio
import logging
import os
import queue
import random
import re
import threading
import time
from typing import Optional

from playwright.async_api import Error as PlaywrightError

from . import cookie_manager

logger = logging.getLogger("ticketalert.checkout")

# ═════════════════════════════════════════════════════════════════════════════
# §1  CONFIGURATION & CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Residential proxy credentials (set in Railway env vars)
PROXY_SERVER   = os.environ.get("PROXY_SERVER", "")
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")

# Timeouts (milliseconds for Playwright, seconds for Python)
NAV_TIMEOUT_MS         = 45_000   # page.goto max wait
NETWORK_IDLE_MS        = 15_000   # wait_for_load_state("networkidle")
SEAT_LOCK_TIMEOUT_MS   = 12_000   # max wait for seat-lock XHR response
ELEMENT_TIMEOUT_MS     = 8_000    # locator visibility wait
DISTRICT_QUEUE_POLL_S  = 4        # poll queue page every 4s
DISTRICT_QUEUE_MAX_S   = 600      # give up queue after 10 min

# Maximum seats to try to grab — we always go for the max available.
DEFAULT_MAX_QTY = 10

# User-Agent pool — MUST match the Chromium major version that Playwright
# actually ships, otherwise Akamai flags the UA-vs-runtime mismatch and
# blocks instantly. We build UAs at runtime from `browser.version` (see
# _build_ua_pool()); this static list is just a sane fallback.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


def _build_ua_pool(browser_version: str) -> list[str]:
    """
    Build a pool of UA strings whose Chrome major version matches the
    actual Chromium that Playwright just launched (prevents the Akamai
    UA/runtime-mismatch fingerprint block).
    """
    # browser.version returns strings like "131.0.6778.33" or just "125.0"
    try:
        major = int(browser_version.split(".")[0])
    except Exception:
        # fallback to the static list if version parsing fails
        return USER_AGENTS
    v = f"{major}.0.0.0"
    return [
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36",
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36",
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36",
    ]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
]

# ── BookMyShow-specific selectors ────────────────────────────────────────────
# FLAW 5 FIX: React auto-hashes classnames at build time (qty-picker_a4f21),
# so raw `[class*='qty-picker']` patterns will silently break the next time
# BMS ships a deploy. We lead with TEXT-based locators (robust across redeploys)
# and only fall through to class patterns as a best-effort backup.

BMS_QTY_DETECT = [
    # Text-based first — survives React class hashing
    "text=/how\\s*many\\s*seats/i",
    "text=/select\\s*number\\s*of\\s*seats/i",
    "text=/number\\s*of\\s*tickets/i",
    ":has-text('How Many Seats')",
    # ARIA / role based
    "[role='dialog'] >> text=/seat/i",
    # Legacy class fallbacks
    "[class*='howManySeats']",
    "[class*='qty-picker']",
    "[class*='seatPicker']",
    "[class*='seat-layout'] [class*='qty']",
]

BMS_CONTINUE = [
    "button:has-text('Continue')",
    "a:has-text('Continue')",
    "[class*='continue']",
    "[class*='proceed-btn']",
    "button:has-text('Proceed')",
]

BMS_BOOK = [
    "button:has-text('Book')",
    "button:has-text('BOOK')",
    "[class*='book-button']",
    "[class*='bookBtn']",
    "button:has-text('Proceed')",
    "button:has-text('Add to Cart')",
    "a:has-text('Book')",
]

# Patterns the network interceptor watches for — XHR endpoints that BMS hits
# when locking seats in their backend.
SEAT_LOCK_URL_PATTERNS = [
    "seats/lock",
    "blockseats",
    "block-seats",
    "reserve",
    "seat-layout",
    "addtocart",
    "add-to-cart",
    "ssadapter",
    "payment-options",
    "ticket-options",
]

# ── District.in queue/waiting-room selectors ────────────────────────────────

DISTRICT_QUEUE_SELECTORS = [
    "text=/you are in line/i",
    "text=/you're in line/i",
    "text=/waiting room/i",
    "text=/position in (the )?queue/i",
    "text=/estimated wait/i",
    "text=/please wait/i",
    "[class*='queue' i]",
    "[class*='waiting-room' i]",
    "[class*='waitingRoom']",
    "[id*='queue' i]",
    "[data-queue]",
]

# If any of these are visible, we know we've made it through the queue
DISTRICT_THROUGH_QUEUE_SELECTORS = [
    "[class*='ticket-card']",
    "[class*='tier']",
    "[class*='seat']",
    "text=/select.*ticket/i",
    "text=/choose.*seat/i",
    "button:has-text('Buy')",
    "button:has-text('Get Tickets')",
    "button:has-text('Book')",
]


# ═════════════════════════════════════════════════════════════════════════════
# §2  SESSION STATE (thread-safe)
# ═════════════════════════════════════════════════════════════════════════════

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _session_id(watcher_id: str) -> str:
    return f"{watcher_id}-cart"


def _update(session_id: str, **kwargs):
    """Thread-safe session update (called from worker thread)."""
    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id].update(kwargs)


def _cleanup_stale_sessions(max_age_s: int = 1800):
    """Remove sessions older than 30 min to prevent memory leaks."""
    now = time.time()
    with _sessions_lock:
        stale = [
            sid for sid, s in _sessions.items()
            if now - s.get("created_at", now) > max_age_s
            and s.get("status") != "running"
        ]
        for sid in stale:
            del _sessions[sid]
    if stale:
        logger.info(f"Cleaned up {len(stale)} stale cart sessions")


# ── Public API for Flask routes ──────────────────────────────────────────────

def get_session(session_id: str) -> dict:
    """Returns session state for frontend polling."""
    with _sessions_lock:
        sess = _sessions.get(session_id, {})
    return {
        "status":   sess.get("status", "idle"),
        "message":  sess.get("message", ""),
        "cart_url": sess.get("cart_url"),
    }


def get_watcher_session(watcher_id: str) -> dict:
    """Get the cart session for a watcher."""
    sid = _session_id(watcher_id)
    return {**get_session(sid), "session_id": sid}


# ═════════════════════════════════════════════════════════════════════════════
# §3  JOB QUEUE — Flask → Worker communication
# ═════════════════════════════════════════════════════════════════════════════

_job_queue: queue.Queue = queue.Queue(maxsize=100)


class BookingJob:
    """Immutable value object representing a single cart request."""
    __slots__ = ("watcher_id", "checkout_url", "target_price",
                 "max_qty", "owner_email")

    def __init__(self, watcher_id: str, checkout_url: str,
                 target_price: str = "", max_qty: int = DEFAULT_MAX_QTY,
                 owner_email: str = ""):
        self.watcher_id   = watcher_id
        self.checkout_url = checkout_url
        self.target_price = target_price
        self.max_qty      = max_qty or DEFAULT_MAX_QTY
        self.owner_email  = owner_email


# ═════════════════════════════════════════════════════════════════════════════
# §4  HUMAN-LIKE INTERACTION HELPERS
# ═════════════════════════════════════════════════════════════════════════════

async def _human_delay(lo: float = 0.3, hi: float = 1.0):
    """Random pause to mimic human reaction time."""
    await asyncio.sleep(random.uniform(lo, hi))


async def _human_move(page, x: float, y: float):
    """Move mouse to (x, y) with a human-like curve."""
    try:
        await page.mouse.move(x, y, steps=random.randint(8, 20))
    except Exception:
        pass


async def _human_click(page, locator_or_sel, timeout: int = ELEMENT_TIMEOUT_MS) -> bool:
    """
    Click an element with realistic mouse movement and positional jitter.
    Accepts either a Playwright Locator or a CSS selector string.
    Returns True if the click succeeded.
    """
    try:
        el = (locator_or_sel if hasattr(locator_or_sel, "click")
              else page.locator(locator_or_sel).first)
        await el.wait_for(state="visible", timeout=timeout)
        box = await el.bounding_box()
        if box:
            x = box["x"] + random.uniform(box["width"] * 0.25, box["width"] * 0.75)
            y = box["y"] + random.uniform(box["height"] * 0.25, box["height"] * 0.75)
            await _human_move(page, x, y)
            await _human_delay(0.08, 0.25)
            await page.mouse.click(x, y)
        else:
            await el.click()
        await _human_delay(0.15, 0.4)
        return True
    except Exception:
        return False


async def _human_scroll(page, distance: int = 400):
    """Scroll down smoothly in random increments."""
    steps = random.randint(3, 7)
    for _ in range(steps):
        await page.mouse.wheel(0, distance // steps + random.randint(-15, 15))
        await _human_delay(0.04, 0.12)


async def _try_click_first(page, selectors: list,
                           timeout: int = 5_000) -> bool:
    """Try each selector in order; click the first visible one."""
    for sel in selectors:
        if await _human_click(page, sel, timeout):
            return True
    return False


# ═════════════════════════════════════════════════════════════════════════════
# §5  PROXY CONFIGURATION — Sticky Sessions
# ═════════════════════════════════════════════════════════════════════════════

def _build_proxy_config() -> Optional[dict]:
    """
    Build Playwright proxy dict with a sticky session.

    The proxy username is suffixed with a unique session UUID so the residential
    proxy provider keeps a single exit IP for the entire browser context
    lifetime, preventing Akamai from invalidating _abck cookies between loads.
    """
    if not all([PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD]):
        logger.warning(
            "No proxy configured (PROXY_SERVER/PROXY_USERNAME/PROXY_PASSWORD). "
            "Akamai will likely block datacenter IPs. Falling back to "
            "instant URL derivation mode."
        )
        return None

    proxy = {
        "server":   f"http://{PROXY_SERVER}",
        "username": PROXY_USERNAME,
        "password": PROXY_PASSWORD,
    }
    logger.info(f"Proxy config activated: {PROXY_SERVER}")
    return proxy


# ═════════════════════════════════════════════════════════════════════════════
# §6  NETWORK INTERCEPTION — Seat Lock Verification
# ═════════════════════════════════════════════════════════════════════════════

def _is_seat_lock_response(response) -> bool:
    """Predicate used by page.expect_response to match BMS/District lock APIs."""
    try:
        url_lower = response.url.lower()
    except Exception:
        return False
    if not any(p in url_lower for p in SEAT_LOCK_URL_PATTERNS):
        return False
    # Only successful API responses count as "lock acquired"
    try:
        return 200 <= response.status < 400
    except Exception:
        return False


class _ResponseWatcher:
    """
    FLAW 6 FIX: Proper asyncio-native response watcher.

    Replaces the old `page.on("response") + while time.time() < deadline`
    polling loop (which had race conditions + CPU burn) with a single
    `asyncio.Future` that completes the instant the matching response
    fires. The listener is always cleaned up in `stop()`, preventing the
    memory leak that plagued the previous version.

    Usage:
        watcher = _ResponseWatcher(page, _is_seat_lock_response)
        watcher.start()
        try:
            await trigger_click()
            resp = await watcher.wait(timeout_ms=12_000)
        finally:
            watcher.stop()
    """
    def __init__(self, page, predicate):
        self._page = page
        self._predicate = predicate
        self._loop = asyncio.get_event_loop()
        self._future: asyncio.Future = self._loop.create_future()
        self._started = False

    def _on_response(self, response):
        if self._future.done():
            return
        try:
            if self._predicate(response):
                # Schedule result onto the running loop (thread-safe)
                self._loop.call_soon_threadsafe(
                    self._future.set_result, response
                )
        except Exception:
            pass

    def start(self):
        if not self._started:
            self._page.on("response", self._on_response)
            self._started = True

    def stop(self):
        if self._started:
            try:
                self._page.remove_listener("response", self._on_response)
            except Exception:
                pass
            self._started = False
        if not self._future.done():
            self._future.cancel()

    async def wait(self, timeout_ms: int):
        try:
            return await asyncio.wait_for(self._future, timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            return None


async def _wait_for_seat_lock(page, session_id: str,
                              timeout_ms: int = SEAT_LOCK_TIMEOUT_MS) -> Optional[dict]:
    """
    Backwards-compatible wrapper around _ResponseWatcher for callers that
    want a single-shot wait AFTER the click has already happened. Prefer the
    explicit watcher pattern when you can attach the listener BEFORE the
    click that triggers the XHR.
    """
    _update(session_id, message="Waiting for seat lock confirmation...")
    watcher = _ResponseWatcher(page, _is_seat_lock_response)
    watcher.start()
    try:
        resp = await watcher.wait(timeout_ms)
        if resp is not None:
            logger.info(
                f"[{session_id}] Seat lock confirmed: "
                f"{resp.status} {resp.url[:120]}"
            )
            _update(session_id, message="Seats locked! Proceeding to cart...")
            return resp
        logger.warning(f"[{session_id}] Seat lock timeout after {timeout_ms}ms")
        _update(session_id, message="Seat lock timed out — proceeding anyway...")
        return None
    finally:
        watcher.stop()


# ═════════════════════════════════════════════════════════════════════════════
# §7  BOOKMYSHOW CART FLOW
# ═════════════════════════════════════════════════════════════════════════════

async def _bms_handle_popups(page, session_id: str):
    """Dismiss any overlaying modals, cookie banners, or login prompts."""
    dismiss_selectors = [
        "button:has-text('Accept')",
        "button:has-text('Got It')",
        "button:has-text('OK')",
        "button:has-text('Close')",
        "button:has-text('Proceed')",
        "[class*='cookie'] button",
        "[class*='consent'] button",
        "[class*='close']",
        "[class*='Close']",
        "svg > path[d*='M19']",
        "button[aria-label='Close']",
        "[class*='dialog'] button:has-text('Later')",
        "[class*='modal'] button:has-text('Skip')",
    ]
    for sel in dismiss_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1_500):
                await loc.click()
                await _human_delay(0.2, 0.5)
        except Exception:
            pass


async def _bms_select_quantity(page, session_id: str, max_qty: int = DEFAULT_MAX_QTY):
    """
    Handle BookMyShow 'How many seats?' popup — always pick the MAXIMUM
    quantity that the dialog offers (up to max_qty).
    """
    _update(session_id, message="Selecting max seat quantity...")

    dialog_found = False
    for sel in BMS_QTY_DETECT:
        try:
            if await page.locator(sel).first.is_visible(timeout=ELEMENT_TIMEOUT_MS):
                dialog_found = True
                break
        except Exception:
            continue

    if not dialog_found:
        logger.info(f"[{session_id}] No qty dialog — may go directly to map")
        return True

    await _human_delay(0.5, 1.0)

    selected = False
    # Try each quantity from max down to 1 — first visible wins
    for qty in list(range(max_qty, 0, -1)):
        if selected:
            break
        for tag in ["div", "span", "button", "li", "a"]:
            try:
                num_loc = page.locator(f"{tag}:text-is('{qty}')").first
                if await num_loc.is_visible(timeout=800):
                    await _human_click(page, num_loc)
                    selected = True
                    logger.info(f"[{session_id}] Selected max quantity: {qty}")
                    break
            except Exception:
                continue

    if not selected:
        try:
            inp = page.locator("input[type='number'], input[name*='qty' i]").first
            if await inp.is_visible(timeout=2_000):
                await inp.fill(str(max_qty))
                selected = True
        except Exception:
            pass

    await _human_delay(0.4, 0.8)

    # Click Continue and wait for network to settle
    for sel in BMS_CONTINUE:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3_000):
                await _human_click(page, btn)
                logger.info(f"[{session_id}] Clicked Continue")
                await _human_delay(1.5, 3.0)
                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
                except Exception:
                    pass
                return True
        except Exception:
            continue

    logger.warning(f"[{session_id}] Could not find Continue button")
    return True


async def _bms_select_cheapest_category(page, session_id: str,
                                        target_price: str = ""):
    """
    Select the CHEAPEST seat category available. If target_price is set, prefer
    that exact price, else the minimum price tier among visible categories.
    """
    label = f" at Rs.{target_price}" if target_price else " (cheapest)"
    _update(session_id, message=f"Selecting seat category{label}...")

    target_num = 0
    if target_price:
        target_num = int("".join(filter(str.isdigit, str(target_price))) or "0")

    await _human_delay(1.0, 2.0)

    candidates = []
    # FLAW 5 FIX: BMS's venueCategory/category classes are React-hashed and
    # flip between deploys. We now scan via ARIA roles + ₹/price text first
    # (stable markers of a price tier), then fall back to class-based hints.
    category_selectors = [
        # Text-based: price tier rows always contain a "₹" symbol
        "li:has-text('₹')",
        "[role='listitem']:has-text('₹')",
        "[role='button']:has-text('₹')",
        "div:has(> *:text-matches('₹\\s*\\d', 'i'))",
        "aside li:has-text('₹')",
        # Legacy class fallbacks
        "[class*='venueCategory'] [class*='category']",
        "[class*='category-list'] li",
        "[class*='price-card']",
        "[class*='venueSeatLayout'] [class*='category']",
        "[class*='ticketTypes'] li",
        "[class*='type-list'] > div",
        "[class*='side-bar'] [class*='item']",
        "aside li",
    ]

    for container_sel in category_selectors:
        try:
            items = await page.locator(container_sel).all()
            for item in items:
                text = (await item.text_content() or "").replace(",", "").replace("\u20b9", "")
                # Skip categories clearly marked as sold-out
                if any(k in text.lower() for k in
                       ["sold out", "unavailable", "coming soon"]):
                    continue
                nums = re.findall(r"\d+", text)
                for num_str in nums:
                    val = int(num_str)
                    if 50 <= val <= 200_000:
                        candidates.append((val, item, text.strip()[:80]))
                        break
            if candidates:
                break
        except Exception:
            continue

    if not candidates:
        # Fallback: search page text for typical price strings
        for price_str in ["499", "500", "750", "999", "1000", "1250", "1500",
                          "1750", "2000", "2500", "3000", "5000", "7500",
                          "10000", "15000", "20000"]:
            try:
                els = await page.locator(f"text=/{price_str}/").all()
                for el in els[:2]:
                    if await el.is_visible(timeout=800):
                        candidates.append((int(price_str), el, price_str))
            except Exception:
                continue

    if not candidates:
        logger.warning(f"[{session_id}] No seat categories found")
        return False

    # Dedupe by price and sort ascending — cheapest first
    seen = set()
    unique = []
    for val, el, txt in candidates:
        if val not in seen:
            seen.add(val)
            unique.append((val, el, txt))
    candidates = sorted(unique, key=lambda x: x[0])
    logger.info(f"[{session_id}] Categories: {[f'Rs.{c[0]}' for c in candidates]}")

    chosen = None
    if target_num:
        # Exact match first
        for c in candidates:
            if c[0] == target_num:
                chosen = c
                break
        # Otherwise cheapest that is >= target (respect user's min price)
        if not chosen:
            for c in candidates:
                if c[0] >= target_num:
                    chosen = c
                    break
    # Fallback: absolute cheapest
    if not chosen:
        chosen = candidates[0]

    val, el, txt = chosen
    logger.info(f"[{session_id}] Selecting Rs.{val}: {txt}")
    await _human_click(page, el)
    await _human_delay(0.8, 1.5)
    return True


async def _bms_select_subsection(page, session_id: str):
    """Click the first available stand/block subsection."""
    _update(session_id, message="Selecting stand section...")
    await _human_delay(0.5, 1.0)

    for sel in ["[class*='sub-category']", "[class*='subCategory']",
                "[class*='venue-block']", "[class*='block-name']",
                "[class*='section-name']", "[class*='stand']"]:
        try:
            items = await page.locator(sel).all()
            for item in items:
                text = (await item.text_content() or "").lower()
                if any(x in text for x in ["sold", "unavailable", "no seats"]):
                    continue
                if await item.is_visible(timeout=1_000):
                    await _human_click(page, item)
                    logger.info(f"[{session_id}] Subsection: {text.strip()[:60]}")
                    await _human_delay(0.8, 1.5)
                    return True
        except Exception:
            continue

    for keyword in ["Upper", "Lower", "Block", "Stand", "Gallery", "Terrace"]:
        try:
            el = page.locator(f"text=/{keyword}/i").first
            if await el.is_visible(timeout=1_500):
                await _human_click(page, el)
                logger.info(f"[{session_id}] Subsection keyword: {keyword}")
                await _human_delay(0.8, 1.5)
                return True
        except Exception:
            continue

    logger.info(f"[{session_id}] No subsections — map may be direct")
    return True


async def _bms_select_max_seats(page, session_id: str, qty: int = DEFAULT_MAX_QTY):
    """Select as many available seats as possible on the stadium map (up to qty)."""
    _update(session_id, message=f"Selecting up to {qty} seats on map...")
    await _human_delay(1.0, 2.0)
    selected = 0

    # Strategy 1: Structured DOM seats
    seat_selectors = [
        "[class*='seat'][class*='available']:not([class*='sold'])",
        "[class*='seat']:not([class*='sold']):not([class*='blocked'])"
        ":not([class*='booked']):not([class*='unavailable'])",
        "[data-available='true']",
        "[class*='seatBox']:not([class*='sold'])",
        "[class*='SeatBlock'] [class*='available']",
    ]

    for sel in seat_selectors:
        try:
            seats = await page.locator(sel).all()
            if not seats:
                continue
            logger.info(f"[{session_id}] Found {len(seats)} seats via: {sel}")
            for seat in seats:
                if selected >= qty:
                    break
                try:
                    if await seat.is_visible(timeout=800):
                        await seat.scroll_into_view_if_needed()
                        await _human_click(page, seat)
                        selected += 1
                        await _human_delay(0.08, 0.2)
                except Exception:
                    continue
            if selected > 0:
                break
        except Exception:
            continue

    # Strategy 2: SVG circles
    if selected == 0:
        try:
            circles = await page.locator(
                "svg circle, svg rect, [class*='Seat'] circle"
            ).all()
            grey = ["#ccc", "#ddd", "#eee", "grey", "gray", "#999",
                    "sold", "blocked", "booked", "unavailable", "#e0e0e0"]
            for circle in circles:
                if selected >= qty:
                    break
                try:
                    fill  = (await circle.get_attribute("fill") or "").lower()
                    cls   = (await circle.get_attribute("class") or "").lower()
                    style = (await circle.get_attribute("style") or "").lower()
                    combined = fill + cls + style
                    if any(m in combined for m in grey):
                        continue
                    if not await circle.is_visible(timeout=500):
                        continue
                    await circle.scroll_into_view_if_needed()
                    await _human_click(page, circle)
                    selected += 1
                    await _human_delay(0.08, 0.2)
                except Exception:
                    continue
        except Exception:
            pass

    # Strategy 3: Generic seat elements
    if selected == 0:
        try:
            map_seats = await page.locator("[class*='seat'], [class*='Seat']").all()
            skip = ["sold", "blocked", "booked", "unavailable", "disabled"]
            for seat in map_seats:
                if selected >= qty:
                    break
                try:
                    cls = (await seat.get_attribute("class") or "").lower()
                    if any(x in cls for x in skip):
                        continue
                    if await seat.is_visible(timeout=500):
                        await _human_click(page, seat)
                        selected += 1
                        await _human_delay(0.08, 0.2)
                except Exception:
                    continue
        except Exception:
            pass

    logger.info(f"[{session_id}] Selected {selected}/{qty} seats")
    return selected > 0


async def _bms_capture_url(page, session_id: str) -> str:
    """Capture the best URL to send to user (ticket-options > cart > current)."""
    url = page.url

    if "ticket-options" in url:
        logger.info(f"[{session_id}] Captured ticket-options URL: {url}")
        return url

    if any(k in url.lower() for k in ["cart", "checkout", "payment", "order"]):
        logger.info(f"[{session_id}] Captured cart URL: {url}")
        return url

    try:
        links = await page.evaluate("""
            () => {
                const found = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    if (/(ticket-options|checkout|cart|payment)/.test(a.href))
                        found.push(a.href);
                });
                return found;
            }
        """)
        if links:
            logger.info(f"[{session_id}] Found link in DOM: {links[0]}")
            return links[0]
    except Exception:
        pass

    logger.info(f"[{session_id}] Using current URL: {url}")
    return url


async def _bms_handle_contact_details(page, session_id: str, owner_email: str):
    """
    If we land on a ticket-options or intermediate checkout page requiring email/phone,
    fill it out so we can reach the final payment page.
    """
    try:
        await _human_delay(1.5, 3.0)
        # Check if we are on a page where we need to input email and phone
        email_inputs = await page.locator("input[type='email'], input[name*='email' i], input[placeholder*='Email' i]").all()
        if email_inputs:
            for inp in email_inputs:
                if await inp.is_visible(timeout=1000):
                    email_to_use = owner_email or os.environ.get("ALERT_EMAIL", "")
                    if not email_to_use:
                        logger.warning(f"[{session_id}] No email available for contact form — skipping")
                        break
                    await inp.fill(email_to_use)
                    logger.info(f"[{session_id}] Filled email: {email_to_use}")
                    break

        phone_inputs = await page.locator("input[type='tel'], input[name*='phone' i], input[name*='mobile' i], input[placeholder*='Mobile' i]").all()
        # Use the configured alert phone (strip +91 prefix for 10-digit field)
        _raw_phone = os.environ.get("ALERT_PHONE", "")
        _phone_digits = re.sub(r"[^\d]", "", _raw_phone)
        if _phone_digits.startswith("91") and len(_phone_digits) == 12:
            _phone_digits = _phone_digits[2:]  # strip country code
        if phone_inputs and _phone_digits:
            for inp in phone_inputs:
                if await inp.is_visible(timeout=1000):
                    await inp.fill(_phone_digits)
                    logger.info(f"[{session_id}] Filled phone number: {_phone_digits}")
                    break

        # Check for T&C checkboxes that might need checking
        checkboxes = await page.locator("input[type='checkbox'], [class*='checkbox']").all()
        for cb in checkboxes:
            try:
                if await cb.is_visible(timeout=500):
                    if not await cb.is_checked():
                        await _human_click(page, cb)
            except Exception:
                pass
        
        # Look for buttons to submit contact details or Proceed to Pay
        proceed_selectors = [
            "button:has-text('Proceed to Pay')",
            "button:has-text('Proceed')",
            "button:has-text('Submit')",
            "button:has-text('Continue')",
            "a:has-text('Proceed to Pay')",
            "[class*='proceed-btn']",
            "button:has-text('Update Details')"
        ]
        
        clicked = await _try_click_first(page, proceed_selectors, timeout=2000)
        if clicked:
            logger.info(f"[{session_id}] Clicked Proceed on contact details page")
            try:
                await page.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:
                pass
            
    except Exception as e:
        logger.warning(f"[{session_id}] Contact details handling issue: {e}")

async def _run_bms_cart(page, session_id: str, target_price: str,
                        watcher_id: str, max_qty: int = DEFAULT_MAX_QTY,
                        owner_email: str = "") -> str:
    """
    Complete BookMyShow cart flow:
      popups → qty(max) → continue → category(cheapest) → subsection
      → seats(max) → WAIT FOR SEAT LOCK → Book → capture cart URL forms

    Robust to partial failures — returns the best URL we got even if
    seat selection failed (so the user can still open the page manually
    with the bot's session cookies transferred).
    """
    await _bms_handle_popups(page, session_id)
    await _bms_select_quantity(page, session_id, max_qty)
    await _human_delay(0.5, 1.0)
    logger.info(f"[{session_id}] After qty: {page.url}")

    # If URL got redirected to a non-event page during qty selection,
    # bail early but still return a useful URL.
    if any(k in page.url.lower() for k in ("/cinemas", "/movies", "/home")):
        logger.warning(f"[{session_id}] Redirected during qty — abort seat pick")
        return page.url

    await _bms_select_cheapest_category(page, session_id, target_price)
    await _bms_select_subsection(page, session_id)

    seats_selected = await _bms_select_max_seats(page, session_id, max_qty)
    if not seats_selected:
        # Log a prominent warning so user understands why cart is empty
        logger.warning(
            f"[{session_id}] ⚠️ 0 seats selected — likely causes: "
            f"(1) seat map requires login, (2) Akamai bot-check, "
            f"(3) sale hasn't actually opened, (4) selectors stale. "
            f"Returning current URL so user can open it manually with "
            f"transferred cookies."
        )
        _update(session_id, message="Could not auto-select seats — cookies captured so you can pick manually.")
        return page.url

    # ── NETWORK INTERCEPTION — wait for seat lock before proceeding ──────
    if seats_selected:
        lock_resp = await _wait_for_seat_lock(page, session_id)
        if lock_resp:
            logger.info(f"[{session_id}] Seat lock verified — safe to proceed")
        else:
            logger.warning(f"[{session_id}] No seat lock intercepted — cautious proceed")

    pre_book_url = await _bms_capture_url(page, session_id)
    logger.info(f"[{session_id}] pre-book URL: {pre_book_url}")

    _update(session_id, message="Adding to cart...")
    await _human_delay(0.5, 1.0)

    # ── FLAW 1 + 9 FIX: Attach a response watcher BEFORE clicking Book so we
    #    capture the real cart-creation / transaction URL as it fires —
    #    that URL carries a transaction ID tied to the user's BMS account
    #    and is the one the user actually needs to open to pay.
    cart_watcher = _ResponseWatcher(page, _is_seat_lock_response)
    cart_watcher.start()

    clicked_book = await _try_click_first(page, BMS_BOOK, timeout=4_000)
    captured_txn_url = ""
    if clicked_book:
        logger.info(f"[{session_id}] Clicked Book button")
        try:
            # Wait up to 12s for the cart/checkout/ticket-options XHR response
            txn_resp = await cart_watcher.wait(timeout_ms=12_000)
            if txn_resp is not None:
                captured_txn_url = (txn_resp.url or "")
                logger.info(
                    f"[{session_id}] 🎯 Captured transaction response: "
                    f"{txn_resp.status} {captured_txn_url[:160]}"
                )
        except Exception as e:
            logger.warning(f"[{session_id}] Transaction watch error: {e}")

        # FLAW 1 FIX: the old code only matched `**/*checkout*` in the URL,
        # but BMS lands users on /ticket-options, /payment, /order, /seat-layout
        # depending on the event type. Match ANY of these; don't give up on
        # the single checkout pattern.
        for url_glob in (
            "**/*checkout*", "**/*ticket-options*", "**/*payment*",
            "**/*order*", "**/*seat-layout*", "**/*cart*",
        ):
            try:
                await page.wait_for_url(url_glob, timeout=4_000)
                logger.info(f"[{session_id}] Landed on cart URL matching {url_glob}")
                break
            except Exception:
                continue
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

        # Try to handle contact details after clicking Book
        await _bms_handle_contact_details(page, session_id, owner_email)

        # In case there's another proceed jump (e.g. order-summary confirmation)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
    cart_watcher.stop()

    # Grab the actual final URL
    cart_url = await _bms_capture_url(page, session_id)

    # FLAW 9: Prefer the transaction URL we captured from the XHR response
    # IF it points to a real checkout/ticket-options page (not just the lock
    # API endpoint). This is the link that survives IP transfer because it's
    # signed with the logged-in user's MEMBERID/transaction token.
    if captured_txn_url:
        low = captured_txn_url.lower()
        # Only use the captured URL if it looks like a user-facing page
        # (contains ticket-options / checkout / payment / order) — lock/addtocart
        # endpoints are API-only and would 404 if the user opens them.
        if any(tok in low for tok in
               ("ticket-options", "/checkout", "/payment", "/order", "/cart")):
            if not cart_url or not _is_useful_cart_url(cart_url):
                cart_url = captured_txn_url
                logger.info(
                    f"[{session_id}] Promoted captured transaction URL "
                    f"as cart_url (was junk)"
                )

    return cart_url


# ═════════════════════════════════════════════════════════════════════════════
# §8  DISTRICT.IN CART FLOW (with queue/waiting-room handler)
# ═════════════════════════════════════════════════════════════════════════════

async def _district_in_queue(page) -> bool:
    """Return True if the page is currently a queue/waiting-room screen."""
    for sel in DISTRICT_QUEUE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                return True
        except Exception:
            continue
    # Also check URL
    url_lower = page.url.lower()
    if any(k in url_lower for k in ["queue", "waitingroom", "waiting-room"]):
        return True
    return False


async def _district_past_queue(page) -> bool:
    """Return True if we appear to be on the ticket selection page."""
    for sel in DISTRICT_THROUGH_QUEUE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                return True
        except Exception:
            continue
    return False


async def _district_wait_through_queue(page, session_id: str) -> bool:
    """
    If District shows a queue/waiting-room, poll until we're through or time
    out. Returns True once we see ticket-selection elements (or if no queue
    was present to begin with).
    """
    in_queue = await _district_in_queue(page)
    if not in_queue:
        return True

    logger.info(f"[{session_id}] Queue detected — waiting up to "
                f"{DISTRICT_QUEUE_MAX_S}s to be let through")
    _update(session_id, status="queued",
            message="You're in line on District — waiting for our turn...")

    deadline = time.time() + DISTRICT_QUEUE_MAX_S
    last_position_log = 0.0

    while time.time() < deadline:
        # Poll for position/ETA text and log it every ~20s
        if time.time() - last_position_log > 20:
            try:
                for sel in [
                    "text=/position.*\\d+/i",
                    "text=/\\d+.*ahead/i",
                    "text=/estimated wait.*\\d+/i",
                    "[class*='position' i]",
                    "[class*='eta' i]",
                ]:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=500):
                        info = (await loc.text_content() or "").strip()[:120]
                        if info:
                            _update(session_id,
                                    message=f"In queue: {info}")
                            logger.info(f"[{session_id}] Queue status: {info}")
                            break
            except Exception:
                pass
            last_position_log = time.time()

        # Wait a few seconds, then re-check both states
        await asyncio.sleep(DISTRICT_QUEUE_POLL_S)

        # If we're past the queue, success
        if await _district_past_queue(page):
            logger.info(f"[{session_id}] Through the queue — proceeding")
            _update(session_id, status="running",
                    message="Through the queue! Picking cheapest seats...")
            return True

        # Still in queue? Keep waiting. If queue markers disappeared but we
        # don't see ticket UI yet, give networkidle a chance.
        if not await _district_in_queue(page):
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass
            if await _district_past_queue(page):
                logger.info(f"[{session_id}] Through the queue (after idle)")
                _update(session_id, status="running",
                        message="Through the queue! Picking cheapest seats...")
                return True

    logger.warning(f"[{session_id}] Queue wait timed out after "
                   f"{DISTRICT_QUEUE_MAX_S}s")
    _update(session_id, message="Queue wait timed out — trying anyway...")
    return False


async def _district_pick_cheapest_tier(page, session_id: str,
                                       target_price: str = "") -> bool:
    """
    Pick the cheapest ticket tier on District. Prefer target_price if set,
    otherwise minimum price.
    """
    _update(session_id, message="Picking cheapest ticket tier on District...")
    await _human_delay(0.8, 1.5)

    target_num = 0
    if target_price:
        target_num = int("".join(filter(str.isdigit, str(target_price))) or "0")

    candidates = []
    tier_selectors = [
        "[class*='ticket-card']",
        "[class*='ticketCard']",
        "[class*='tier-card']",
        "[class*='tier']",
        "[class*='price-card']",
        "[class*='ticket-option']",
    ]

    for sel in tier_selectors:
        try:
            items = await page.locator(sel).all()
            for item in items:
                text = (await item.text_content() or "").replace(",", "").replace("\u20b9", "")
                # Skip sold-out tiers
                if any(k in text.lower() for k in
                       ["sold out", "unavailable", "coming soon", "waitlist"]):
                    continue
                cls = (await item.get_attribute("class") or "").lower()
                if any(k in cls for k in ["sold", "unavailable", "disabled"]):
                    continue
                nums = re.findall(r"\d+", text)
                for n in nums:
                    val = int(n)
                    if 50 <= val <= 200_000:
                        candidates.append((val, item, text.strip()[:80]))
                        break
            if candidates:
                break
        except Exception:
            continue

    if not candidates:
        logger.info(f"[{session_id}] No explicit tiers — falling back to button click")
        return await _try_click_first(page, [
            "button:has-text('Buy')",
            "button:has-text('Get Tickets')",
            "button:has-text('Book')",
            "a:has-text('Buy Tickets')",
        ], timeout=5_000)

    # Dedupe & sort ascending
    seen = set()
    unique = []
    for val, el, txt in candidates:
        if val not in seen:
            seen.add(val)
            unique.append((val, el, txt))
    candidates = sorted(unique, key=lambda x: x[0])
    logger.info(f"[{session_id}] District tiers: "
                f"{[f'Rs.{c[0]}' for c in candidates]}")

    chosen = None
    if target_num:
        for c in candidates:
            if c[0] == target_num:
                chosen = c
                break
        if not chosen:
            for c in candidates:
                if c[0] >= target_num:
                    chosen = c
                    break
    if not chosen:
        chosen = candidates[0]

    val, el, txt = chosen
    logger.info(f"[{session_id}] Selecting District tier Rs.{val}: {txt}")
    await _human_click(page, el)
    await _human_delay(1.0, 1.8)
    return True


async def _district_set_max_qty(page, session_id: str,
                                max_qty: int = DEFAULT_MAX_QTY):
    """Increment the quantity selector to the maximum allowed."""
    # Try the + / plus button repeatedly
    for _ in range(max_qty):
        clicked = await _try_click_first(page, [
            "button[aria-label*='increase' i]",
            "button[aria-label*='plus' i]",
            "button:has-text('+')",
            "[class*='qty'] button:has-text('+')",
            "[class*='quantity'] button:has-text('+')",
            "[class*='counter'] button:has-text('+')",
        ], timeout=700)
        if not clicked:
            break
        await _human_delay(0.1, 0.3)

    # Fallback: direct number input
    try:
        inp = page.locator(
            "input[type='number'], input[name*='qty' i], input[name*='quantity' i]"
        ).first
        if await inp.is_visible(timeout=1_500):
            await inp.fill(str(max_qty))
            await _human_delay(0.2, 0.5)
    except Exception:
        pass


# ── District queue BYPASS helpers ─────────────────────────────────────────────

def _extract_district_event_id(url: str) -> Optional[str]:
    """
    Pull the event slug/id out of a District URL.
    District URLs look like:
      https://www.district.in/events/<slug>-EVT<N>
      https://www.district.in/events/<slug>
    """
    m = re.search(r"district\.in/(?:events|experiences|shows)/([A-Za-z0-9\-]+)", url)
    if m:
        return m.group(1).rstrip("/")
    return None


def _district_bypass_urls(checkout_url: str) -> list:
    """
    Build a list of direct-access URLs that may bypass District's waiting room.
    We try these BEFORE the user-facing event page so the queue never sees us.
    """
    slug = _extract_district_event_id(checkout_url)
    if not slug:
        return []

    base = "https://www.district.in"
    ts = int(time.time() * 1000)  # cache-bust
    urls = [
        # Direct book/ticket endpoints
        f"{base}/events/{slug}/book?_t={ts}",
        f"{base}/events/{slug}/tickets?_t={ts}",
        f"{base}/events/{slug}/checkout?_t={ts}",
        f"{base}/book/{slug}?_t={ts}",
        f"{base}/tickets/{slug}?_t={ts}",
        # Mobile app deeplink (often skips web queue)
        f"{base}/m/events/{slug}?_t={ts}",
        # Event page with cache bust + bypass hint
        f"{base}/events/{slug}?skipQueue=1&_t={ts}",
    ]
    return urls


async def _district_clear_queue_cookies(ctx, session_id: str):
    """Remove known queue/waiting-room cookies to force a fresh session."""
    try:
        cookies = await ctx.cookies()
        keepers = []
        removed = 0
        for c in cookies:
            name = (c.get("name") or "").lower()
            if any(k in name for k in [
                "queue", "waiting", "qit", "queueit", "q-pass",
                "qt-token", "wr-", "waitingroom",
            ]):
                removed += 1
                continue
            keepers.append(c)
        if removed:
            await ctx.clear_cookies()
            await ctx.add_cookies(keepers)
            logger.info(f"[{session_id}] Removed {removed} queue cookies")
    except Exception as e:
        logger.warning(f"[{session_id}] clear_queue_cookies: {e}")


async def _district_try_bypass(page, session_id: str,
                               checkout_url: str) -> bool:
    """
    Try to bypass District's queue by hitting direct endpoints.
    Returns True if we successfully landed on a ticket-selection page.
    """
    bypass_urls = _district_bypass_urls(checkout_url)
    if not bypass_urls:
        return False

    _update(session_id, message="Trying District queue bypass...")

    # Set a mobile user-agent hint via extraHTTPHeaders — some queue gates
    # whitelist the mobile app
    ctx = page.context
    try:
        await ctx.set_extra_http_headers({
            "X-Requested-With":  "com.district.consumer",
            "User-Agent-Platform": "Android",
            "Referer": "https://www.district.in/",
            "Accept":  "application/json, text/html, */*",
        })
    except Exception:
        pass

    # Clear queue cookies first to avoid being recognized
    await _district_clear_queue_cookies(ctx, session_id)

    for bypass_url in bypass_urls:
        try:
            logger.info(f"[{session_id}] Bypass attempt: {bypass_url}")
            resp = await page.goto(bypass_url,
                                   wait_until="domcontentloaded",
                                   timeout=20_000)
            if resp and resp.status >= 400:
                logger.info(f"[{session_id}] Bypass {resp.status} — skip")
                continue

            await _human_delay(0.8, 1.5)
            try:
                await page.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:
                pass

            # Did we land on a queue page again?
            if await _district_in_queue(page):
                logger.info(f"[{session_id}] Bypass hit queue — try next")
                continue

            # Did we land on something useful?
            if await _district_past_queue(page):
                logger.info(f"[{session_id}] BYPASS SUCCESS via {bypass_url}")
                _update(session_id,
                        message="Bypassed queue — picking cheapest seats...")
                return True
        except Exception as e:
            logger.info(f"[{session_id}] Bypass error for {bypass_url}: {e}")
            continue

    logger.info(f"[{session_id}] All bypass routes failed — falling back")
    return False


async def _district_aggressive_refresh(page, session_id: str,
                                       max_refreshes: int = 8) -> bool:
    """
    When stuck in the queue, aggressively refresh with cache-busters.
    Sometimes a refresh at the right moment skips ahead in the queue.
    """
    ctx = page.context
    for i in range(max_refreshes):
        try:
            await _district_clear_queue_cookies(ctx, session_id)
            current = page.url.split("?")[0]
            fresh = f"{current}?_cb={int(time.time()*1000)}{i}"
            logger.info(f"[{session_id}] Aggressive refresh {i+1}/{max_refreshes}")
            await page.goto(fresh, wait_until="domcontentloaded", timeout=15_000)
            await _human_delay(1.0, 2.0)
            if await _district_past_queue(page):
                logger.info(f"[{session_id}] Refresh broke through queue!")
                return True
        except Exception as e:
            logger.info(f"[{session_id}] Refresh error: {e}")
        await asyncio.sleep(2.0)
    return False


async def _run_district_cart(page, session_id: str, target_price: str,
                             watcher_id: str,
                             max_qty: int = DEFAULT_MAX_QTY,
                             checkout_url: str = "") -> str:
    """
    District.in cart flow with AGGRESSIVE queue bypass:
      0. Try direct bypass URLs (skip web queue entirely)
      1. If that fails, aggressively refresh with cache-busters
      2. Fall back to patient queue wait (up to 10 min)
      3. Pick cheapest tier
      4. Bump quantity to max
      5. Add to cart and capture URL
    """
    _update(session_id, message="On District — attempting queue bypass...")

    # ── Step 0: direct-URL bypass (fastest, most reliable) ───────────────
    bypassed = False
    if checkout_url:
        bypassed = await _district_try_bypass(page, session_id, checkout_url)

    # ── Step 0b: if bypass failed and we're in queue, try aggressive refresh
    if not bypassed and await _district_in_queue(page):
        _update(session_id, message="In queue — aggressive refresh bypass...")
        bypassed = await _district_aggressive_refresh(page, session_id)

    # ── Step 1: if STILL in queue, wait it out ──────────────────────────
    if not bypassed:
        await _district_wait_through_queue(page, session_id)

    # Give the ticket UI a moment to fully render
    await _human_delay(1.0, 2.0)
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass

    # Step 2: pick cheapest tier
    await _district_pick_cheapest_tier(page, session_id, target_price)

    # Step 3: set max quantity
    await _district_set_max_qty(page, session_id, max_qty)

    # Step 4: proceed to cart
    _update(session_id, message="Adding District tickets to cart...")
    await _try_click_first(page, [
        "button:has-text('Add to Cart')",
        "button:has-text('Proceed')",
        "button:has-text('Continue')",
        "button:has-text('Checkout')",
        "button:has-text('Book Now')",
        "button[type='submit']",
    ], timeout=5_000)
    await _human_delay(1.5, 3.0)

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass

    url = page.url
    logger.info(f"[{session_id}] District cart URL: {url}")
    return url


# ═════════════════════════════════════════════════════════════════════════════
# §9  URL DERIVATION (fallback when no proxy is available)
# ═════════════════════════════════════════════════════════════════════════════

_SPORTS_KEYWORDS = (
    "-vs-", " vs ", "ipl-", "-ipl", "cricket", "football", "kabaddi",
    "-t20", "-odi-", "-test-", "hockey", "badminton", "pro-kabaddi",
    "isl-", "-isl", "pkl-", "-pkl", "wpl-", "-wpl",
    "mumbai-indians", "chennai-super-kings", "royal-challengers",
    "kolkata-knight-riders", "delhi-capitals", "punjab-kings",
    "rajasthan-royals", "sunrisers-hyderabad", "gujarat-titans",
    "lucknow-super-giants",
)


def _looks_like_sports_slug(slug: str) -> bool:
    """Heuristic: does this slug describe a sports/cricket event?"""
    low = slug.lower()
    return any(k in low for k in _SPORTS_KEYWORDS)


def _derive_buytickets_url(event_url: str) -> str:
    """
    Return the EVENT PAGE URL as the user-facing fallback.

    /buytickets/ URLs DO NOT WORK when pasted directly into a browser — BMS
    requires navigating through its JS flow (event page → Book button → seat
    selection). Pasting a /buytickets/ URL makes BMS show a 404 or redirect
    to /cinemas.

    So we convert /buytickets/ → the correct PUBLIC landing page:
      • sports events (IPL / cricket / football / kabaddi …) → /sports/
      • everything else (concerts, plays, comedy)             → /events/
    """
    # /buytickets/slug/ET... → /sports/slug/ET... or /events/slug/ET...
    m = re.search(r"in\.bookmyshow\.com/buytickets/([^?#]+)", event_url)
    if m:
        slug = m.group(1).rstrip("/")
        if _looks_like_sports_slug(slug):
            return f"https://in.bookmyshow.com/sports/{slug}"
        return f"https://in.bookmyshow.com/events/{slug}"
    # Already a /sports/ or /events/ page — keep as-is
    return event_url


# ═════════════════════════════════════════════════════════════════════════════
# §10  MAIN CART COROUTINE
# ═════════════════════════════════════════════════════════════════════════════

async def _run_cart(session_id: str, checkout_url: str, target_price: str,
                    watcher_id: str, max_qty: int = DEFAULT_MAX_QTY,
                    owner_email: str = ""):
    """
    Core cart coroutine — runs inside the worker thread's event loop.
    Opens stealth Chromium via residential proxy, adds cheapest tier + max
    seats to cart, returns the cart/checkout URL. Never touches payment.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed")
        _update(session_id, status="failed", message="Playwright not installed")
        return

    # Import stealth patcher
    _stealth_cls = None
    try:
        from playwright_stealth import Stealth
        _stealth_cls = Stealth
        logger.info(f"[{session_id}] playwright-stealth v2 loaded")
    except Exception as e:
        logger.warning(f"[{session_id}] playwright-stealth not available ({e}) — using manual JS patches")

    _update(session_id, status="running",
            message="Starting cart session — cheapest tier + max seats...")

    vp = random.choice(VIEWPORTS)
    proxy = _build_proxy_config()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

        # ── FLAW 3 FIX: dynamically match UA to the Chromium we just launched
        #    so Akamai can't flag a "Chrome 131 claim from Chromium 125" mismatch.
        try:
            real_version = browser.version  # e.g. "125.0.6422.112"
            ua_pool = _build_ua_pool(real_version)
            logger.info(f"[{session_id}] Chromium runtime version: {real_version}")
        except Exception:
            ua_pool = USER_AGENTS
        ua = random.choice(ua_pool)

        ctx_kwargs = {
            "user_agent":        ua,
            "viewport":          vp,
            "locale":            "en-IN",
            "timezone_id":       "Asia/Kolkata",
            "extra_http_headers": {
                "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                "Accept":          "text/html,application/xhtml+xml,"
                                   "application/xml;q=0.9,*/*;q=0.8",
                "DNT":             "1",
            },
        }
        if proxy:
            ctx_kwargs["proxy"] = proxy

        ctx = await browser.new_context(**ctx_kwargs)
        
        # ── Inject user cookies if provided ────────────────────────────
        has_custom_cookies = await cookie_manager.inject_cookies_if_exist(ctx, session_id)

        # ── Apply stealth patches to context ───────────────────────────
        stealth_applied = False
        if _stealth_cls:
            try:
                await _stealth_cls().apply_stealth_async(ctx)
                stealth_applied = True
                logger.info(f"[{session_id}] Stealth v2 patches applied to context")
            except Exception as e:
                logger.warning(f"[{session_id}] Stealth.apply_stealth_async failed ({e})")

        if not stealth_applied:
            await ctx.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                try{delete navigator.__proto__.webdriver}catch(e){}
                Object.defineProperty(navigator,'plugins',{
                    get:()=>[{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',
                    description:'Portable Document Format',length:1},
                    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                    description:'',length:1},
                    {name:'Native Client',filename:'internal-nacl-plugin',
                    description:'',length:2}]
                });
                Object.defineProperty(navigator,'languages',{
                    get:()=>['en-IN','en-US','en','hi']
                });
                window.chrome={runtime:{connect:()=>{},sendMessage:()=>{}},
                    loadTimes:()=>({}),csi:()=>({})};
                const _oq=navigator.permissions.query.bind(navigator.permissions);
                navigator.permissions.query=p=>p.name==='notifications'
                    ?Promise.resolve({state:Notification.permission}):_oq(p);
                const _gp=WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter=function(p){
                    if(p===37445)return'Intel Inc.';
                    if(p===37446)return'Intel Iris OpenGL Engine';
                    return _gp.call(this,p)};
            """)
            logger.info(f"[{session_id}] Manual JS stealth patches applied")

        page = await ctx.new_page()

        try:
            is_bms      = "bookmyshow.com" in checkout_url.lower()
            is_district = "district.in" in checkout_url.lower()

            # ── AKAMAI WARMUP ─────────────────────────────────────────────
            # Visit the site's homepage first so Akamai sets its _abck/bm_sz
            # cookies via the legitimate bot-check script. Going STRAIGHT to
            # /buytickets/ET... triggers the "suspicious entry" heuristic
            # and gets the session killed before we can see seats.
            try:
                # IMPORTANT: even if we injected the user's BMS auth cookies,
                # we ALWAYS run warmup. The user's CF/Akamai cookies are
                # IP-bound and were stripped by cookie_manager — the bot
                # still needs to earn fresh CF clearance on its proxy IP
                # before BMS will serve the event page. Auth cookies
                # (bmsId, ud) layer on top of that fresh CF session.
                if is_bms:
                    warmup_url = "https://in.bookmyshow.com/"
                elif is_district:
                    warmup_url = "https://www.district.in/"
                else:
                    warmup_url = None
                if has_custom_cookies and warmup_url:
                    logger.info(
                        f"[{session_id}] Custom BMS session present — running warmup "
                        f"anyway to earn fresh CF clearance on proxy IP"
                    )

                if warmup_url:
                    _update(session_id, message="Warming up session (Akamai handshake)...")
                    logger.info(f"[{session_id}] Warmup → {warmup_url}")
                    await page.goto(warmup_url, wait_until="domcontentloaded",
                                    timeout=NAV_TIMEOUT_MS)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8_000)
                    except Exception:
                        pass
                    # Small human-like mouse dance so Akamai's telemetry
                    # sees a "real" user interacting with the page.
                    try:
                        await page.mouse.move(
                            random.randint(200, 600),
                            random.randint(150, 400),
                            steps=random.randint(8, 18),
                        )
                        await _human_delay(0.4, 0.9)
                        await page.mouse.move(
                            random.randint(400, 900),
                            random.randint(300, 600),
                            steps=random.randint(8, 18),
                        )
                        await _human_scroll(page, 300)
                    except Exception:
                        pass
                    await _human_delay(0.6, 1.4)
                    logger.info(f"[{session_id}] Warmup done; now navigating to event")
            except Exception as e:
                logger.warning(f"[{session_id}] Warmup step failed ({e}) — continuing")

            # ── FLAW 1 FIX: snapshot cookies after warmup so we can prove
            #    (in logs) that the bot earned CF clearance + carries the
            #    user's auth wristband into the event page.
            try:
                post_warmup_cookies = await ctx.cookies()
                warmup_names = sorted({c.get("name", "") for c in post_warmup_cookies})
                logger.info(
                    f"[{session_id}] 🍪 {len(post_warmup_cookies)} cookies after "
                    f"warmup: {warmup_names[:20]}"
                    f"{'…' if len(warmup_names) > 20 else ''}"
                )
            except Exception as _e:
                logger.warning(f"[{session_id}] cookie snapshot failed: {_e}")

            # ── Normalize /buytickets/ → /sports/ or /events/ before nav ──
            # BMS's /buytickets/<slug>/ET... URLs 404 when hit directly
            # (they only work inside the JS flow). Convert the URL to the
            # proper public landing page so the bot AND the user's browser
            # see real content instead of "page unavailable".
            nav_url = checkout_url
            if is_bms and "/buytickets/" in checkout_url.lower():
                nav_url = _derive_buytickets_url(checkout_url)
                if nav_url != checkout_url:
                    logger.info(
                        f"[{session_id}] Rewrote /buytickets/ URL for nav: "
                        f"{checkout_url} → {nav_url}"
                    )

            # ── Navigate to the actual event/checkout URL ────────────────
            logger.info(f"[{session_id}] Navigating → {nav_url}")
            _update(session_id, message="Navigating to event page...")
            await page.goto(nav_url, wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
            except Exception:
                pass

            # Snapshot cookies again after hitting the event page — this is
            # where Akamai/CF typically sets _abck/bm_sz/cf_clearance values
            # tied to the proxy IP. Logging names (not values) makes debugging
            # cookie-transfer issues much faster in Railway logs.
            try:
                post_nav_cookies = await ctx.cookies()
                nav_names = sorted({c.get("name", "") for c in post_nav_cookies})
                logger.info(
                    f"[{session_id}] 🍪 {len(post_nav_cookies)} cookies after "
                    f"event-page nav: {nav_names[:25]}"
                    f"{'…' if len(nav_names) > 25 else ''}"
                )
            except Exception:
                pass

            # ── BOT-CHECK DETECTION ──────────────────────────────────────
            # If Akamai served the bot-challenge page, bail early so we
            # don't loop trying to find seats on a challenge page.
            try:
                page_text = (await page.evaluate("document.body.innerText") or "").lower()
                bot_check_markers = [
                    "access denied", "reference #",
                    "blocked by bot", "verify you are a human",
                    "you don't have permission",
                    "request unsuccessful", "incapsula",
                ]
                if any(m in page_text for m in bot_check_markers):
                    logger.warning(
                        f"[{session_id}] Bot-check page detected — "
                        f"cookies still captured, cart_url will be derived"
                    )
                    _update(session_id,
                            message="Akamai bot-check hit — capturing cookies anyway...")
            except Exception:
                pass

            # Redirect-to-landing detection
            final_url_lower = page.url.lower()
            for junk in ("/cinemas", "/movies", "/home", "/explore",
                         "/offers", "/search"):
                if final_url_lower.rstrip("/").endswith(junk):
                    logger.warning(
                        f"[{session_id}] Redirected to landing ({page.url}) — "
                        f"skipping cart flow, capturing cookies only"
                    )
                    break

            # ── Cart flow ────────────────────────────────────────────────
            cart_url = ""
            cart_error = None
            try:
                if is_bms:
                    cart_url = await _run_bms_cart(
                        page, session_id, target_price, watcher_id, max_qty,
                        owner_email=owner_email
                    )
                elif is_district:
                    cart_url = await _run_district_cart(
                        page, session_id, target_price, watcher_id, max_qty,
                        checkout_url=checkout_url,
                    )
                else:
                    cart_url = page.url
            except Exception as e:
                cart_error = str(e)[:200]
                logger.error(f"[{session_id}] Cart flow crashed: {e}")
                # fall through — we STILL want to capture cookies

            # Strict validation — reject /cinemas, /movies, /home, root, etc.
            if not _is_useful_cart_url(cart_url):
                better_url = _derive_buytickets_url(checkout_url)
                logger.warning(
                    f"[{session_id}] cart_url was junk ({cart_url!r}) "
                    f"— replaced with {better_url}"
                )
                cart_url = better_url

            # ── Extract session cookies (the "VIP wristband") ─────────────
            # ALWAYS runs — even if the cart flow crashed or selected 0 seats.
            # The BMS/District session cookies (Akamai _abck, bm_sz, etc.) are
            # still valuable for the user to paste into Cookie-Editor so the
            # website recognizes their browser as "the one that passed bot
            # verification" and doesn't redirect them to /cinemas.
            cookie_payload = await _capture_cookies_safely(ctx, session_id)

            if cart_error:
                _update(session_id, status="cart_ready",
                        message=f"Partial — cart build hit an issue, but session cookies were captured. {cart_error}",
                        cart_url=cart_url,
                        cart_cookies=cookie_payload)
            else:
                _update(session_id, status="cart_ready",
                        message="Cart ready — tap Open Cart, or Copy Session to paste in Cookie-Editor.",
                        cart_url=cart_url,
                        cart_cookies=cookie_payload)
            logger.info(f"[{session_id}] Cart URL: {cart_url}")

            if watcher_id:
                _notify_cart_ready(watcher_id, cart_url, cookie_payload)
            return

        except Exception as e:
            logger.error(f"[{session_id}] Cart error: {e}")
            # Last-ditch: try to capture cookies even here
            try:
                cookie_payload = await _capture_cookies_safely(ctx, session_id)
            except Exception:
                cookie_payload = {"raw": [], "editthiscookie": [], "ok": False}
            fallback_url = _derive_buytickets_url(checkout_url)
            _update(session_id, status="cart_ready",
                    message=f"Cart flow failed, but cookies saved: {str(e)[:120]}",
                    cart_url=fallback_url,
                    cart_cookies=cookie_payload)
            if watcher_id:
                _notify_cart_ready(watcher_id, fallback_url, cookie_payload)
        finally:
            await ctx.close()
            await browser.close()


# ═════════════════════════════════════════════════════════════════════════════
# §11  CART NOTIFICATION
# ═════════════════════════════════════════════════════════════════════════════

# In-process hook — app.py registers _store_cart_url here at startup so the
# worker can deliver the cart URL directly without an HTTP roundtrip
# (which was failing with 404 on multi-process setups).
_cart_ready_hook = None  # type: ignore


def set_cart_ready_hook(fn):
    """Register a callable(watcher_id, cart_url) -> bool to be called from the worker."""
    global _cart_ready_hook
    _cart_ready_hook = fn


# ── Strict cart-URL filter (same rules as app.py) ─────────────────────────────
_JUNK_CART_PATHS = {
    "", "/", "/cinemas", "/movies", "/home", "/explore", "/search",
    "/offers", "/login", "/signin", "/signup", "/account", "/profile",
    "/plays", "/events", "/sports", "/activities", "/comedy",
}
_VALID_CART_TOKENS = (
    "buytickets", "cart", "checkout", "payment", "order",
    "ticket-options", "seat-layout", "booking", "book-now", "/ET",
)


async def _capture_cookies_safely(ctx, session_id: str) -> dict:
    """
    Capture Playwright session cookies safely.
    Logs name+domain (NOT values) to Railway so user can confirm capture.
    """
    try:
        raw = await ctx.cookies()
        if not raw:
            logger.warning(f"[{session_id}] Cookie capture returned 0 cookies")
            return {"raw": [], "editthiscookie": [], "ok": False}

        # Group by domain for a tidy summary
        by_domain = {}
        for c in raw:
            d = c.get("domain", "?")
            by_domain.setdefault(d, []).append(c.get("name", "?"))
        summary = "; ".join(f"{d}={len(ns)}" for d, ns in by_domain.items())
        logger.info(
            f"[{session_id}] 🎟️ CAPTURED {len(raw)} session cookies → {summary}"
        )
        # Also log names of key bot-gate cookies so user can confirm
        key_names = [c.get("name", "") for c in raw
                     if any(k in (c.get("name") or "").lower()
                            for k in ["_abck", "bm_sz", "bm_sv", "ak_bmsc",
                                      "sess", "auth", "jsessionid", "__cf",
                                      "cart", "queue"])]
        if key_names:
            logger.info(f"[{session_id}] 🔑 KEY cookies present: {key_names}")

        return {
            "raw":            raw,
            "editthiscookie": _to_editthiscookie_format(raw),
            "ok":             True,
        }
    except Exception as e:
        logger.error(f"[{session_id}] cookie capture failed: {e}")
        return {"raw": [], "editthiscookie": [], "ok": False}


def _to_editthiscookie_format(raw_cookies: list) -> list:
    """
    Convert Playwright cookie dicts to the schema that EditThisCookie /
    Cookie-Editor expect. Users can copy this JSON, open Cookie-Editor on
    the target domain, click 'Import', paste, and refresh — they inherit
    the bot's session wristband and land on the cart page directly.
    """
    out = []
    for c in raw_cookies or []:
        try:
            name = c.get("name") or ""
            if not name:
                continue
            expires = c.get("expires")
            # Playwright uses -1 / absent for session cookies
            is_session = expires is None or (isinstance(expires, (int, float)) and expires <= 0)
            domain = c.get("domain") or ""
            path = c.get("path") or "/"
            same_site = (c.get("sameSite") or "unspecified").lower()
            if same_site == "none":
                same_site = "no_restriction"
            elif same_site == "lax":
                same_site = "lax"
            elif same_site == "strict":
                same_site = "strict"
            else:
                same_site = "unspecified"

            entry = {
                "domain":     domain,
                "hostOnly":   not domain.startswith("."),
                "httpOnly":   bool(c.get("httpOnly", False)),
                "name":       name,
                "path":       path,
                "sameSite":   same_site,
                "secure":     bool(c.get("secure", False)),
                "session":    is_session,
                "storeId":    "0",
                "value":      c.get("value") or "",
            }
            if not is_session and expires is not None:
                entry["expirationDate"] = float(expires)
            out.append(entry)
        except Exception:
            continue
    return out


def _is_useful_cart_url(cart_url: str) -> bool:
    """True if cart_url looks like a real seat/cart/checkout URL."""
    if not cart_url or not cart_url.startswith("http"):
        return False
    from urllib.parse import urlparse
    try:
        parsed = urlparse(cart_url)
    except Exception:
        return False
    path = parsed.path or ""
    if path.rstrip("/").lower() in _JUNK_CART_PATHS:
        return False
    lowered = cart_url.lower()
    if not any(tok.lower() in lowered for tok in _VALID_CART_TOKENS):
        return False
    return True


def _notify_cart_ready(watcher_id: str, cart_url: str,
                       cookies: Optional[dict] = None):
    """Deliver cart URL + cookies to the Flask app. Prefers in-process hook."""
    if _cart_ready_hook is not None:
        try:
            try:
                ok = _cart_ready_hook(watcher_id, cart_url, cookies)
            except TypeError:
                # Legacy hook signature without cookies
                ok = _cart_ready_hook(watcher_id, cart_url)
            if ok:
                logger.info(f"Cart URL delivered in-process for watcher {watcher_id}")
                return
            logger.warning(f"In-process hook returned False for {watcher_id} — falling back to HTTP")
        except Exception as e:
            logger.warning(f"In-process hook raised ({e}) — falling back to HTTP")

    port = os.environ.get("PORT", "8000")
    try:
        import requests as req
        resp = req.post(
            f"http://127.0.0.1:{port}/api/watchers/{watcher_id}/cart-url",
            json={"cart_url": cart_url, "cookies": cookies},
            timeout=10,
        )
        if resp.ok:
            logger.info(f"Cart URL notification sent for watcher {watcher_id}")
        else:
            logger.warning(f"Cart URL notification failed: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not notify cart URL: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# §12  BACKGROUND WORKER THREAD
# ═════════════════════════════════════════════════════════════════════════════

_worker_thread: Optional[threading.Thread] = None
_worker_started = threading.Event()


# Maximum cart jobs to run CONCURRENTLY on the single worker loop.
# Each job spawns its own Playwright browser so we cap this to avoid
# exhausting the Railway container's memory/CPU. 3 is safe on a 512MB box.
_MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_CART_JOBS", "3"))


async def _process_job(job: "BookingJob"):
    """Wrapper: run a single booking job and log any failure."""
    sid = _session_id(job.watcher_id)
    try:
        logger.info(
            f"Worker processing: watcher={job.watcher_id} "
            f"url={job.checkout_url[:80]} target={job.target_price} "
            f"max_qty={job.max_qty}"
        )
        has_proxy = all([PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD])
        if not has_proxy:
            logger.warning(
                f"[{sid}] No proxy configured — running from local IP. "
                f"This may trigger Akamai blocks."
            )
        await _run_cart(
            sid, job.checkout_url,
            target_price=job.target_price,
            watcher_id=job.watcher_id,
            max_qty=job.max_qty,
            owner_email=job.owner_email,
        )
    except Exception as e:
        logger.error(f"[{sid}] Job failed: {e}", exc_info=True)
        try:
            _update(sid, status="failed", message=f"Job error: {str(e)[:160]}")
        except Exception:
            pass


async def _job_drain_loop():
    """
    FLAW 2 FIX: instead of `loop.run_until_complete` (which blocks the
    worker thread for the entire duration of ONE cart job), we keep a
    persistent drain loop that spawns each job as an independent task.
    This lets us process up to _MAX_CONCURRENT_JOBS tickets in parallel
    — critical during high-demand drops where seats evaporate in seconds.
    """
    sem = asyncio.Semaphore(_MAX_CONCURRENT_JOBS)
    logger.info(
        f"Cart worker drain loop started "
        f"(max concurrent jobs = {_MAX_CONCURRENT_JOBS})"
    )

    async def _run_one(job: "BookingJob"):
        async with sem:
            await _process_job(job)
        _job_queue.task_done()

    loop = asyncio.get_event_loop()
    while True:
        try:
            # Blocking queue.get() would freeze the loop — run it in a
            # thread-pool executor so the loop stays free to schedule tasks.
            job: BookingJob = await loop.run_in_executor(None, _job_queue.get)
            # Fire-and-forget: schedule as a concurrent task so the very
            # next queued job can start immediately in parallel (up to
            # the semaphore cap).
            asyncio.create_task(_run_one(job))
        except Exception as e:
            logger.error(f"Drain loop error: {e}", exc_info=True)
            # Tiny backoff so a persistent failure doesn't pin a CPU
            await asyncio.sleep(0.5)


def _worker_main():
    """Background thread entry point — owns its own asyncio event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _worker_started.set()
    logger.info("Cart worker thread started (dedicated asyncio loop)")
    try:
        loop.run_until_complete(_job_drain_loop())
    except Exception as e:
        logger.critical(f"Worker loop crashed: {e}", exc_info=True)
    finally:
        try:
            loop.close()
        except Exception:
            pass


def start_worker():
    """Start the background cart worker thread (called from gunicorn post_fork)."""
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(
        target=_worker_main, daemon=True, name="cart-worker"
    )
    _worker_thread.start()
    _worker_started.wait(timeout=5)
    logger.info("Cart worker thread is ready")


# ═════════════════════════════════════════════════════════════════════════════
# §13  PUBLIC ENTRY POINT — Called by Flask
# ═════════════════════════════════════════════════════════════════════════════

def trigger_auto_checkout(watcher_id: str, checkout_url: str,
                          target_price: str = "",
                          max_qty: int = DEFAULT_MAX_QTY,
                          owner_email: str = "",
                          **_ignored):
    """
    Enqueue a cart-add job for the background worker.

    Always cart-only: picks the cheapest price tier and max available seats,
    returns the cart link. NON-BLOCKING.

    Extra keyword arguments are silently ignored for backwards compatibility
    (e.g. legacy ``cart_mode`` flag).
    """
    _cleanup_stale_sessions()

    sid = _session_id(watcher_id)

    # Guard against duplicate jobs
    with _sessions_lock:
        existing = _sessions.get(sid, {}).get("status")
        if existing in ("running", "queued", "cart_ready"):
            logger.info(f"[{sid}] Already active ({existing}) — skipping")
            return
        # Reserve slot atomically
        _sessions[sid] = {
            "status":     "running",
            "message":    "Queued — waiting for worker...",
            "cart_url":   None,
            "created_at": time.time(),
        }

    job = BookingJob(
        watcher_id=watcher_id,
        checkout_url=checkout_url,
        target_price=target_price,
        max_qty=max_qty,
        owner_email=owner_email,
    )

    try:
        _job_queue.put_nowait(job)
        logger.info(f"[{sid}] Cart job enqueued (target={target_price}, max_qty={max_qty})")
    except queue.Full:
        logger.error(f"[{sid}] Job queue full — dropping")
        _update(sid, status="failed", message="Server busy — try again")
