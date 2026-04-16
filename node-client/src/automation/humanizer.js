/**
 * humanizer.js
 * Injects realistic human-like behaviour (random scrolls, mouse movement,
 * hover dwell time) before high-value interactions like clicking "Buy Now".
 * Reduces the likelihood of bot-detection heuristics triggering.
 */

/** Returns a random integer in [min, max] inclusive. */
function rand(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

/** Resolves after a random delay in [minMs, maxMs]. */
function randomDelay(minMs, maxMs) {
  return new Promise((res) => setTimeout(res, rand(minMs, maxMs)));
}

/**
 * Moves the mouse in a curved arc from current position to the target element,
 * dwells briefly, then returns.
 *
 * @param {import('playwright').Page} page
 * @param {import('playwright').ElementHandle | import('playwright').Locator} target
 */
export async function hoverNaturally(page, target) {
  const box = await target.boundingBox();
  if (!box) return;

  // Land slightly off-centre to avoid dead-centre clicks (a bot tell)
  const destX = box.x + box.width  * (0.3 + Math.random() * 0.4);
  const destY = box.y + box.height * (0.3 + Math.random() * 0.4);

  // Move in small steps to simulate cursor acceleration / deceleration
  const steps = rand(8, 18);
  await page.mouse.move(destX, destY, { steps });

  // Dwell on the element before acting (200–600 ms)
  await randomDelay(200, 600);
}

/**
 * Performs a natural-feeling scroll sequence on the page:
 * down a bit, pause, maybe scroll back up slightly, then settle.
 *
 * @param {import('playwright').Page} page
 */
export async function naturalScroll(page) {
  const scrollDistance = rand(200, 500);

  await page.mouse.wheel(0, scrollDistance);
  await randomDelay(400, 900);

  // Occasionally scroll back up a little (mimics reading behaviour)
  if (Math.random() > 0.5) {
    await page.mouse.wheel(0, -rand(50, 150));
    await randomDelay(300, 700);
  }

  await page.mouse.wheel(0, rand(50, 150));
  await randomDelay(200, 500);
}

/**
 * Full pre-click ritual: scroll the page, move to the element, hover, then click.
 * Use this instead of a bare `element.click()` on the "Buy Now" button.
 *
 * @param {import('playwright').Page} page
 * @param {import('playwright').Locator} locator  The button to click.
 */
export async function humanClick(page, locator) {
  await naturalScroll(page);
  await hoverNaturally(page, locator);

  // Small delay between hover and click (100–300 ms)
  await randomDelay(100, 300);
  await locator.click();
}

/**
 * Types a string character-by-character with randomised inter-key delays
 * to mimic natural typing rhythm.
 *
 * @param {import('playwright').Locator} locator  Input field.
 * @param {string} text
 */
export async function humanType(locator, text) {
  await locator.click();
  await randomDelay(80, 200);

  for (const char of text) {
    await locator.pressSequentially(char, { delay: rand(40, 120) });
  }
}
