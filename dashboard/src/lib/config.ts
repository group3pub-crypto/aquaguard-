// Runtime configuration for the browser.
//
// NEXT_PUBLIC_* values are inlined at build time, which would tie one Docker image to one
// domain. The root layout therefore reads the *server* environment at request time
// (see lib/serverConfig.ts) and injects it as `window.__AQUAGUARD_CONFIG__`. The inlined
// NEXT_PUBLIC_* values below are only the fallback (dev, tests, static contexts).

export interface RuntimeConfig {
  apiUrl: string;
  wsUrl: string;
  /** IANA timezone the backend uses for baseline hour-of-week slots (settings.timezone). */
  timezone: string;
}

declare global {
  interface Window {
    __AQUAGUARD_CONFIG__?: Partial<RuntimeConfig>;
  }
}

export const DEFAULT_API_URL = 'http://localhost:8000';
export const DEFAULT_TIMEZONE = 'UTC';

const trim = (u: string) => u.replace(/\/+$/, '');

export function deriveWsUrl(apiUrl: string): string {
  return apiUrl.replace(/^http/, 'ws');
}

export function getConfig(): RuntimeConfig {
  const injected = typeof window !== 'undefined' ? window.__AQUAGUARD_CONFIG__ : undefined;
  const apiUrl = trim(injected?.apiUrl || process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL);
  const wsUrl = trim(injected?.wsUrl || process.env.NEXT_PUBLIC_WS_URL || deriveWsUrl(apiUrl));
  const timezone = injected?.timezone || process.env.NEXT_PUBLIC_BACKEND_TZ || DEFAULT_TIMEZONE;
  return { apiUrl, wsUrl, timezone };
}
