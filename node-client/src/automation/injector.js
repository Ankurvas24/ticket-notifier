/**
 * injector.js
 * Fills checkout / payment form fields using a "wait-for-selector" strategy.
 * Each field is only filled after its element is confirmed visible in the DOM,
 * which handles late-loading iframes and dynamic form reveals.
 */

import { humanType } from './humanizer.js';
import { UserBillingProfile as P } from './profile.js';

const TIMEOUT = 15_000; // ms to wait for any single selector

/**
 * Waits for `selector` to be visible, then fills it with `value`.
 * Silent no-op if the selector is absent (field doesn't exist on this page).
 *
 * @param {import('playwright').Page | import('playwright').FrameLocator} scope
 * @param {string} selector
 * @param {string} value
 * @param {boolean} [useHumanType=false]  Type char-by-char instead of fill().
 */
async function safeType(scope, selector, value, useHumanType = false) {
  if (!value) return;
  try {
    const locator = scope.locator(selector).first();
    await locator.waitFor({ state: 'visible', timeout: TIMEOUT });
    if (useHumanType) {
      await humanType(locator, value);
    } else {
      await locator.fill(value);
    }
  } catch {
    // Field not present on this checkout page — skip silently
  }
}

/**
 * Fills the standard personal / address fields on the checkout page.
 *
 * @param {import('playwright').Page} page
 */
export async function fillPersonalDetails(page) {
  console.log('[Injector] Filling personal details…');

  await safeType(page, 'input[name*="first"], input[id*="first"]',   P.firstName);
  await safeType(page, 'input[name*="last"],  input[id*="last"]',    P.lastName);
  await safeType(page, 'input[name*="email"], input[id*="email"], input[type="email"]', P.email);
  await safeType(page, 'input[name*="phone"], input[id*="phone"], input[type="tel"]',   P.phone);
  await safeType(page, 'input[name*="address"], input[id*="address"]', P.address);
  await safeType(page, 'input[name*="city"],  input[id*="city"]',    P.city);
  await safeType(page, 'input[name*="zip"],   input[id*="zip"],  input[name*="pincode"]', P.zip);
}

/**
 * Fills payment card fields.
 * Handles both inline forms and payment iframes (e.g. Stripe, Razorpay).
 *
 * @param {import('playwright').Page} page
 */
export async function fillPaymentDetails(page) {
  console.log('[Injector] Waiting for payment form…');

  // --- Try inline form first ---
  const inlineCard = page.locator(
    'input[name*="card"], input[id*="card"], input[name*="cardnumber"]'
  ).first();

  const inlineVisible = await inlineCard
    .waitFor({ state: 'visible', timeout: 8_000 })
    .then(() => true)
    .catch(() => false);

  if (inlineVisible) {
    await _fillCardFields(page);
    return;
  }

  // --- Fall back: look for a payment iframe (Stripe / Razorpay style) ---
  console.log('[Injector] Inline form not found — scanning for payment iframe…');
  const iframeSelectors = [
    'iframe[name*="card"]',
    'iframe[src*="stripe"]',
    'iframe[src*="razorpay"]',
    'iframe[src*="payu"]',
    'iframe[title*="payment"]',
    'iframe[title*="card"]',
  ];

  for (const sel of iframeSelectors) {
    try {
      await page.waitForSelector(sel, { timeout: 5_000 });
      const frameLocator = page.frameLocator(sel);
      await _fillCardFields(frameLocator);
      console.log(`[Injector] Filled card fields inside iframe: ${sel}`);
      return;
    } catch { /* try next */ }
  }

  console.warn('[Injector] Payment fields not found — manual entry required.');
}

/**
 * Fills the four card fields inside `scope` (page or frameLocator).
 * Uses humanType for card number to mimic real typing cadence.
 *
 * @param {import('playwright').Page | import('playwright').FrameLocator} scope
 */
async function _fillCardFields(scope) {
  await safeType(
    scope,
    'input[name*="number"], input[id*="number"], input[placeholder*="Card number"]',
    P.cardNumber,
    true    // char-by-char for card number
  );
  await safeType(
    scope,
    'input[name*="expiry"], input[id*="expiry"], input[placeholder*="MM"], input[name*="exp"]',
    P.cardExpiry
  );
  await safeType(
    scope,
    'input[name*="cvv"], input[id*="cvv"], input[name*="cvc"], input[placeholder*="CVV"]',
    P.cardCvv
  );
  await safeType(
    scope,
    'input[name*="name"], input[id*="name"], input[placeholder*="Name on card"]',
    P.cardName
  );
}

/**
 * Sets a <select> or radio-button quantity control to the desired value.
 *
 * @param {import('playwright').Page} page
 * @param {number} qty
 */
export async function setQuantity(page, qty) {
  const qtySelectors = [
    'select[name*="qty"]',
    'select[id*="qty"]',
    'select[name*="quantity"]',
    'input[type="number"][name*="qty"]',
    'input[type="number"][name*="quantity"]',
  ];

  for (const sel of qtySelectors) {
    try {
      const el = page.locator(sel).first();
      await el.waitFor({ state: 'visible', timeout: 4_000 });
      await el.selectOption(String(qty)).catch(() => el.fill(String(qty)));
      console.log(`[Injector] Quantity set to ${qty}`);
      return;
    } catch { /* try next */ }
  }

  console.warn('[Injector] Quantity selector not found — defaulting to 1.');
}
