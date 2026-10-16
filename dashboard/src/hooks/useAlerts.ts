'use client';
import { useCallback, useEffect, useRef } from 'react';
import { api } from '@/lib/api';
import { useLive } from '@/context/LiveProvider';
import { isOpenAlert } from '@/lib/format';
import type { Alert, AlertFilter } from '@/lib/types';

export interface UseAlertsParams extends AlertFilter {
  /**
   * Only open alerts (backend OPEN_STATES). `GET /alerts` has no such filter and rejects
   * unknown `state` values with 422, so this is applied client-side.
   */
  open?: boolean;
}
import { usePolling } from './usePolling';

/** Order-independent, undefined-free serialisation so inline param objects are stable. */
export function serializeParams(params: UseAlertsParams): string {
  return JSON.stringify(
    Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => [k, String(v)] as const)
      .sort(([a], [b]) => a.localeCompare(b)),
  );
}

function matches(alert: Alert, params: UseAlertsParams): boolean {
  if (params.open && !isOpenAlert(alert)) return false;
  if (params.flat_id !== undefined && String(alert.flat_id) !== String(params.flat_id)) return false;
  if (params.state && alert.state !== params.state) return false;
  return true;
}

export function useAlerts(params: UseAlertsParams = {}, intervalMs = 30_000) {
  const key = serializeParams(params);
  const { lastAlert } = useLive();
  const result = usePolling<Alert[]>(
    async (signal) => {
      const { open, ...query } = params;
      const list = await api.alerts({ limit: 500, ...query }, { signal });
      return Array.isArray(list) ? (open ? list.filter(isOpenAlert) : list) : [];
    },
    {
      key: `alerts:${key}`,
      intervalMs,
    },
  );
  const { setData } = result;

  const paramsRef = useRef(params);
  paramsRef.current = params;

  /** Merge an alert into state: replace if known, drop if it no longer matches the filter. */
  const upsert = useCallback(
    (alert: Alert) => {
      const keep = matches(alert, paramsRef.current);
      setData((prev) => {
        if (!prev) return prev;
        const idx = prev.findIndex((a) => String(a.id) === String(alert.id));
        if (idx >= 0) return keep ? prev.map((a, i) => (i === idx ? alert : a)) : prev.filter((_, i) => i !== idx);
        return keep ? [alert, ...prev] : prev;
      });
    },
    [setData],
  );

  useEffect(() => {
    if (lastAlert) upsert(lastAlert);
  }, [lastAlert, upsert]);

  return { ...result, alerts: result.data, upsert };
}
