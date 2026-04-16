/**
 * inventory.js
 * Fetches and processes inventory data for a given event.
 */

import { client, withRetry } from './apiClient.js';

/**
 * Fetches inventory for `eventId` and returns the cheapest ticket listing
 * that satisfies the availability and quantity requirements.
 *
 * @param {string} eventId
 * @param {number} requestedQty  Minimum quantity needed.
 * @returns {Promise<InventoryItem | null>}
 *
 * @typedef {{ id: string, price: number, quantity: number, available: boolean, [key: string]: unknown }} InventoryItem
 */
export async function findBestAvailableListing(eventId, requestedQty) {
  if (!eventId) throw new TypeError('eventId is required');
  if (!Number.isInteger(requestedQty) || requestedQty < 1) {
    throw new RangeError('requestedQty must be a positive integer');
  }

  const response = await withRetry(() =>
    client.get('/api/v1/inventory', { params: { eventId } })
  );

  /** @type {InventoryItem[]} */
  const listings = response.data;

  if (!Array.isArray(listings)) {
    throw new TypeError(`Expected array from inventory API, got: ${typeof listings}`);
  }

  const eligible = listings.filter(
    (item) => item.available === true && item.quantity >= requestedQty
  );

  if (eligible.length === 0) return null;

  // Return the listing with the lowest price (stable sort — first match wins ties)
  return eligible.reduce((best, item) => (item.price < best.price ? item : best));
}
