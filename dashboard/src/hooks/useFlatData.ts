'use client';
import { useEffect, useMemo } from 'react';
import { api } from '@/lib/api';
import { mergeReadings } from '@/lib/chart';
import { toValveState } from '@/lib/guards';
import { useLive } from '@/context/LiveProvider';
import type { Reading } from '@/lib/types';
import { usePolling } from './usePolling';

/** All data for one flat; each resource has independent loading/error state. */
export function useFlatData(flatId: string) {
  const { readings: liveReadings, valves, valveCommands, lastAnomaly } = useLive();
  const flat = usePolling((signal) => api.flat(flatId, { signal }), {
    key: `flat:${flatId}`,
    intervalMs: 30_000,
  });
  const history = usePolling(
    (signal) => api.readings(flatId, { limit: 288 }, { signal }),
    { key: `readings:${flatId}`, intervalMs: 60_000 },
  );
  const baseline = usePolling((signal) => api.baseline(flatId, { signal }), {
    key: `baseline:${flatId}`,
  });
  const forecast = usePolling((signal) => api.forecast(flatId, { signal }), {
    key: `forecast:${flatId}`,
    intervalMs: 300_000,
  });
  const anomalies = usePolling((signal) => api.anomalies({ flat_id: flatId }, { signal }), {
    key: `anomalies:${flatId}`,
    intervalMs: 30_000,
  });

  const refetchAnomalies = anomalies.refetch;
  useEffect(() => {
    if (lastAnomaly && String(lastAnomaly.flat_id) === flatId) refetchAnomalies();
  }, [lastAnomaly, flatId, refetchAnomalies]);

  const live = liveReadings[flatId];
  const readings: Reading[] = useMemo(() => {
    const liveSeries: Reading[] = (live ?? []).map((r) => ({
      timestamp: r.timestamp,
      flow_rate: r.flow_rate,
      volume: 0,
    }));
    return mergeReadings(history.data ?? [], liveSeries);
  }, [history.data, live]);

  const valveState = valves[flatId] ?? toValveState(flat.data?.valve_state);
  // Prefer the freshest live reading; fall back to the detail endpoint's current_flow (nullable).
  const currentFlow = live && live.length ? (live[live.length - 1]?.flow_rate ?? null) : (flat.data?.current_flow ?? null);
  const pendingCommand = valveCommands[flatId]?.status === 'pending' ? valveCommands[flatId] : undefined;

  return { flat, history, baseline, forecast, anomalies, readings, valveState, currentFlow, pendingCommand };
}
