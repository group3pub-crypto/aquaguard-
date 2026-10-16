import Link from 'next/link';
import { flatStatus, fmtNumber } from '@/lib/format';
import type { Flat, SensorStatusName } from '@/lib/types';
import type { LiveReading } from '@/context/LiveProvider';
import { Sparkline } from './Sparkline';
import { StatusBadge } from './StatusBadge';

/**
 * `GET /flats` has no current_flow or sensor status, so the live flow comes only from
 * WebSocket readings (and sensor status only from WebSocket `sensor` events).
 */
export function FlatTile({
  flat,
  live,
  hasAlert,
  sensor,
}: {
  flat: Flat;
  live: LiveReading[] | undefined;
  hasAlert: boolean;
  sensor?: SensorStatusName | null;
}) {
  const status = flatStatus(flat, hasAlert, sensor);
  const last = live && live.length ? live[live.length - 1] : undefined;
  return (
    <li>
      <Link
        href={`/flat/${encodeURIComponent(String(flat.id))}`}
        className="block rounded-lg border border-line bg-canvas p-4 transition-colors hover:bg-panel"
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-semibold">Flat {flat.unit_number}</span>
          <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
        </div>
        <p className="mt-0.5 text-xs text-ink-2">
          {flat.floor !== null && flat.floor !== undefined ? `Floor ${flat.floor} · ` : ''}
          {flat.residents} resident{flat.residents === 1 ? '' : 's'}
        </p>
        <p className="mt-3 text-2xl font-semibold">
          {last ? fmtNumber(last.flow_rate) : '–'} <span className="text-sm font-normal text-ink-2">L/min</span>
        </p>
        {last ? null : <p className="text-xs text-ink-2">Waiting for live data</p>}
        <Sparkline values={(live ?? []).map((r) => r.flow_rate)} />
      </Link>
    </li>
  );
}
