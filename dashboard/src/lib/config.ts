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
  /** Mirrors the backend ALLOW_SELF_REGISTRATION flag (default off); controls the /register link. */
  allowSelfRegistration: boolean;
  /** Mirrors the backend SHUTOFF_WARNING_SECONDS; only used to render the warning countdown. */
  shutoffWarningSeconds: number;
}

declare global {
  interface Window {
    __AQUAGUARD_CONFIG__?: Partial<RuntimeConfig>;
  }
}

export const DEFAULT_API_URL = 'http://localhost:8000';
export const DEFAULT_TIMEZONE = 'UTC';
export const DEFAULT_SHUTOFF_WARNING_SECONDS = 300;

export function parseBool(v: string | undefined): boolean {
  return v !== undefined && /^(1|true|yes|on)$/i.test(v.trim());
}
export function parsePositiveNumber(v: string | undefined, fallback: number): number {
  const n = Number(v);
  return v !== undefined && Number.isFinite(n) && n > 0 ? n : fallback;
}

const trim = (u: string) => u.replace(/\/+$/, '');

export function deriveWsUrl(apiUrl: string): string {
  return apiUrl.replace(/^http/, 'ws');
}

export function getConfig(): RuntimeConfig {
  const injected = typeof window !== 'undefined' ? window.__AQUAGUARD_CONFIG__ : undefined;
  const apiUrl = trim(injected?.apiUrl || process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL);
  const wsUrl = trim(injected?.wsUrl || process.env.NEXT_PUBLIC_WS_URL || deriveWsUrl(apiUrl));
  const timezone = injected?.timezone || process.env.NEXT_PUBLIC_BACKEND_TZ || DEFAULT_TIMEZONE;
  const allowSelfRegistration =
    typeof injected?.allowSelfRegistration === 'boolean'
      ? injected.allowSelfRegistration
      : parseBool(process.env.NEXT_PUBLIC_ALLOW_SELF_REGISTRATION);
  const shutoffWarningSeconds =
    typeof injected?.shutoffWarningSeconds === 'number' && injected.shutoffWarningSeconds > 0
      ? injected.shutoffWarningSeconds
      : parsePositiveNumber(process.env.NEXT_PUBLIC_SHUTOFF_WARNING_SECONDS, DEFAULT_SHUTOFF_WARNING_SECONDS);
  return { apiUrl, wsUrl, timezone, allowSelfRegistration, shutoffWarningSeconds };
}
