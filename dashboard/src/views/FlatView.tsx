'use client';
import Link from 'next/link';
import { fmtNumber, fmtTime, humanize } from '@/lib/format';
import { useLive } from '@/context/LiveProvider';
import { useAlerts } from '@/hooks/useAlerts';
import { useFlatData } from '@/hooks/useFlatData';
import { AlertTimeline } from '@/components/AlertTimeline';
import { AnomalyList } from '@/components/AnomalyList';
import { FlowChart } from '@/components/FlowChart';
import { DataState } from '@/components/States';
import { ValveControls } from '@/components/ValveControls';
import { StatusBadge } from '@/components/StatusBadge';

export function FlatView({ flatId }: { flatId: string }) {
  const d = useFlatData(flatId);
  const alerts = useAlerts({ flat_id: flatId });
  const { sensors } = useLive();

  return (
    <div className="space-y-8">
      <div>
        <Link href="/" className="text-sm font-semibold text-action">
          ← All flats
        </Link>
        <h1 className="mt-2 text-3xl font-semibold">{d.flat.data ? `Flat ${d.flat.data.unit_number}` : `Flat ${flatId}`}</h1>
      </div>

      <section aria-labelledby="status-h" className="card">
        <h2 id="status-h" className="mb-4 text-xl font-semibold">
          Status
        </h2>
        <DataState
          data={d.flat.data}
          loading={d.flat.loading}
          error={d.flat.error}
          onRetry={d.flat.refetch}
          isEmpty={() => false}
          emptyTitle="Flat not found"
          loadingLabel="Loading flat"
        >
          {(flat) => (
            <div className="grid gap-6 md:grid-cols-2">
              <dl className="grid grid-cols-2 gap-4 text-sm">
                <div>
                  <dt className="text-ink-2">Live flow</dt>
                  <dd className="text-2xl font-semibold">{fmtNumber(d.currentFlow)} <span className="text-sm font-normal text-ink-2">L/min</span></dd>
                </div>
                <div>
                  <dt className="text-ink-2">Floor / residents</dt>
                  <dd className="font-semibold">{flat.floor ?? '–'} / {flat.residents}</dd>
                </div>
                <div>
                  <dt className="text-ink-2">Baseline</dt>
                  <dd><StatusBadge tone={flat.baseline_status === 'ready' ? 'safe' : 'neutral'}>{humanize(String(flat.baseline_status ?? 'unknown'))}</StatusBadge></dd>
                </div>
                <div>
                  <dt className="text-ink-2">Sensor</dt>
                  <dd>
                    {(() => {
                      // sensor_status is an object {status,last_seen,missed_readings}; never render it directly.
                      const status = sensors[String(flat.id)] ?? flat.sensor_status?.status ?? 'unknown';
                      return (
                        <>
                          <StatusBadge tone={status === 'online' ? 'safe' : status === 'late' ? 'warn' : status === 'offline' ? 'error' : 'neutral'}>
                            {humanize(status)}
                          </StatusBadge>
                          <span className="ml-2 text-xs text-ink-2">
                            Last seen {fmtTime(flat.sensor_status?.last_seen ?? flat.last_reading_at)}
                            {flat.sensor_status?.missed_readings ? ` · ${flat.sensor_status.missed_readings} missed` : ''}
                          </span>
                        </>
                      );
                    })()}
                  </dd>
                </div>
              </dl>
              <ValveControls flatId={flat.id} valveState={d.valveState} pendingCommand={d.pendingCommand} onChanged={d.flat.refetch} />
            </div>
          )}
        </DataState>
      </section>

      <section aria-labelledby="chart-h" className="card">
        <h2 id="chart-h" className="mb-4 text-xl font-semibold">
          Flow vs baseline
        </h2>
        <DataState
          data={d.history.data}
          loading={d.history.loading}
          error={d.history.error}
          onRetry={d.history.refetch}
          isEmpty={() => d.readings.length === 0}
          emptyTitle="No readings yet"
          emptyHint="Readings will appear as soon as the sensor reports."
          loadingLabel="Loading readings"
        >
          {() => <FlowChart readings={d.readings} baseline={d.baseline.data} forecast={d.forecast.data} />}
        </DataState>
        {d.baseline.error ? <p className="mt-2 text-xs text-ink-2">Baseline unavailable: {d.baseline.error.message}</p> : null}
        {d.forecast.error ? <p className="mt-1 text-xs text-ink-2">Forecast unavailable: {d.forecast.error.message}</p> : null}
        {d.forecast.data && !d.forecast.error ? (
          <p className="mt-1 text-xs text-ink-2">
            Forecast model {String(d.forecast.data.model_version)}
            {typeof d.forecast.data.mape === 'number' ? ` · MAPE ${fmtNumber(d.forecast.data.mape)}%` : ''}
          </p>
        ) : null}
      </section>

      <div className="grid gap-8 lg:grid-cols-2">
        <section aria-labelledby="alerts-h" className="card">
          <h2 id="alerts-h" className="mb-4 text-xl font-semibold">
            Alert timeline
          </h2>
          <DataState
            data={alerts.alerts}
            loading={alerts.loading}
            error={alerts.error}
            onRetry={alerts.refetch}
            emptyTitle="No alerts"
            emptyHint="This flat has no alert history."
            loadingLabel="Loading alerts"
          >
            {(list) => <AlertTimeline alerts={list} />}
          </DataState>
        </section>
        <section aria-labelledby="anom-h" className="card">
          <h2 id="anom-h" className="mb-4 text-xl font-semibold">
            Anomalies
          </h2>
          <DataState
            data={d.anomalies.data}
            loading={d.anomalies.loading}
            error={d.anomalies.error}
            onRetry={d.anomalies.refetch}
            emptyTitle="No anomalies detected"
            loadingLabel="Loading anomalies"
          >
            {(list) => <AnomalyList anomalies={list} />}
          </DataState>
        </section>
      </div>
    </div>
  );
}
