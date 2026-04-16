/**
 * otpGate.js
 * Human-in-the-Loop gate.
 *
 * The script reaches this module right before the final "Confirm / Pay" step.
 * It:
 *   1. Detects the OTP / 2FA screen (by URL pattern or DOM selector).
 *   2. Prints a clear terminal prompt so the user knows to act.
 *   3. Pauses — browser stays open for manual OTP entry.
 *   4. Resumes after the user presses Enter in the terminal.
 *   5. Observes the page for success or failure and returns the outcome.
 */

import readline from 'readline';

// --- Detection patterns ---

/** URL fragments that indicate the OTP / 2FA screen is active. */
const OTP_URL_PATTERNS = [/otp/i, /verify/i, /2fa/i, /authenticate/i, /confirm/i];

/** DOM selectors that indicate an OTP input is present. */
const OTP_SELECTORS = [
  'input[name*="otp"]',
  'input[id*="otp"]',
  'input[placeholder*="OTP"]',
  'input[placeholder*="Enter code"]',
  'input[autocomplete="one-time-code"]',
  '[class*="otp"]',
];

/** Selectors that signal a completed / successful transaction. */
const SUCCESS_SELECTORS = [
  '[class*="success"]',
  '[class*="confirmed"]',
  '[class*="booking-confirmed"]',
  'h1,h2,h3,p',   // fallback — checked with text filter below
];
const SUCCESS_TEXT_PATTERN = /success|confirmed|booked|order placed|thank you/i;

/** Selectors / text that signal failure. */
const FAILURE_TEXT_PATTERN = /failed|declined|error|invalid|expired/i;

// ---------------------------------------------------------------------------

/**
 * Waits for the OTP screen, then hands control to the user.
 * Returns once the user presses Enter in the terminal.
 *
 * @param {import('playwright').Page} page
 * @returns {Promise<void>}
 */
export async function waitForOtpAndResume(page) {
  console.log('\n[OTP Gate] Monitoring for OTP screen…');

  // Poll for OTP indicators (URL or DOM) for up to 60 s
  const detected = await _pollForOtpScreen(page, 60_000);

  if (detected) {
    console.log('\n╔══════════════════════════════════════════════════╗');
    console.log('║        *** ACTION REQUIRED — OTP SCREEN ***      ║');
    console.log('║                                                  ║');
    console.log('║  1. Look at the browser window.                  ║');
    console.log('║  2. Enter the OTP / 2FA code sent to your phone. ║');
    console.log('║  3. Click "Confirm" / "Submit" in the browser.   ║');
    console.log('║  4. Then press  [Enter]  here to continue.       ║');
    console.log('╚══════════════════════════════════════════════════╝\n');
  } else {
    console.log('[OTP Gate] OTP screen not detected — pausing for manual review.');
    console.log('[OTP Gate] Complete any pending steps in the browser,');
    console.log('           then press [Enter] here to continue.\n');
  }

  await _waitForEnter();
  console.log('[OTP Gate] Resumed — observing final outcome…');
}

/**
 * Observes the page after the user resumes and returns a structured result.
 *
 * @param {import('playwright').Page} page
 * @returns {Promise<{ success: boolean, message: string, url: string }>}
 */
export async function observeOutcome(page) {
  // Give the page up to 15 s to settle after the user acts
  await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});

  const url     = page.url();
  const content = await page.textContent('body').catch(() => '');

  if (SUCCESS_TEXT_PATTERN.test(content) || SUCCESS_TEXT_PATTERN.test(url)) {
    return { success: true,  message: 'Booking confirmed.', url };
  }
  if (FAILURE_TEXT_PATTERN.test(content) || FAILURE_TEXT_PATTERN.test(url)) {
    return { success: false, message: 'Transaction failed or was declined.', url };
  }

  return { success: false, message: 'Outcome unclear — check browser.', url };
}

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

async function _pollForOtpScreen(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    // Check URL
    if (OTP_URL_PATTERNS.some((p) => p.test(page.url()))) return true;

    // Check DOM
    for (const sel of OTP_SELECTORS) {
      const el = await page.$(sel).catch(() => null);
      if (el) return true;
    }

    await new Promise((r) => setTimeout(r, 1_000));
  }
  return false;
}

function _waitForEnter() {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin });
    rl.question('  → Press [Enter] when done: ', () => {
      rl.close();
      resolve();
    });
  });
}
