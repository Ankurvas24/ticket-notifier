/**
 * browser.js
 * Launches a persistent Chromium context so session cookies, localStorage,
 * and auth tokens survive across script runs.
 *
 * headless: false  — required so the user can interact with the OTP screen.
 */

import { chromium } from 'playwright';
import path from 'path';
import os from 'os';

// Cookies / storage are persisted here between runs
const PROFILE_DIR = path.join(os.homedir(), '.ticketalert', 'browser-profile');

let _browser = null;
let _context = null;

/**
 * Returns (and lazily creates) the shared persistent browser context.
 * @returns {Promise<import('playwright').BrowserContext>}
 */
export async function getContext() {
  if (_context) return _context;

  _browser = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: false,
    channel: 'chromium',
    viewport: { width: 1280, height: 800 },
    locale: 'en-US',
    timezoneId: 'Asia/Kolkata',
    args: [
      '--disable-blink-features=AutomationControlled', // suppress navigator.webdriver flag
      '--no-sandbox',
    ],
    // Mimic a real Chrome user-agent
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ' +
      'AppleWebKit/537.36 (KHTML, like Gecko) ' +
      'Chrome/124.0.0.0 Safari/537.36',
  });

  _context = _browser;
  return _context;
}

/**
 * Opens a new page inside the shared context.
 * @returns {Promise<import('playwright').Page>}
 */
export async function newPage() {
  const ctx = await getContext();
  const page = await ctx.newPage();

  // Mask automation fingerprints at the JS layer
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
  });

  return page;
}

/** Gracefully close the browser. */
export async function closeBrowser() {
  if (_browser) {
    await _browser.close();
    _browser = null;
    _context = null;
  }
}
