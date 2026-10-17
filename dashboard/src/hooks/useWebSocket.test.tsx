import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backoffDelay, useWebSocket } from './useWebSocket';

class MockWS {
  static instances: MockWS[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((e?: { code: number }) => void) | null = null;
  closed = false;
  constructor(public url: string) {
    MockWS.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  fail(code?: number) {
    this.onclose?.(code === undefined ? undefined : { code });
  }
}

/** Flush the microtask the hook's async connect() awaits before touching a WebSocket. */
const flush = () => act(() => Promise.resolve());

const fixedUrl = () => Promise.resolve('ws://x');

beforeEach(() => {
  MockWS.instances = [];
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', MockWS);
  vi.spyOn(Math, 'random').mockReturnValue(1);
});
afterEach(() => vi.restoreAllMocks());

describe('backoffDelay', () => {
  it('doubles from 1s up to a 30s cap', () => {
    expect([0, 1, 2, 3, 4, 5, 6].map((n) => backoffDelay(n, 1))).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
  });
  it('applies downward jitter', () => {
    expect(backoffDelay(0, 0)).toBe(750);
  });
});

describe('useWebSocket', () => {
  it('on close code 4401 without a refresher does NOT retry with the stale ticket and signals unauthorized', async () => {
    const onUnauthorized = vi.fn();
    const { result } = renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage: vi.fn(), onUnauthorized }));
    await flush();
    act(() => MockWS.instances[0]?.fail(4401));
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(result.current.state).toBe('disconnected');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(MockWS.instances).toHaveLength(1);
  });

  it('still retries on ordinary closes (1006)', async () => {
    const onUnauthorized = vi.fn();
    renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage: vi.fn(), onUnauthorized }));
    await flush();
    act(() => MockWS.instances[0]?.fail(1006));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(MockWS.instances).toHaveLength(2);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('reconnects on the backoff schedule then reports disconnected', async () => {
    const { result } = renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage: vi.fn(), maxRetries: 3 }));
    await flush();
    expect(MockWS.instances).toHaveLength(1);
    const expected = [1000, 2000, 4000];
    for (let i = 0; i < expected.length; i++) {
      const delay = expected[i]!;
      act(() => MockWS.instances[i]?.fail());
      expect(result.current.state).toBe('reconnecting');
      await act(async () => {
        await vi.advanceTimersByTimeAsync(delay - 1);
      });
      expect(MockWS.instances).toHaveLength(i + 1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(MockWS.instances).toHaveLength(i + 2);
    }
    act(() => MockWS.instances[3]?.fail());
    expect(result.current.state).toBe('disconnected');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(MockWS.instances).toHaveLength(4);
  });

  it('resets the backoff after a successful open', async () => {
    renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage: vi.fn() }));
    await flush();
    act(() => MockWS.instances[0]?.fail());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    act(() => MockWS.instances[1]?.onopen?.());
    act(() => MockWS.instances[1]?.fail());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(MockWS.instances).toHaveLength(3);
  });

  it('cleans up the socket and pending timers on unmount', async () => {
    const { unmount } = renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage: vi.fn() }));
    await flush();
    act(() => MockWS.instances[0]?.fail());
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(MockWS.instances).toHaveLength(1);

    const second = renderHook(() => useWebSocket({ getUrl: () => Promise.resolve('ws://y'), onMessage: vi.fn() }));
    await flush();
    const sock = MockWS.instances[1];
    second.unmount();
    expect(sock?.closed).toBe(true);
  });

  it('does not reconnect when only the handler identity changes', async () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ h }) => useWebSocket({ getUrl: fixedUrl, onMessage: h }), { initialProps: { h: first } });
    await flush();
    rerender({ h: second });
    rerender({ h: vi.fn() });
    expect(MockWS.instances).toHaveLength(1);
    act(() => MockWS.instances[0]?.onmessage?.({ data: 'hello' } as MessageEvent));
    expect(second).not.toHaveBeenCalled();
  });

  it('delivers messages to the latest handler', async () => {
    const first = vi.fn();
    const latest = vi.fn();
    const { rerender } = renderHook(({ h }) => useWebSocket({ getUrl: fixedUrl, onMessage: h }), { initialProps: { h: first } });
    await flush();
    rerender({ h: latest });
    act(() => MockWS.instances[0]?.onmessage?.({ data: 'hello' } as MessageEvent));
    expect(first).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledWith('hello');
  });

  it('stays idle when not enabled (no auth yet)', async () => {
    const { result } = renderHook(() => useWebSocket({ getUrl: null, onMessage: vi.fn() }));
    await flush();
    expect(result.current.state).toBe('idle');
    expect(MockWS.instances).toHaveLength(0);
  });

  it('fetches a FRESH ticket for every connect and reconnect', async () => {
    let n = 0;
    const getUrl = vi.fn(() => Promise.resolve(`ws://x?ticket=t${++n}`));
    renderHook(() => useWebSocket({ getUrl, onMessage: vi.fn() }));
    await flush();
    act(() => MockWS.instances[0]?.fail(1006));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(getUrl).toHaveBeenCalledTimes(2);
    expect(MockWS.instances.map((w) => w.url)).toEqual(['ws://x?ticket=t1', 'ws://x?ticket=t2']);
  });

  it('retries with backoff when the ticket request fails', async () => {
    const getUrl = vi.fn().mockRejectedValueOnce(new Error('down')).mockResolvedValue('ws://x');
    const { result } = renderHook(() => useWebSocket({ getUrl, onMessage: vi.fn() }));
    await flush();
    expect(result.current.state).toBe('reconnecting');
    expect(MockWS.instances).toHaveLength(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(MockWS.instances).toHaveLength(1);
  });

  it('4401: refreshes the token and reconnects ONCE; a second 4401 logs out', async () => {
    const onAuthExpired = vi.fn().mockResolvedValue(true);
    const onUnauthorized = vi.fn();
    renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage: vi.fn(), onAuthExpired, onUnauthorized }));
    await flush();
    act(() => MockWS.instances[0]?.fail(4401));
    await flush();
    await flush();
    expect(onAuthExpired).toHaveBeenCalledTimes(1);
    expect(MockWS.instances).toHaveLength(2);
    expect(onUnauthorized).not.toHaveBeenCalled();
    act(() => MockWS.instances[1]?.fail(4401));
    await flush();
    expect(onAuthExpired).toHaveBeenCalledTimes(1);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(MockWS.instances).toHaveLength(2);
  });

  it('4401: failed refresh means logout, no reconnect', async () => {
    const onAuthExpired = vi.fn().mockResolvedValue(false);
    const onUnauthorized = vi.fn();
    const { result } = renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage: vi.fn(), onAuthExpired, onUnauthorized }));
    await flush();
    act(() => MockWS.instances[0]?.fail(4401));
    await flush();
    await flush();
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(result.current.state).toBe('disconnected');
    expect(MockWS.instances).toHaveLength(1);
  });

  it('4403: shows a reason and never retries', async () => {
    const { result } = renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage: vi.fn() }));
    await flush();
    act(() => MockWS.instances[0]?.fail(4403));
    expect(result.current.state).toBe('disconnected');
    expect(result.current.notice).toMatchObject({ code: 4403 });
    expect(result.current.notice?.message).toMatch(/administrator/i);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });
    expect(MockWS.instances).toHaveLength(1);
  });

  it.each([4429, 4408])('%i: shows a reason and retries politely with backoff', async (code) => {
    const { result } = renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage: vi.fn() }));
    await flush();
    act(() => MockWS.instances[0]?.fail(code));
    expect(result.current.state).toBe('reconnecting');
    expect(result.current.notice?.code).toBe(code);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000); // polite: > the 1s first-attempt delay
    });
    expect(MockWS.instances).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(MockWS.instances).toHaveLength(2);
  });

  it('answers a server {"type":"ping"} with a text frame and does not forward it', async () => {
    const onMessage = vi.fn();
    renderHook(() => useWebSocket({ getUrl: fixedUrl, onMessage }));
    await flush();
    const ws = MockWS.instances[0]!;
    const send = vi.fn();
    (ws as unknown as { send: typeof send }).send = send;
    act(() => ws.onmessage?.({ data: '{"type":"ping"}' } as MessageEvent));
    expect(send).toHaveBeenCalledWith('ping');
    expect(onMessage).not.toHaveBeenCalled();
  });
});
