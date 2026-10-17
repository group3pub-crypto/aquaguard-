'use client';
import { useState } from 'react';
import { api } from '@/lib/api';
import { fmtNumber } from '@/lib/format';
import { useLive } from '@/context/LiveProvider';
import { useAlerts } from '@/hooks/useAlerts';
import { usePolling } from '@/hooks/usePolling';
import { DataState, ErrorState, isForbidden, isNotFound } from '@/components/States';
import { useAuth } from '@/context/AuthProvider';
import { FlatTile } from '@/components/FlatTile';
import type { Society, SocietyStats } from '@/lib/types';

function StatBand({ stats }: { stats: SocietyStats }) {
  const items = [
    { label: 'Flats', value: fmtNumber(stats.flats, 0), unit: '' },
    { label: 'Water used (24 h)', value: fmtNumber(stats.volume_24h_l), unit: 'L' },
    { label: 'Active alerts', value: fmtNumber(stats.active_alerts, 0), unit: '' },
    { label: 'Sensors offline', value: fmtNumber(stats.sensors_offline, 0), unit: '' },
  ];
  return (
    <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line lg:grid-cols-4">
      {items.map((i) => (
        <div key={i.label} className="bg-panel p-6">
          <dt className="text-sm text-ink-2">{i.label}</dt>
          <dd className="mt-1 text-3xl font-semibold">
            {i.value} {i.unit ? <span className="text-base font-normal text-ink-2">{i.unit}</span> : null}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function OverviewView() {
  const { user } = useAuth();
  const societies = usePolling<Society[]>((signal) => api.societies({ signal }), { key: 'societies' });
  const [selected, setSelected] = useState<string | null>(null);
  const list = Array.isArray(societies.data) ? societies.data : [];
  const society = list.find((s) => String(s.id) === selected) ?? list[0];
  const sid = society ? String(society.id) : '';

  const stats = usePolling<SocietyStats>((signal) => api.societyStats(sid, { signal }), {
    key: `stats:${sid}`,
    intervalMs: 30_000,
    enabled: !!sid,
  });
  // Residents only receive their own flat (and possibly no society): list flats regardless.
  const flats = usePolling((signal) => api.flats(sid || undefined, { signal }), {
    key: `flats:${sid}`,
    intervalMs: 30_000,
  });
  const active = useAlerts({ open: true });
  const { readings, sensors, events } = useLive();
  const alerted = new Set((active.alerts ?? []).map((a) => String(a.flat_id)));

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold">{society ? society.name : 'Society overview'}</h1>
          <p className="mt-1 text-ink-2">Live water usage and leak status across all flats.</p>
        </div>
        {list.length > 1 ? (
          <div>
            <label htmlFor="society" className="label">
              Society
            </label>
            <select id="society" className="field" value={sid} onChange={(e) => setSelected(e.target.value)}>
              {list.map((s) => (
                <option key={String(s.id)} value={String(s.id)}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
      </div>

      {societies.error && !societies.data && !isForbidden(societies.error) && !isNotFound(societies.error) ? (
        <ErrorState message={societies.error.message} onRetry={societies.refetch} />
      ) : null}
      {list.length === 0 && !societies.loading && user?.role === 'admin' ? (
        <p className="text-sm text-ink-2">No societies yet. Create one under Societies in the admin menu.</p>
      ) : null}
      {sid && !(isForbidden(stats.error) || isNotFound(stats.error)) ? (
        <section aria-label="Society statistics">
          <DataState
            data={stats.data}
            loading={stats.loading}
            error={stats.error}
            onRetry={stats.refetch}
            isEmpty={() => false}
            emptyTitle="No statistics"
            loadingLabel="Loading statistics"
          >
            {(s) => <StatBand stats={s} />}
          </DataState>
        </section>
      ) : null}
      <section aria-labelledby="flats-h">
        <h2 id="flats-h" className="mb-4 text-xl font-semibold">
          Flats
        </h2>
        <DataState
          data={flats.data}
          loading={flats.loading}
          error={flats.error}
          onRetry={flats.refetch}
          emptyTitle="No flats yet"
          emptyHint="Flats appear here once sensors are registered."
          loadingLabel="Loading flats"
        >
          {(fs) => (
            <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {fs.map((f) => (
                <FlatTile key={String(f.id)} flat={f} live={readings[String(f.id)]} hasAlert={alerted.has(String(f.id))} sensor={sensors[String(f.id)]} />
              ))}
            </ul>
          )}
        </DataState>
      </section>
      <section aria-labelledby="live-h">
        <h2 id="live-h" className="mb-4 text-xl font-semibold">
          Live activity
        </h2>
        {events.length === 0 ? (
          <p className="text-sm text-ink-2">Alerts, anomalies and valve changes appear here in real time.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {events.slice(0, 10).map((e) => (
              <li key={e.id}>{e.text}</li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
