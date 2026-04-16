/**
 * checkout.js
 * Sequential cart-add → checkout-hold workflow with notification trigger.
 */

import { client, withRetry } from './apiClient.js';

// ---------------------------------------------------------------------------
// Step 1 — Add item to cart
// ---------------------------------------------------------------------------

/**
 * @param {{ listingId: string, quantity: number, eventId: string }} payload
 * @returns {Promise<{ cartId: string, [key: string]: unknown }>}
 */
async function addToCart(payload) {
  const response = await withRetry(() =>
    client.post('/api/cart/add', payload)
  );
  return response.data;
}

// ---------------------------------------------------------------------------
// Step 2 — Place a hold on the cart
// ---------------------------------------------------------------------------

/**
 * @param {string} cartId
 * @returns {Promise<{ status: string, expires_at: string, cart_url: string, [key: string]: unknown }>}
 */
async function holdCart(cartId) {
  const response = await withRetry(() =>
    client.post('/checkout/hold', { cartId })
  );
  return response.data;
}

// ---------------------------------------------------------------------------
// Notification formatter
// ---------------------------------------------------------------------------

/**
 * Formats a successful hold into the shape expected by the notification service.
 *
 * @param {string} expires_at   ISO-8601 timestamp from the hold response.
 * @param {string} cart_url     Direct checkout URL from the hold response.
 * @returns {{ expiresAt: string, cartUrl: string, expiresInSeconds: number }}
 */
function formatNotificationPayload(expires_at, cart_url) {
  const expiresAt = new Date(expires_at);
  const expiresInSeconds = Math.max(
    0,
    Math.floor((expiresAt.getTime() - Date.now()) / 1_000)
  );

  return {
    expiresAt: expiresAt.toISOString(),
    cartUrl: cart_url,
    expiresInSeconds,
  };
}

// ---------------------------------------------------------------------------
// Orchestrated workflow
// ---------------------------------------------------------------------------

/**
 * Executes the full cart-add → hold sequence for a selected listing.
 * Calls `onHeld` with a structured notification payload if the hold succeeds.
 *
 * @param {{ listingId: string, quantity: number, eventId: string }} listing
 * @param {(payload: ReturnType<typeof formatNotificationPayload>) => void} onHeld
 *   Callback invoked when the hold status is 'held'.
 * @returns {Promise<void>}
 */
export async function reserveListing(listing, onHeld) {
  // --- Step 1: Add to cart ---
  console.log(`[Checkout] Adding listing ${listing.listingId} to cart…`);
  const cartResult = await addToCart({
    listingId: listing.listingId,
    quantity: listing.quantity,
    eventId: listing.eventId,
  });

  const { cartId } = cartResult;
  if (!cartId) throw new Error('Cart response did not include a cartId');
  console.log(`[Checkout] Cart created: ${cartId}`);

  // --- Step 2: Hold the cart ---
  console.log(`[Checkout] Placing hold on cart ${cartId}…`);
  const holdResult = await holdCart(cartId);
  console.log(`[Checkout] Hold status: ${holdResult.status}`);

  // --- Step 3: Notify if held ---
  if (holdResult.status === 'held') {
    const notificationPayload = formatNotificationPayload(
      holdResult.expires_at,
      holdResult.cart_url
    );
    onHeld(notificationPayload);
  } else {
    console.warn(`[Checkout] Hold returned unexpected status: "${holdResult.status}"`);
  }
}
