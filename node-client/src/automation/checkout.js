/**
 * checkout.js
 * Entry point for the browser automation workflow.
 *
 * Flow:
 *   1. Open event URL  →  discover inventory  →  select best listing
 *   2. Set quantity, click "Buy Now" (with human-like interaction)
 *   3. Fill personal + payment fields
 *   4. Pause at OTP gate  →  user enters OTP manually
 *   5. Observe and log final outcome
 *
 * Usage:
 *   EVENT_URL=https://... node src/automation/checkout.js
 */

import { newPage, closeBrowser }      from './browser.js';
import { discoverAndSelect }          from './scraper.js';
import { fillPersonalDetails,
         fillPaymentDetails,
         setQuantity }                from './injector.js';
import { humanClick, naturalScroll }  from './humanizer.js';
import { waitForOtpAndResume,
         observeOutcome }             from './otpGate.js';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const EVENT_URL = process.env.EVENT_URL;

/** Selectors for the "Buy Now" / "Add to cart" / "Proceed" button. */
const BUY_BUTTON_SELECTORS = [
  'button[class*="buy"]',
  'button[class*="Book"]',
  'button[class*="book"]',
  'button[class*="proceed"]',
  'button[class*="checkout"]',
  'a[class*="buy"]',
  '[data-action="buy"]',
  'button[type="submit"]',
];

/** Selectors for the final "Confirm / Pay" button (before OTP). */
const CONFIRM_BUTTON_SELECTORS = [
  'button[class*="confirm"]',
  'button[class*="pay"]',
  'button[class*="Place"]',
  'button[class*="place"]',
  'button[type="submit"]',
];

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function run() {
  if (!EVENT_URL) {
    console.error('[Checkout] EVENT_URL environment variable is required.');
    console.error('  Usage: EVENT_URL=https://... node src/automation/checkout.js');
    process.exit(1);
  }

  const page = await newPage();

  try {
    // ── Step 1: Discover & select best listing ──────────────────────────────
    const selection = await discoverAndSelect(page, EVENT_URL);
    if (!selection) {
      console.log('[Checkout] No eligible listings found. Exiting.');
      return;
    }

    console.log(
      `[Checkout] Will attempt to buy ${selection.requestedQty}x ` +
      `at ${selection.item.priceText}`
    );

    // ── Step 2: Set quantity & click "Buy Now" ──────────────────────────────
    await setQuantity(page, selection.requestedQty);

    const buyButton = await _findButton(page, BUY_BUTTON_SELECTORS);
    if (!buyButton) {
      console.warn('[Checkout] "Buy" button not found — check selectors or navigate manually.');
    } else {
      await humanClick(page, buyButton);
      console.log('[Checkout] "Buy Now" clicked.');
    }

    // Wait for the checkout/cart page to load
    await page.waitForLoadState('domcontentloaded', { timeout: 15_000 }).catch(() => {});
    await naturalScroll(page);

    // ── Step 3: Fill personal + payment details ─────────────────────────────
    await fillPersonalDetails(page);
    await fillPaymentDetails(page);

    // ── Step 4: Click "Confirm / Pay" (reaches OTP screen) ──────────────────
    const confirmButton = await _findButton(page, CONFIRM_BUTTON_SELECTORS);
    if (confirmButton) {
      await humanClick(page, confirmButton);
      console.log('[Checkout] Confirm/Pay clicked — waiting for OTP screen…');
    } else {
      console.warn('[Checkout] Confirm button not found — please click it manually.');
    }

    // ── Step 5: OTP Gate — hand off to user ─────────────────────────────────
    await waitForOtpAndResume(page);

    // ── Step 6: Observe and report outcome ──────────────────────────────────
    const result = await observeOutcome(page);
    _logResult(result);

  } catch (err) {
    console.error('[Checkout] Fatal error:', err.message);
  } finally {
    // Keep the browser open for a moment so the user can review the result
    await new Promise((r) => setTimeout(r, 5_000));
    await closeBrowser();
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Returns the first visible button matching any of the provided selectors.
 *
 * @param {import('playwright').Page} page
 * @param {string[]} selectors
 * @returns {Promise<import('playwright').Locator | null>}
 */
async function _findButton(page, selectors) {
  for (const sel of selectors) {
    try {
      const loc = page.locator(sel).first();
      await loc.waitFor({ state: 'visible', timeout: 4_000 });
      return loc;
    } catch { /* try next */ }
  }
  return null;
}

function _logResult(result) {
  const border = '═'.repeat(52);
  console.log(`\n╔${border}╗`);
  console.log(`║  RESULT: ${result.success ? '✅ SUCCESS' : '❌ FAILED'}${' '.repeat(41)}║`);
  console.log(`║  ${result.message.padEnd(50)}║`);
  console.log(`║  URL: ${result.url.slice(0, 46).padEnd(46)}║`);
  console.log(`╚${border}╝\n`);
}

// ---------------------------------------------------------------------------

run();
