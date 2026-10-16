import { vi } from 'vitest';
import type { ReactNode } from 'react';
import { AuthContext, type AuthContextValue } from '@/context/AuthProvider';
import { permissionsFor } from '@/lib/permissions';
import type { Role, User } from '@/lib/types';

export interface MockReply {
  status?: number;
  body?: unknown;
}
export type Handler = (url: URL, init: RequestInit) => MockReply | Promise<MockReply> | undefined;

export interface FetchCall {
  url: URL;
  init: RequestInit;
  headers: Record<string, string>;
  body: unknown;
}

/** Installs a mocked global fetch. Unhandled routes return 404. */
export function mockFetch(handler: Handler) {
  const calls: FetchCall[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const headers = (init.headers ?? {}) as Record<string, string>;
    calls.push({
      url,
      init,
      headers,
      body: typeof init.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined,
    });
    const reply = (await handler(url, init)) ?? { status: 404, body: { detail: 'not found' } };
    return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), {
      status: reply.status ?? 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}

export function makeUser(role: Role, username = 'alice'): User {
  return { id: 1, username, name: 'Alice Example', role };
}

export function authValue(role: Role | null, username = 'alice'): AuthContextValue {
  const user = role ? makeUser(role, username) : null;
  return {
    user,
    token: role ? 'tok' : null,
    status: role ? 'authenticated' : 'unauthenticated',
    can: permissionsFor(role),
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  };
}

export function WithAuth({ role, children, username }: { role: Role | null; children: ReactNode; username?: string }) {
  return <AuthContext.Provider value={authValue(role, username)}>{children}</AuthContext.Provider>;
}

export const sampleAlert = {
  id: 7,
  flat_id: 3,
  anomaly_id: 1,
  state: 'active',
  tier: 1,
  severity_score: 0.82,
  message: 'Possible burst pipe',
  message_source: 'template',
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
};
