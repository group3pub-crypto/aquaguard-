// Token storage tradeoff: the JWT is held in module memory and mirrored to
// sessionStorage so a page reload keeps the session. We deliberately avoid
// localStorage: it persists across tabs/restarts and widens the window for
// token theft via XSS. sessionStorage is still script-readable (an XSS bug could
// exfiltrate it), so we also rely on a strict CSP and never render raw HTML.
// An httpOnly cookie would be safer but requires backend support.

const KEY = 'aquaguard.session';
let memoryToken: string | null = null;

export function getToken(): string | null {
  if (memoryToken) return memoryToken;
  try {
    memoryToken = window.sessionStorage.getItem(KEY);
  } catch {
    memoryToken = null;
  }
  return memoryToken;
}

export function setToken(token: string): void {
  memoryToken = token;
  try {
    window.sessionStorage.setItem(KEY, token);
  } catch {
    /* storage unavailable: memory only */
  }
}

export function clearToken(): void {
  memoryToken = null;
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}
