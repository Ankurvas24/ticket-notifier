/**
 * scraper.js
 * Navigates to the event URL, scrapes available inventory from the DOM
 * (with an API-intercept fallback), and returns the best selection:
 *   → maximum permitted quantity at the lowest available price.
 */

/**
 * @typedef {{ priceText: string, priceValue: number, quantity: number, selector: string }} InventoryItem
 * @typedef {{ item: InventoryItem, requestedQty: number }} Selection
 */

// CSS selectors — adjust to match the target site's DOM structure.
// Listed in priority order; the first matching set wins.
const SELECTOR_PRESETS = [
  {
    name: 'BookMyShow-style',
    priceContainer: '[class*="price"], [data-price], .ticket-price',
    quantityInput:  'select[name*="qty"], select[id*="qty"], input[type="number"][name*="qty"]',
    maxQtyAttr:     'max',                      // attribute on the qty input
    addButton:      'button[class*="add"], button[class*="buy"], [data-action="add-to-cart"]',
  },
  {
    name: 'Generic fallback',
    priceContainer: '[class*="price"], [class*="cost"], [class*="fare"]',
    quantityInput:  'select, input[type="number"]',
    maxQtyAttr:     'max',
    addButton:      'button[type="submit"], button[class*="proceed"]',
  },
];

/**
 * Navigates to `url`, discovers inventory, and returns the best selection.
 *
 * @param {import('playwright').Page} page
 * @param {string} url
 * @returns {Promise<Selection | null>}
 */
export async function discoverAndSelect(page, url) {
  console.log(`[Scraper] Navigating to ${url}`);

  // Intercept XHR/fetch inventory responses as a supplementary signal
  const apiInventory = await _interceptInventoryApi(page);

  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 });
  await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});

  // Try each selector preset until one yields results
  for (const preset of SELECTOR_PRESETS) {
    const items = await _scrapeItems(page, preset);
    if (items.length > 0) {
      const best = _selectBest(items);
      const requestedQty = await _resolveMaxQty(page, preset, best);
      console.log(
        `[Scraper] Selected via "${preset.name}": ` +
        `price=${best.priceValue}, qty=${requestedQty}`
      );
      return { item: best, requestedQty };
    }
  }

  // Fall back to API-intercepted data if DOM scraping found nothing
  if (apiInventory.length > 0) {
    console.log('[Scraper] Using API-intercepted inventory data.');
    const best = _selectBest(apiInventory);
    return { item: best, requestedQty: best.quantity };
  }

  console.warn('[Scraper] No inventory found.');
  return null;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

async function _scrapeItems(page, preset) {
  try {
    await page.waitForSelector(preset.priceContainer, { timeout: 5_000 });
  } catch {
    return [];
  }

  return page.$$eval(preset.priceContainer, (els) =>
    els.map((el, index) => {
      const raw = el.textContent?.trim() ?? '';
      const numeric = parseFloat(raw.replace(/[^0-9.]/g, ''));
      return {
        priceText: raw,
        priceValue: isNaN(numeric) ? Infinity : numeric,
        quantity: 1,                      // refined later by _resolveMaxQty
        selector: `[data-scraper-index="${index}"]`,
        element: null,                    // non-serialisable — resolved on-page
      };
    }).filter((i) => i.priceValue < Infinity)
  );
}

function _selectBest(items) {
  return items.reduce((best, item) =>
    item.priceValue < best.priceValue ? item : best
  );
}

async function _resolveMaxQty(page, preset, item) {
  try {
    const qtyEl = await page.$(preset.quantityInput);
    if (!qtyEl) return 1;

    const maxAttr = await qtyEl.getAttribute(preset.maxQtyAttr);
    const maxVal = parseInt(maxAttr ?? '1', 10);
    return Number.isFinite(maxVal) && maxVal > 0 ? maxVal : 1;
  } catch {
    return 1;
  }
}

/**
 * Registers a route intercept to capture JSON inventory payloads
 * from common API patterns (/inventory, /availability, /seats).
 */
async function _interceptInventoryApi(page) {
  const captured = [];

  await page.route(/\/(inventory|availability|seats|tickets)/i, async (route) => {
    const response = await route.fetch();
    try {
      const json = await response.json();
      const list = Array.isArray(json) ? json : json?.data ?? json?.items ?? [];
      for (const entry of list) {
        if (entry.price != null) {
          captured.push({
            priceText: String(entry.price),
            priceValue: Number(entry.price),
            quantity: entry.quantity ?? entry.maxQuantity ?? 1,
            selector: null,
          });
        }
      }
    } catch { /* non-JSON response — ignore */ }
    await route.fulfill({ response });
  });

  return captured;
}
