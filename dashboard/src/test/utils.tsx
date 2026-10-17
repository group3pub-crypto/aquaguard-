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

const BOUND: Record<Role, Partial<User>> = {
  admin: {},
  maintenance: { society_id: '0b1c3a52-6d1f-4f5e-9a39-0d1b5a2f7c11' },
  resident: { flat_id: '3f8e4a6c-2b6d-4c2e-8f55-7a1d9e0b4c22' },
};

/** Users are bound to their scope by default; pass `extra` (e.g. flat_id: null) to make one unbound. */
export function makeUser(role: Role, username = 'alice', extra: Partial<User> = {}): User {
  return { id: '11111111-1111-4111-8111-111111111111', username, name: 'Alice Example', role, ...BOUND[role], ...extra };
}

export function authValue(role: Role | null, username = 'alice', extra: Partial<User> = {}): AuthContextValue {
  const user = role ? makeUser(role, username, extra) : null;
  return {
    user,
    token: role ? 'tok' : null,
    status: role ? 'authenticated' : 'unauthenticated',
    can: permissionsFor(role),
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    expireSession: vi.fn(),
  };
}

export function WithAuth({ role, children, username, user }: { role: Role | null; children: ReactNode; username?: string; user?: Partial<User> }) {
  return <AuthContext.Provider value={authValue(role, username, user)}>{children}</AuthContext.Provider>;
}

export { alert as sampleAlert } from './fixtures';
