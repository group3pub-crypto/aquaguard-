import Link from 'next/link';
import { flatStatus, fmtNumber } from '@/lib/format';
import type { Flat } from '@/lib/types';
import type { LiveReading } from '@/context/LiveProvider';
import { Sparkline } from './Sparkline';
import { StatusBadge } from './StatusBadge';

export function FlatTile({
  flat,
  live,
  hasAlert,
}: {
  flat: Flat;
  live: LiveReading[] | undefined;
  hasAlert: boolean;
}) {
  const status = flatStatus(flat, hasAlert);
  const last = live && live.length ? live[live.length - 1] : undefined;
  const flow = last ? last.flow_rate : flat.current_flow;
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
          Floor {flat.floor} · {flat.residents} resident{flat.residents === 1 ? '' : 's'}
        </p>
        <p className="mt-3 text-2xl font-semibold">
          {fmtNumber(flow)} <span className="text-sm font-normal text-ink-2">L/min</span>
        </p>
        <Sparkline values={(live ?? []).map((r) => r.flow_rate)} />
      </Link>
    </li>
  );
}
