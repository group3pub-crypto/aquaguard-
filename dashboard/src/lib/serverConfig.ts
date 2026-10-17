// Server-side (middleware / root layout) runtime configuration.
//
// Env vars are read with a *dynamic* key on purpose: Next.js statically inlines
// `process.env.NEXT_PUBLIC_*` member expressions at build time, which would bake the
// build machine's URLs into the image. Reading through a variable key keeps the lookup
// at runtime, so one image works behind any domain. Runtime names (no NEXT_PUBLIC_
// prefix) win; the NEXT_PUBLIC_* names are accepted as a fallback for convenience.

import {
  DEFAULT_API_URL,
  DEFAULT_SHUTOFF_WARNING_SECONDS,
  DEFAULT_TIMEZONE,
  deriveWsUrl,
  parseBool,
  parsePositiveNumber,
  type RuntimeConfig,
} from './config';

type Env = Record<string, string | undefined>;

const trim = (u: string) => u.replace(/\/+$/, '');

export function readRuntimeConfig(env: Env = process.env as Env): RuntimeConfig {
  const get = (name: string): string | undefined => {
    const v = env[name];
    return v && v.trim() ? v.trim() : undefined;
  };
  const apiUrl = trim(get('AQUAGUARD_API_URL') ?? get('NEXT_PUBLIC_API_URL') ?? DEFAULT_API_URL);
  const wsUrl = trim(
    get('AQUAGUARD_WS_URL') ?? get('NEXT_PUBLIC_WS_URL') ?? deriveWsUrl(apiUrl),
  );
  const timezone = get('AQUAGUARD_BACKEND_TZ') ?? get('NEXT_PUBLIC_BACKEND_TZ') ?? DEFAULT_TIMEZONE;
  const allowSelfRegistration = parseBool(
    get('DASHBOARD_ALLOW_SELF_REGISTRATION') ?? get('ALLOW_SELF_REGISTRATION') ?? get('NEXT_PUBLIC_ALLOW_SELF_REGISTRATION'),
  );
  const shutoffWarningSeconds = parsePositiveNumber(
    get('SHUTOFF_WARNING_SECONDS') ?? get('NEXT_PUBLIC_SHUTOFF_WARNING_SECONDS'),
    DEFAULT_SHUTOFF_WARNING_SECONDS,
  );
  return { apiUrl, wsUrl, timezone, allowSelfRegistration, shutoffWarningSeconds };
}

/** Builds the CSP for one request. `nonce` authorises Next's own inline scripts. */
export function buildCsp(nonce: string, cfg: RuntimeConfig, dev: boolean): string {
  return [
    "default-src 'self'",
    // 'strict-dynamic' lets nonce'd Next bootstrap scripts load their chunks; no unsafe-inline.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ''}`,
    // style-src keeps 'unsafe-inline': React `style=` attributes (Recharts sets many) and
    // Next's injected <style> tags cannot carry a nonce. Scripts are what matter for XSS.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src 'self' ${cfg.apiUrl} ${cfg.wsUrl}${dev ? ' ws://localhost:*' : ''}`,
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
  ].join('; ');
}
