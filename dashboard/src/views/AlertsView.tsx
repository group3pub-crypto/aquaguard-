'use client';
import { useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { byNewest, humanize } from '@/lib/format';
import { useAlerts } from '@/hooks/useAlerts';
import { AlertRow } from '@/components/AlertRow';
import { DataState } from '@/components/States';

const TABS = ['all', 'active', 'acknowledged', 'snoozed', 'overridden', 'resolved'] as const;
type Tab = (typeof TABS)[number];

export function AlertsView() {
  const uid = useId();
  const { alerts, loading, error, refetch } = useAlerts({});
  const [tab, setTab] = useState<Tab>('active');
  const [tier, setTier] = useState('all');
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const all = alerts ?? [];
  const tiers = [...new Set(all.map((a) => String(a.tier)))].sort();
  const inTier = all.filter((a) => tier === 'all' || String(a.tier) === tier);
  const count = (t: Tab) => inTier.filter((a) => t === 'all' || a.state === t).length;
  const visible = inTier.filter((a) => tab === 'all' || a.state === tab).sort(byNewest);

  const onKeyDown = (e: KeyboardEvent, i: number) => {
    let n = i;
    if (e.key === 'ArrowRight') n = (i + 1) % TABS.length;
    else if (e.key === 'ArrowLeft') n = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = TABS.length - 1;
    else return;
    e.preventDefault();
    const next = TABS[n];
    if (next) {
      setTab(next);
      tabRefs.current[n]?.focus();
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold">Alerts</h1>
          <p className="mt-1 text-ink-2">Review, acknowledge and resolve leak alerts.</p>
        </div>
        <div>
          <label htmlFor={`${uid}-tier`} className="label">
            Tier
          </label>
          <select id={`${uid}-tier`} className="field" value={tier} onChange={(e) => setTier(e.target.value)}>
            <option value="all">All tiers</option>
            {tiers.map((t) => (
              <option key={t} value={t}>
                Tier {t}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div role="tablist" aria-label="Alert state" className="flex gap-1 overflow-x-auto border-b border-line">
        {TABS.map((t, i) => {
          const selected = t === tab;
          return (
            <button
              key={t}
              ref={(el) => {
                tabRefs.current[i] = el;
              }}
              role="tab"
              type="button"
              id={`${uid}-tab-${t}`}
              aria-selected={selected}
              aria-controls={`${uid}-panel`}
              tabIndex={selected ? 0 : -1}
              onClick={() => setTab(t)}
              onKeyDown={(e) => onKeyDown(e, i)}
              className={`-mb-px whitespace-nowrap border-b-2 px-4 py-2 text-sm font-semibold ${
                selected ? 'border-action text-action' : 'border-transparent text-ink-2 hover:text-ink'
              }`}
            >
              {humanize(t)} <span className="ml-1 text-xs text-ink-2">{count(t)}</span>
            </button>
          );
        })}
      </div>

      <div role="tabpanel" id={`${uid}-panel`} aria-labelledby={`${uid}-tab-${tab}`} tabIndex={0}>
        <DataState
          data={alerts}
          loading={loading}
          error={error}
          onRetry={refetch}
          isEmpty={() => visible.length === 0}
          emptyTitle={`No ${tab === 'all' ? '' : `${tab} `}alerts`}
          emptyHint="Nothing to show for this filter."
          loadingLabel="Loading alerts"
        >
          {() => (
            <ul className="space-y-3">
              {visible.map((a) => (
                <AlertRow key={String(a.id)} alert={a} onChanged={refetch} />
              ))}
            </ul>
          )}
        </DataState>
      </div>
    </div>
  );
}
