import { renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { serializeParams, useAlerts } from './useAlerts';
import { mockFetch, sampleAlert } from '@/test/utils';

describe('useAlerts', () => {
  it('does not refetch in a loop when params is a fresh inline object each render', async () => {
    const { fn } = mockFetch(() => ({ body: [sampleAlert] }));
    const { result, rerender } = renderHook(() => useAlerts({ flat_id: '3f8e4a6c-2b6d-4c2e-8f55-7a1d9e0b4c22', state: 'alert' }));
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));
    rerender();
    rerender();
    await new Promise((r) => setTimeout(r, 30));
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('refetches when params actually change', async () => {
    const { fn } = mockFetch(() => ({ body: [] }));
    const { rerender } = renderHook(({ s }) => useAlerts({ state: s }), { initialProps: { s: 'alert' as 'alert' | 'resolved' } });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(1));
    rerender({ s: 'resolved' });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(2));
  });

  it('serialises params independent of key order and undefined values', () => {
    expect(serializeParams({ state: 'alert', flat_id: 1 })).toBe(serializeParams({ flat_id: 1, state: 'alert' }));
    expect(serializeParams({ flat_id: undefined })).toBe(serializeParams({}));
  });

  it('open mode requests WITHOUT a state filter (backend 422s unknown states) and filters client-side', async () => {
    const { calls } = mockFetch(() => ({
      body: [sampleAlert, { ...sampleAlert, id: 'r1', state: 'resolved' }, { ...sampleAlert, id: 'o1', state: 'overridden' }, { ...sampleAlert, id: 'a1', state: 'auto_shutoff' }],
    }));
    const { result } = renderHook(() => useAlerts({ open: true }));
    await waitFor(() => expect(result.current.alerts).toHaveLength(2));
    expect(calls[0]?.url.searchParams.has('state')).toBe(false);
    expect(calls[0]?.url.searchParams.has('open')).toBe(false);
    expect(result.current.alerts?.map((a) => a.state)).toEqual(['alert', 'auto_shutoff']);
  });

  it('exposes errors', async () => {
    mockFetch(() => ({ status: 500, body: { detail: 'boom' } }));
    const { result } = renderHook(() => useAlerts({}));
    await waitFor(() => expect(result.current.error?.message).toBe('boom'));
  });
});
