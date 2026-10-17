// Token storage tradeoff: the short-lived access JWT (15 min) and the rotating refresh
// token are held in module memory and mirrored to sessionStorage so a page reload keeps
// the session. We deliberately avoid localStorage: it persists across tabs/restarts and
// widens the window for token theft via XSS. sessionStorage is still script-readable (an
// XSS bug could exfiltrate it), so we also rely on a strict CSP and never render raw HTML.
// Rotation limits the damage: a stolen refresh token that is replayed after the legitimate
// client rotated it revokes the whole token family server-side. An httpOnly, SameSite
// cookie for the refresh token would be safer but needs backend support.
// Tokens are never logged and never placed in URLs (the WebSocket uses a one-time ticket).

const ACCESS_KEY = 'aquaguard.session';
const REFRESH_KEY = 'aquaguard.refresh';
const EXPIRY_KEY = 'aquaguard.exp';

let memoryAccess: string | null = null;
let memoryRefresh: string | null = null;
let memoryExpiry: number | null = null;
let hydrated = false;

function read(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}
function write(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    /* storage unavailable: memory only */
  }
}
function remove(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

function hydrate(): void {
  if (hydrated && memoryAccess) return;
  memoryAccess = memoryAccess ?? read(ACCESS_KEY);
  memoryRefresh = memoryRefresh ?? read(REFRESH_KEY);
  if (memoryExpiry === null) {
    const e = Number(read(EXPIRY_KEY));
    memoryExpiry = Number.isFinite(e) && e > 0 ? e : null;
  }
  hydrated = true;
}

export function getToken(): string | null {
  hydrate();
  return memoryAccess;
}

export function getRefreshToken(): string | null {
  hydrate();
  return memoryRefresh;
}

/** Epoch ms at which the access token expires, or null when unknown. */
export function getAccessExpiry(): number | null {
  hydrate();
  return memoryExpiry;
}

/** Stores only an access token (no refresh/expiry knowledge). */
export function setToken(token: string): void {
  memoryAccess = token;
  memoryRefresh = null;
  memoryExpiry = null;
  hydrated = true;
  write(ACCESS_KEY, token);
  remove(REFRESH_KEY);
  remove(EXPIRY_KEY);
}

export interface TokenSet {
  access_token: string;
  refresh_token: string;
  /** Access-token lifetime in seconds. */
  expires_in: number;
}

/** Stores a full pair. ALWAYS call this with the NEW refresh token after login/refresh (rotation). */
export function setTokens(t: TokenSet, now: number = Date.now()): void {
  memoryAccess = t.access_token;
  memoryRefresh = t.refresh_token;
  memoryExpiry = now + Math.max(0, t.expires_in) * 1000;
  hydrated = true;
  write(ACCESS_KEY, t.access_token);
  write(REFRESH_KEY, t.refresh_token);
  write(EXPIRY_KEY, String(memoryExpiry));
}

export function clearToken(): void {
  memoryAccess = null;
  memoryRefresh = null;
  memoryExpiry = null;
  hydrated = false;
  remove(ACCESS_KEY);
  remove(REFRESH_KEY);
  remove(EXPIRY_KEY);
}
