/**
 * ticketAlert.js
 * Entry point — wires together inventory lookup, checkout, and notification.
 *
 * Usage:
 *   node src/ticketAlert.js
 *
 * Environment variables:
 *   API_BASE_URL   Base URL of the ticket API   (default: https://api.example.com)
 *   API_JWT        Bearer token for auth         (optional)
 *   EVENT_ID       Target event ID               (default: '12345')
 *   REQUESTED_QTY  Number of tickets needed      (default: 1)
 */

import { setJwt, setSharedHeaders } from './apiClient.js';
import { findBestAvailableListing } from './inventory.js';
import { reserveListing } from './checkout.js';

// ---------------------------------------------------------------------------
// Bootstrap — apply any environment-provided auth before the first request
// ---------------------------------------------------------------------------
if (process.env.API_JWT) {
  setJwt(process.env.API_JWT);
}

// Example: inject a correlation ID header for distributed tracing
setSharedHeaders({ 'X-Correlation-ID': crypto.randomUUID() });

// ---------------------------------------------------------------------------
// Notification handler — replace with your actual notification service call
// ---------------------------------------------------------------------------

/**
 * @param {{ expiresAt: string, cartUrl: string, expiresInSeconds: number }} payload
 */
function handleNotification(payload) {
  console.log('[Notification] Hold secured! Dispatching notification:');
  console.log(JSON.stringify(payload, null, 2));

  // TODO: forward `payload` to your email / SMS / push notification service
}

// ---------------------------------------------------------------------------
// Main workflow
// ---------------------------------------------------------------------------

async function main() {
  const eventId = process.env.EVENT_ID ?? '12345';
  const requestedQty = Number(process.env.REQUESTED_QTY ?? 1);

  console.log(`[TicketAlert] Checking inventory — event: ${eventId}, qty: ${requestedQty}`);

  const listing = await findBestAvailableListing(eventId, requestedQty);

  if (!listing) {
    console.log('[TicketAlert] No eligible listings found. Exiting.');
    return;
  }

  console.log(`[TicketAlert] Best listing: id=${listing.id}, price=${listing.price}`);

  await reserveListing(
    { listingId: listing.id, quantity: requestedQty, eventId },
    handleNotification
  );
}

main().catch((err) => {
  console.error('[TicketAlert] Fatal error:', err.friendlyMessage ?? err.message);
  process.exit(1);
});
