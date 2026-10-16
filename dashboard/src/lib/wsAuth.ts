import { getConfig } from './config';

/**
 * The ONLY place that knows how the WebSocket authenticates.
 *
 * Today the backend accepts the JWT as a `?token=` query parameter on /api/v1/ws/live.
 * That puts the token in URLs (proxy/access logs). The backend will later offer a
 * short-lived ticket; switching then means changing just this function (e.g. fetch a
 * ticket with api.* and return `?ticket=...`, making this async).
 */
export function buildWsUrl(token: string | null): string | null {
  if (!token) return null;
  return `${getConfig().wsUrl}/api/v1/ws/live?token=${encodeURIComponent(token)}`;
}

/** Close code the backend uses for a missing/invalid/expired token (routers/ws.py). */
export const WS_UNAUTHORIZED = 4401;
