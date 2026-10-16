import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, request, setUnauthorizedHandler } from './api';
import { getToken, setToken } from './session';
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
    mockFetch(() => ({ body: { access_token: 'newtok', token_type: 'bearer', role: 'admin', expires_in: 60 } }));
    await api.login({ username: 'a', password: 'b' });
    expect(getToken()).toBe('newtok');
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
