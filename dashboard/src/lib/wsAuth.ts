import { getConfig } from './config';
import { api } from './api';

/**
 * The ONLY place that knows how the WebSocket authenticates.
 *
 * The backend issues a single-use, 30 s ticket (POST /api/v1/auth/ws-ticket) and connects
 * with `/api/v1/ws/live?ticket=...`; it does NOT accept the JWT itself (that would put it in
 * URLs/access logs). A fresh ticket must be fetched for every connection attempt, including
 * reconnects, since a ticket cannot be reused.
 */
export async function buildWsUrl(authenticated: boolean): Promise<string | null> {
  if (!authenticated) return null;
  const { ticket } = await api.wsTicket();
  return `${getConfig().wsUrl}/api/v1/ws/live?ticket=${encodeURIComponent(ticket)}`;
}

/** Close code the backend uses for a missing/invalid/expired ticket (routers/ws.py). */
export const WS_UNAUTHORIZED = 4401;
