'use client';
import { useId, useState } from 'react';
import { api } from '@/lib/api';
import { fmtNumber, fmtTime, humanize } from '@/lib/format';
import { useAuth } from '@/context/AuthProvider';
import { useLive } from '@/context/LiveProvider';
import { useAction } from '@/hooks/useAction';
import { usePolling } from '@/hooks/usePolling';
import { DataState, EmptyState } from '@/components/States';
import { FAULT_TYPES, type FaultType } from '@/lib/types';

export function DemoView() {
  const uid = useId();
  const { can } = useAuth();
  const { events, readings, log } = useLive();
  const flats = usePolling((signal) => api.flats(undefined, { signal }), { key: 'demo-flats', enabled: can.demo });
  const status = usePolling((signal) => api.demoStatus({ signal }), {
    key: 'demo-status',
    intervalMs: 5000,
    enabled: can.demo,
  });
  const [flatId, setFlatId] = useState('');
  const [fault, setFault] = useState<FaultType>('slow_leak');
  const [speed, setSpeed] = useState(1);
  const { busy, error, run } = useAction();

  if (!can.demo) {
    return (
      <div className="card" role="alert">
        <h1 className="text-2xl font-semibold">Demo controls</h1>
        <p className="mt-2 text-ink-2">This page is available to administrators only.</p>
      </div>
    );
  }

  const selected = flatId || (flats.data?.[0] ? String(flats.data[0].id) : '');
  const live = readings[selected];
  const lastFlow = live?.length ? live[live.length - 1]?.flow_rate : undefined;

  const doInject = async () => {
    if (!selected) return;
    const ok = await run(() => api.injectFault({ flat_id: selected, fault_type: fault }));
    if (ok) {
      log(`Injected ${fault} on flat ${selected}`);
      status.refetch();
    }
  };
  const doClear = async () => {
    if (!selected) return;
    const ok = await run(() => api.clearFault(selected));
    if (ok) {
      log(`Cleared fault on flat ${selected}`);
      status.refetch();
    }
  };
  const commitSpeed = async () => {
    const ok = await run(() => api.setSpeed(speed));
    if (ok) {
      log(`Simulation speed set to ${speed}x`);
      status.refetch();
    }
  };

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-semibold">Demo controls</h1>
        <p className="mt-1 text-ink-2">Inject simulated faults and watch AquaGuard react.</p>
      </div>
      {error ? (
        <p role="alert" className="rounded-md border border-error bg-error-soft p-3 text-sm">
          {error}
        </p>
      ) : null}

      <div className="grid gap-8 lg:grid-cols-2">
        <section className="card space-y-6" aria-labelledby={`${uid}-ctl`}>
          <h2 id={`${uid}-ctl`} className="text-xl font-semibold">
            Fault injection
          </h2>
          <DataState
            data={flats.data}
            loading={flats.loading}
            error={flats.error}
            onRetry={flats.refetch}
            emptyTitle="No flats available"
            loadingLabel="Loading flats"
          >
            {(list) => (
              <div>
                <label className="label" htmlFor={`${uid}-flat`}>
                  Flat
                </label>
                <select id={`${uid}-flat`} className="field" value={selected} onChange={(e) => setFlatId(e.target.value)}>
                  {list.map((f) => (
                    <option key={String(f.id)} value={String(f.id)}>
                      Flat {f.unit_number}
                    </option>
                  ))}
                </select>
                {lastFlow !== undefined ? (
                  <p className="mt-2 text-sm text-ink-2">Live flow: {fmtNumber(lastFlow)} L/min</p>
                ) : null}
              </div>
            )}
          </DataState>

          <fieldset>
            <legend className="label">Fault type</legend>
            <div role="radiogroup" aria-label="Fault type" className="flex flex-wrap gap-2">
              {FAULT_TYPES.map((f) => (
                <label
                  key={f}
                  className={`cursor-pointer rounded-pill border px-4 py-2 text-sm font-semibold focus-within:outline focus-within:outline-2 focus-within:outline-action ${
                    fault === f ? 'border-action bg-action-soft text-action' : 'border-line'
                  }`}
                >
                  <input
                    type="radio"
                    name={`${uid}-fault`}
                    value={f}
                    checked={fault === f}
                    onChange={() => setFault(f)}
                    className="sr-only"
                  />
                  {humanize(f)}
                </label>
              ))}
            </div>
          </fieldset>

          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn btn-danger" disabled={busy || !selected} onClick={() => void doInject()}>
              Inject fault
            </button>
            <button type="button" className="btn" disabled={busy || !selected} onClick={() => void doClear()}>
              Clear fault
            </button>
          </div>

          <div>
            <label className="label" htmlFor={`${uid}-speed`}>
              Simulation speed: {speed}x
            </label>
            <input
              id={`${uid}-speed`}
              type="range"
              min={1}
              max={60}
              step={1}
              value={speed}
              onChange={(e) => setSpeed(Number(e.target.value))}
              onMouseUp={() => void commitSpeed()}
              onTouchEnd={() => void commitSpeed()}
              onKeyUp={() => void commitSpeed()}
              className="w-full accent-action"
            />
          </div>
        </section>

        <div className="space-y-8">
          <section className="card" aria-labelledby={`${uid}-st`}>
            <h2 id={`${uid}-st`} className="mb-4 text-xl font-semibold">
              Simulator status
            </h2>
            <DataState
              data={status.data}
              loading={status.loading}
              error={status.error}
              onRetry={status.refetch}
              isEmpty={() => false}
              emptyTitle="No status"
              loadingLabel="Loading status"
            >
              {(s) => (
                <div className="text-sm">
                  <p>
                    Simulator: <strong>{s.simulator_running ? 'running' : 'stopped'}</strong> · Speed {s.speed}x
                  </p>
                  {s.active_faults.length === 0 ? (
                    <p className="mt-2 text-ink-2">No active faults.</p>
                  ) : (
                    <ul className="mt-2 list-disc pl-5">
                      {s.active_faults.map((f) => (
                        <li key={`${f.flat_id}-${f.fault_type}`}>
                          Flat {String(f.flat_id)}: {humanize(f.fault_type)}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </DataState>
          </section>

          <section className="card" aria-labelledby={`${uid}-log`}>
            <h2 id={`${uid}-log`} className="mb-4 text-xl font-semibold">
              Live event log
            </h2>
            {events.length === 0 ? (
              <EmptyState title="No events yet" hint="Alerts and valve changes appear here in real time." />
            ) : (
              <ul aria-live="polite" className="max-h-80 space-y-2 overflow-y-auto text-sm">
                {events.map((e) => (
                  <li key={e.id} className="border-b border-line pb-2">
                    <time dateTime={e.at} className="mr-2 text-xs text-ink-2">
                      {fmtTime(e.at)}
                    </time>
                    {e.text}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
