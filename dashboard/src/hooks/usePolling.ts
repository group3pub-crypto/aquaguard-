'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/lib/api';

export interface PollingOptions {
  /** Stable string identifying the request; changing it refetches and resets data. */
  key: string;
  intervalMs?: number;
  enabled?: boolean;
}

export interface PollingResult<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  refetch: () => void;
  setData: (updater: (prev: T | null) => T | null) => void;
}

/**
 * Runs `fetcher` immediately and (optionally) on an interval. The fetcher is kept in a
 * ref so passing an inline function never causes a refetch loop; only `key`,
 * `intervalMs` and `enabled` control re-runs.
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  { key, intervalMs, enabled = true }: PollingOptions,
): PollingResult<T> {
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const [data, setDataState] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [nonce, setNonce] = useState(0);
  const lastKey = useRef(key);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const keyChanged = lastKey.current !== key;
    lastKey.current = key;
    if (keyChanged) setDataState(null);
    setLoading(true);
    setError(null);

    let cancelled = false;
    let controller = new AbortController();
    const run = async (initial: boolean) => {
      controller.abort();
      controller = new AbortController();
      const mine = controller;
      try {
        const result = await fetcherRef.current(mine.signal);
        if (cancelled || mine.signal.aborted) return;
        setDataState(result);
        setError(null);
      } catch (e) {
        if (cancelled || mine.signal.aborted) return;
        if (e instanceof ApiError && e.kind === 'aborted') return;
        setError(e instanceof Error ? e : new Error('Unknown error'));
      } finally {
        if (!cancelled && initial) setLoading(false);
      }
    };
    void run(true);
    const timer = intervalMs ? setInterval(() => void run(false), intervalMs) : null;
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearInterval(timer);
    };
  }, [key, intervalMs, enabled, nonce]);

  const refetch = useCallback(() => setNonce((n) => n + 1), []);
  const setData = useCallback(
    (updater: (prev: T | null) => T | null) => setDataState((prev) => updater(prev)),
    [],
  );
  return { data, error, loading, refetch, setData };
}
