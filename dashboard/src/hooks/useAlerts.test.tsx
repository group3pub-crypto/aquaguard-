import { renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { serializeParams, useAlerts } from './useAlerts';
import { mockFetch, sampleAlert } from '@/test/utils';

describe('useAlerts', () => {
  it('does not refetch in a loop when params is a fresh inline object each render', async () => {
    const { fn } = mockFetch(() => ({ body: [sampleAlert] }));
    const { result, rerender } = renderHook(() => useAlerts({ flat_id: 3, state: 'active' }));
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));
    rerender();
    rerender();
    await new Promise((r) => setTimeout(r, 30));
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('refetches when params actually change', async () => {
    const { fn } = mockFetch(() => ({ body: [] }));
    const { rerender } = renderHook(({ s }) => useAlerts({ state: s }), { initialProps: { s: 'active' } });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(1));
    rerender({ s: 'resolved' });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(2));
  });

  it('serialises params independent of key order and undefined values', () => {
    expect(serializeParams({ state: 'a', flat_id: 1 })).toBe(serializeParams({ flat_id: 1, state: 'a', ...{} }));
    expect(serializeParams({ flat_id: undefined })).toBe(serializeParams({}));
  });

  it('exposes errors', async () => {
    mockFetch(() => ({ status: 500, body: { detail: 'boom' } }));
    const { result } = renderHook(() => useAlerts({}));
    await waitFor(() => expect(result.current.error?.message).toBe('boom'));
  });
});
