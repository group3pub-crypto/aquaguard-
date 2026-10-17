import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, request, setUnauthorizedHandler } from './api';
import { getAccessExpiry, getRefreshToken, getToken, setToken, setTokens } from './session';
import { mockFetch } from '@/test/utils';

afterEach(() => {
  setUnauthorizedHandler(null);
});

describe('api client', () => {
  it('sends the Bearer token from the session', async () => {
    setToken('abc.def');
    const { calls } = mockFetch(() => ({ body: [] }));
    await api.societies();
    expect(calls[0]?.headers.Authorization).toBe('Bearer abc.def');
    expect(calls[0]?.url.pathname).toBe('/api/v1/societies');
  });

  it('does not send a token or trigger the 401 flow for login', async () => {
    setToken('stale');
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const { calls } = mockFetch(() => ({ status: 401, body: { detail: 'Invalid credentials' } }));
    await expect(api.login({ username: 'a', password: 'b' })).rejects.toMatchObject({ status: 401, message: 'Invalid credentials' });
    expect(calls[0]?.headers.Authorization).toBeUndefined();
    expect(handler).not.toHaveBeenCalled();
  });

  it('stores the token after a successful login', async () => {
    mockFetch(() => ({ body: { access_token: 'newtok', refresh_token: 'r'.repeat(24), token_type: 'bearer', role: 'admin', expires_in: 60, refresh_expires_in: 600 } }));
    await api.login({ username: 'a', password: 'b' });
    expect(getToken()).toBe('newtok');
    expect(getRefreshToken()).toBe('r'.repeat(24));
    expect(getAccessExpiry()).toBeGreaterThan(Date.now());
  });

  it('clears the session and calls the handler on 401', async () => {
    setToken('expired');
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    mockFetch(() => ({ status: 401, body: { detail: 'Token expired' } }));
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem('aquaguard.session')).toBeNull();
  });

  it('maps string and array validation details to the error message', async () => {
    mockFetch(() => ({ status: 422, body: { detail: [{ msg: 'field required' }, { msg: 'too short' }] } }));
    await expect(request('/x')).rejects.toMatchObject({ status: 422, message: 'field required; too short' });
    mockFetch(() => ({ status: 403, body: { detail: 'Forbidden' } }));
    await expect(request('/x')).rejects.toMatchObject({ status: 403, message: 'Forbidden' });
  });

  it('serialises query params, skipping undefined values', async () => {
    const { calls } = mockFetch(() => ({ body: [] }));
    await api.alerts({ flat_id: 5, state: undefined });
    expect(calls[0]?.url.search).toBe('?flat_id=5');
  });

  it('times out via AbortController', async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_u: string, init: RequestInit) =>
          new Promise((_res, rej) => {
            init.signal?.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')));
          }),
      ),
    );
    const p = request('/slow', { timeoutMs: 500 });
    const assertion = expect(p).rejects.toMatchObject({ kind: 'timeout', status: 0 });
    await vi.advanceTimersByTimeAsync(501);
    await assertion;
  });

  it('wraps network failures in ApiError', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(request('/x')).rejects.toMatchObject({ kind: 'network', status: 0 });
  });
});

describe('silent refresh', () => {
  const R1 = 'refresh-token-number-0001';
  const R2 = 'refresh-token-number-0002';
  const R3 = 'refresh-token-number-0003';
  const pair = (access: string, refresh: string) => ({
    access_token: access,
    refresh_token: refresh,
    token_type: 'bearer',
    role: 'admin',
    expires_in: 900,
    refresh_expires_in: 604800,
  });

  it('on 401 refreshes once, stores the ROTATED refresh token and retries the original request once', async () => {
    setTokens({ access_token: 'old', refresh_token: R1, expires_in: 900 });
    const { calls } = mockFetch((url, init) => {
      if (url.pathname.endsWith('/auth/refresh')) return { body: pair('new', R2) };
      const auth = (init.headers as Record<string, string>).Authorization;
      return auth === 'Bearer new' ? { body: [] } : { status: 401, body: { detail: 'expired' } };
    });
    await expect(api.societies()).resolves.toEqual([]);
    expect(calls.map((c) => c.url.pathname)).toEqual(['/api/v1/societies', '/api/v1/auth/refresh', '/api/v1/societies']);
    expect(calls[1]?.body).toEqual({ refresh_token: R1 });
    expect(calls[1]?.headers.Authorization).toBeUndefined();
    expect(getToken()).toBe('new');
    expect(getRefreshToken()).toBe(R2);
  });

  it('is single-flight: concurrent 401s share ONE refresh call', async () => {
    setTokens({ access_token: 'old', refresh_token: R1, expires_in: 900 });
    let refreshes = 0;
    const { calls } = mockFetch(async (url, init) => {
      if (url.pathname.endsWith('/auth/refresh')) {
        refreshes += 1;
        await new Promise((r) => setTimeout(r, 20));
        return { body: pair('new', R2) };
      }
      const auth = (init.headers as Record<string, string>).Authorization;
      return auth === 'Bearer new' ? { body: [] } : { status: 401, body: { detail: 'expired' } };
    });
    await Promise.all([api.societies(), api.alerts(), api.flats(), api.anomalies()]);
    expect(refreshes).toBe(1);
    expect(calls.filter((c) => c.url.pathname.endsWith('/auth/refresh'))).toHaveLength(1);
    expect(getRefreshToken()).toBe(R2);
  });

  it('refreshes proactively shortly before the access token expires', async () => {
    setTokens({ access_token: 'soon', refresh_token: R1, expires_in: 10 }); // inside the 30 s skew
    const { calls } = mockFetch((url) =>
      url.pathname.endsWith('/auth/refresh') ? { body: pair('fresh', R2) } : { body: [] },
    );
    await api.societies();
    expect(calls.map((c) => c.url.pathname)).toEqual(['/api/v1/auth/refresh', '/api/v1/societies']);
    expect(calls[1]?.headers.Authorization).toBe('Bearer fresh');
  });

  it('uses each rotated refresh token exactly once across successive refreshes', async () => {
    setTokens({ access_token: 'a', refresh_token: R1, expires_in: 1 });
    const seen: string[] = [];
    mockFetch((url, init) => {
      if (url.pathname.endsWith('/auth/refresh')) {
        const presented = (JSON.parse(String(init.body)) as { refresh_token: string }).refresh_token;
        seen.push(presented);
        return { body: pair(`acc-${seen.length}`, seen.length === 1 ? R2 : R3) };
      }
      return { body: [] };
    });
    await api.societies();
    setTokens({ access_token: 'b', refresh_token: getRefreshToken() ?? '', expires_in: 1 });
    await api.societies();
    expect(seen).toEqual([R1, R2]);
    expect(getRefreshToken()).toBe(R3);
  });

  it('a rejected refresh (expired / reuse detected) is a logout: clears tokens, calls the handler, no retry loop', async () => {
    setTokens({ access_token: 'old', refresh_token: R1, expires_in: 900 });
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const { calls } = mockFetch((url) =>
      url.pathname.endsWith('/auth/refresh') ? { status: 401, body: { detail: 'Invalid refresh token' } } : { status: 401, body: { detail: 'expired' } },
    );
    await expect(api.societies()).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(getToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
    expect(calls.filter((c) => c.url.pathname.endsWith('/auth/refresh'))).toHaveLength(1);
  });

  it('a second 401 after a successful refresh ends the session instead of looping', async () => {
    setTokens({ access_token: 'old', refresh_token: R1, expires_in: 900 });
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const { calls } = mockFetch((url) =>
      url.pathname.endsWith('/auth/refresh') ? { body: pair('new', R2) } : { status: 401, body: { detail: 'nope' } },
    );
    await expect(api.societies()).rejects.toMatchObject({ status: 401 });
    expect(calls).toHaveLength(3); // original, refresh, ONE retry
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('a transient refresh failure (5xx) does not log the user out', async () => {
    setTokens({ access_token: 'old', refresh_token: R1, expires_in: 900 });
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    mockFetch((url) =>
      url.pathname.endsWith('/auth/refresh') ? { status: 503, body: { detail: 'down' } } : { status: 401, body: { detail: 'expired' } },
    );
    await expect(api.societies()).rejects.toMatchObject({ status: 401 });
    expect(handler).not.toHaveBeenCalled();
    expect(getRefreshToken()).toBe(R1);
  });

  it('logout posts the refresh token, forgets the session locally and never throws', async () => {
    setTokens({ access_token: 'a', refresh_token: R1, expires_in: 900 });
    const { calls } = mockFetch(() => ({ status: 500, body: {} }));
    await expect(api.logout()).resolves.toBeUndefined();
    expect(calls[0]?.url.pathname).toBe('/api/v1/auth/logout');
    expect(calls[0]?.body).toEqual({ refresh_token: R1 });
    expect(getToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });

  it('change-password stores the fresh token pair it returns', async () => {
    setTokens({ access_token: 'a', refresh_token: R1, expires_in: 900 });
    mockFetch(() => ({ body: pair('after-change', R2) }));
    await api.changePassword({ current_password: 'old-pass1', new_password: 'new-pass2' });
    expect(getToken()).toBe('after-change');
    expect(getRefreshToken()).toBe(R2);
  });

  it('never writes tokens to the console', async () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    setTokens({ access_token: 'secret-access', refresh_token: R1, expires_in: 900 });
    mockFetch((url) => (url.pathname.endsWith('/auth/refresh') ? { body: pair('new-secret', R2) } : { status: 401, body: {} }));
    await api.societies().catch(() => undefined);
    for (const s of spies) expect(s).not.toHaveBeenCalled();
    spies.forEach((s) => s.mockRestore());
  });
});
