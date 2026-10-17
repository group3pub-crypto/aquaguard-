import { api } from './api';
import { getConfig } from './config';

/**
 * The ONLY place that knows how the WebSocket authenticates.
 *
 * The backend no longer accepts the JWT in the URL (it would land in proxy/access logs).
 * Instead, before EVERY connect and reconnect we trade the access token for a single-use
 * ticket (valid 30 s) via POST /auth/ws-ticket and connect with `?ticket=...`. A ticket is
 * burned on use, so it must never be cached or reused for a reconnect.
 */
export async function buildWsUrl(): Promise<string> {
  const { ticket } = await api.wsTicket();
  return `${getConfig().wsUrl}/api/v1/ws/live?ticket=${encodeURIComponent(ticket)}`;
}

/** Close codes the backend uses (routers/ws.py). */
export const WS_UNAUTHORIZED = 4401;
export const WS_FORBIDDEN = 4403;
export const WS_IDLE = 4408;
export const WS_TOO_MANY = 4429;

/** Human text for the connection banner, or null for ordinary (network) closes. */
export function wsCloseMessage(code: number): string | null {
  switch (code) {
    case WS_UNAUTHORIZED:
      return 'Your session for the live feed expired.';
    case WS_FORBIDDEN:
      return 'Your account has no live-feed access (nothing is linked to it yet). Contact your administrator.';
    case WS_TOO_MANY:
      return 'Too many live connections for this account (close other tabs). Retrying shortly.';
    case WS_IDLE:
      return 'The live feed was closed for inactivity. Reconnecting.';
    default:
      return null;
  }
}
