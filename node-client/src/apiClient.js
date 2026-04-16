/**
 * apiClient.js
 * Centralised Axios instance with header management, session state,
 * and exponential-backoff retry logic.
 */

import axios from 'axios';

// ---------------------------------------------------------------------------
// Session state — mutate via the helpers exported below, never directly.
// ---------------------------------------------------------------------------
const session = {
  jwt: null,
  cookies: {},
};

// ---------------------------------------------------------------------------
// Backoff configuration
// ---------------------------------------------------------------------------
const RETRY_CONFIG = {
  maxRetries: 4,
  baseDelayMs: 500,   // first retry waits ~500 ms
  maxDelayMs: 16_000, // cap at 16 s
  jitterFactor: 0.2,  // ±20 % random jitter to avoid thundering herd
};

// ---------------------------------------------------------------------------
// Axios instance
// ---------------------------------------------------------------------------
export const client = axios.create({
  baseURL: process.env.API_BASE_URL ?? 'https://api.example.com',
  timeout: 10_000,
  headers: {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'User-Agent': 'TicketAlert/1.0 Node.js',
  },
});

// ---------------------------------------------------------------------------
// Request interceptor — injects live auth/session headers before every call
// ---------------------------------------------------------------------------
client.interceptors.request.use((config) => {
  if (session.jwt) {
    config.headers['Authorization'] = `Bearer ${session.jwt}`;
  }

  const cookieString = Object.entries(session.cookies)
    .map(([k, v]) => `${k}=${v}`)
    .join('; ');
  if (cookieString) {
    config.headers['Cookie'] = cookieString;
  }

  return config;
});

// ---------------------------------------------------------------------------
// Response interceptor — captures Set-Cookie headers returned by the server
// ---------------------------------------------------------------------------
client.interceptors.response.use((response) => {
  const setCookie = response.headers['set-cookie'];
  if (setCookie) {
    for (const raw of [setCookie].flat()) {
      const [pair] = raw.split(';');
      const [name, value] = pair.split('=');
      if (name && value !== undefined) {
        session.cookies[name.trim()] = value.trim();
      }
    }
  }
  return response;
});

// ---------------------------------------------------------------------------
// Session helpers
// ---------------------------------------------------------------------------

/** Replace the active JWT (call after login / token refresh). */
export function setJwt(token) {
  session.jwt = token;
}

/** Merge extra headers into every future request (e.g. X-Request-ID). */
export function setSharedHeaders(headers = {}) {
  Object.assign(client.defaults.headers.common, headers);
}

/** Wipe auth state (call on logout or session expiry). */
export function clearSession() {
  session.jwt = null;
  session.cookies = {};
}

// ---------------------------------------------------------------------------
// Exponential backoff wrapper
// ---------------------------------------------------------------------------

/**
 * Executes `fn` (an async function that returns a Promise) with automatic
 * retries on transient failures.
 *
 * Retryable: network errors, 429 Too Many Requests, 5xx server errors.
 * Non-retryable: 4xx client errors (except 429).
 *
 * @param {() => Promise<import('axios').AxiosResponse>} fn
 * @param {Partial<typeof RETRY_CONFIG>} [retryOptions]
 * @returns {Promise<import('axios').AxiosResponse>}
 */
export async function withRetry(fn, retryOptions = {}) {
  const { maxRetries, baseDelayMs, maxDelayMs, jitterFactor } = {
    ...RETRY_CONFIG,
    ...retryOptions,
  };

  let attempt = 0;

  while (true) {
    try {
      return await fn();
    } catch (err) {
      const status = err.response?.status;
      const isRetryable =
        !status ||                     // network/timeout error
        status === 429 ||              // rate-limited
        (status >= 500 && status < 600); // server error

      if (!isRetryable) {
        throw enrichError(err);
      }

      if (attempt >= maxRetries) {
        throw enrichError(err, `Max retries (${maxRetries}) exceeded`);
      }

      // Honour Retry-After header when the server provides it (RFC 7231)
      const retryAfterSec = parseRetryAfter(err.response?.headers?.['retry-after']);
      const exponential = Math.min(baseDelayMs * 2 ** attempt, maxDelayMs);
      const jitter = exponential * jitterFactor * (Math.random() * 2 - 1);
      const delay = retryAfterSec != null
        ? retryAfterSec * 1_000
        : Math.round(exponential + jitter);

      console.warn(
        `[TicketAlert] Attempt ${attempt + 1} failed (HTTP ${status ?? 'network'}). ` +
        `Retrying in ${delay} ms…`
      );

      await sleep(delay);
      attempt++;
    }
  }
}

// ---------------------------------------------------------------------------
// Internal utilities
// ---------------------------------------------------------------------------

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseRetryAfter(header) {
  if (!header) return null;
  const seconds = Number(header);
  return Number.isFinite(seconds) ? seconds : null;
}

function enrichError(err, extra = '') {
  const status = err.response?.status;
  const message = err.response?.data?.message ?? err.message;
  const label =
    status === 404 ? 'Resource not found (404)' :
    status === 429 ? 'Rate limit exceeded (429)' :
    status >= 500  ? `Server error (${status})` :
    status         ? `Client error (${status})` :
    'Network / timeout error';

  err.friendlyMessage = extra ? `${label}: ${extra}` : `${label} — ${message}`;
  return err;
}
