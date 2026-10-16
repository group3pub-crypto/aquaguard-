import { alertStateTone, byNewest, describeSource, fmtTime, humanize } from '@/lib/format';
import type { Alert } from '@/lib/types';
import { StatusBadge } from './StatusBadge';

export function AlertTimeline({ alerts }: { alerts: Alert[] }) {
  const sorted = [...alerts].sort(byNewest);
  return (
    <ol className="space-y-4 border-l border-line pl-4">
      {sorted.map((a) => (
        <li key={String(a.id)} className="relative">
          <span aria-hidden="true" className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-canvas bg-action" />
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={alertStateTone(String(a.state))}>{humanize(String(a.state))}</StatusBadge>
            <time dateTime={a.created_at} className="text-xs text-ink-2">
              {fmtTime(a.created_at)}
            </time>
          </div>
          <p className="mt-1 text-sm">{a.message}</p>
          <p className="text-xs text-ink-2">{describeSource(a.message_source)}</p>
        </li>
      ))}
    </ol>
  );
}
