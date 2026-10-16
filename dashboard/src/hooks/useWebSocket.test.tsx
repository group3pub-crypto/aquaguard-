import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backoffDelay, useWebSocket } from './useWebSocket';

class MockWS {
  static instances: MockWS[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    MockWS.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  fail() {
    this.onclose?.();
  }
}

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
  it('reconnects on the backoff schedule then reports disconnected', () => {
    const { result } = renderHook(() => useWebSocket({ url: 'ws://x', onMessage: vi.fn(), maxRetries: 3 }));
    expect(MockWS.instances).toHaveLength(1);
    const expected = [1000, 2000, 4000];
    expected.forEach((delay, i) => {
      act(() => MockWS.instances[i]?.fail());
      expect(result.current.state).toBe('reconnecting');
      act(() => {
        vi.advanceTimersByTime(delay - 1);
      });
      expect(MockWS.instances).toHaveLength(i + 1);
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(MockWS.instances).toHaveLength(i + 2);
    });
    act(() => MockWS.instances[3]?.fail());
    expect(result.current.state).toBe('disconnected');
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(MockWS.instances).toHaveLength(4);
  });

  it('resets the backoff after a successful open', () => {
    renderHook(() => useWebSocket({ url: 'ws://x', onMessage: vi.fn() }));
    act(() => MockWS.instances[0]?.fail());
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    act(() => MockWS.instances[1]?.onopen?.());
    act(() => MockWS.instances[1]?.fail());
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(MockWS.instances).toHaveLength(3);
  });

  it('cleans up the socket and pending timers on unmount', () => {
    const { unmount } = renderHook(() => useWebSocket({ url: 'ws://x', onMessage: vi.fn() }));
    act(() => MockWS.instances[0]?.fail());
    unmount();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(MockWS.instances).toHaveLength(1);

    const second = renderHook(() => useWebSocket({ url: 'ws://y', onMessage: vi.fn() }));
    const sock = MockWS.instances[1];
    second.unmount();
    expect(sock?.closed).toBe(true);
  });

  it('does not reconnect when only the handler identity changes', () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ h }) => useWebSocket({ url: 'ws://x', onMessage: h }), { initialProps: { h: first } });
    rerender({ h: second });
    rerender({ h: vi.fn() });
    expect(MockWS.instances).toHaveLength(1);
    act(() => MockWS.instances[0]?.onmessage?.({ data: 'hello' } as MessageEvent));
    expect(second).not.toHaveBeenCalled();
  });

  it('delivers messages to the latest handler', () => {
    const first = vi.fn();
    const latest = vi.fn();
    const { rerender } = renderHook(({ h }) => useWebSocket({ url: 'ws://x', onMessage: h }), { initialProps: { h: first } });
    rerender({ h: latest });
    act(() => MockWS.instances[0]?.onmessage?.({ data: 'hello' } as MessageEvent));
    expect(first).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledWith('hello');
  });

  it('stays idle without a url (no token)', () => {
    const { result } = renderHook(() => useWebSocket({ url: null, onMessage: vi.fn() }));
    expect(result.current.state).toBe('idle');
    expect(MockWS.instances).toHaveLength(0);
  });
});
