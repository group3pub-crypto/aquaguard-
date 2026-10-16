import type { ReactNode } from 'react';
import type { Tone } from '@/lib/format';

const tones: Record<Tone, string> = {
  safe: 'bg-safe-soft text-ink border-safe',
  warn: 'bg-warn-soft text-ink border-warn',
  error: 'bg-error-soft text-ink border-error',
  action: 'bg-action-soft text-ink border-action',
  neutral: 'bg-panel text-ink-2 border-line',
};
const dots: Record<Tone, string> = {
  safe: 'bg-safe',
  warn: 'bg-warn',
  error: 'bg-error',
  action: 'bg-action',
  neutral: 'bg-ink-2',
};

export function StatusBadge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-pill border px-2.5 py-0.5 text-xs font-semibold ${tones[tone]}`}
    >
      <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${dots[tone]}`} />
      {children}
    </span>
  );
}
