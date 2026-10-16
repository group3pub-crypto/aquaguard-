import { describe, expect, it } from 'vitest';
import { NextRequest } from 'next/server';
import { buildCsp, readRuntimeConfig } from './serverConfig';
import { middleware } from '@/middleware';

describe('runtime config + CSP', () => {
  it('derives urls from env at call time, ws from api, timezone default UTC', () => {
    expect(readRuntimeConfig({ AQUAGUARD_API_URL: 'https://api.example.com/' })).toEqual({
      apiUrl: 'https://api.example.com',
      wsUrl: 'wss://api.example.com',
      timezone: 'UTC',
    });
    expect(readRuntimeConfig({ NEXT_PUBLIC_API_URL: 'http://a:1', NEXT_PUBLIC_WS_URL: 'ws://b:2', NEXT_PUBLIC_BACKEND_TZ: 'Asia/Kolkata' })).toMatchObject({
      apiUrl: 'http://a:1',
      wsUrl: 'ws://b:2',
      timezone: 'Asia/Kolkata',
    });
  });

  it('script-src uses the nonce and never unsafe-inline; connect-src follows config', () => {
    const csp = buildCsp('abc', { apiUrl: 'https://api.x.io', wsUrl: 'wss://api.x.io', timezone: 'UTC' }, false);
    const script = csp.split('; ').find((d) => d.startsWith('script-src')) ?? '';
    expect(script).toContain("'nonce-abc'");
    expect(script).not.toContain('unsafe-inline');
    expect(script).not.toContain('unsafe-eval');
    expect(csp).toContain("connect-src 'self' https://api.x.io wss://api.x.io");
  });

  it('middleware issues a fresh nonce per request and passes it to the app', () => {
    process.env.AQUAGUARD_API_URL = 'https://runtime.example.org';
    const r1 = middleware(new NextRequest('http://localhost/'));
    const r2 = middleware(new NextRequest('http://localhost/'));
    const c1 = r1.headers.get('content-security-policy') ?? '';
    const c2 = r2.headers.get('content-security-policy') ?? '';
    expect(c1).toContain('https://runtime.example.org');
    expect(c1).not.toBe(c2);
    expect(r1.headers.get('x-middleware-request-x-nonce')).toBeTruthy();
    delete process.env.AQUAGUARD_API_URL;
  });
});
