import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { buildCsp, readRuntimeConfig } from '@/lib/serverConfig';

/**
 * Per-request CSP with a script nonce (Next.js documented pattern). The nonce is passed to
 * the app via the request header `x-nonce`; Next reads the CSP request header and applies
 * the nonce to its own scripts. connect-src is derived from the *runtime* environment on
 * every request (see lib/serverConfig.ts), so one image works behind any domain.
 */
export function middleware(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());
  const csp = buildCsp(nonce, readRuntimeConfig(), process.env.NODE_ENV !== 'production');

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set('x-nonce', nonce);
  requestHeaders.set('Content-Security-Policy', csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set('Content-Security-Policy', csp);
  return response;
}

export const config = {
  matcher: [
    {
      source: '/((?!_next/static|_next/image|favicon.ico).*)',
      missing: [
        { type: 'header', key: 'next-router-prefetch' },
        { type: 'header', key: 'purpose', value: 'prefetch' },
      ],
    },
  ],
};
