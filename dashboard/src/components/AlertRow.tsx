'use client';
import { useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api';
import { canTransition } from '@/lib/alertActions';
import { alertStateLabel, alertStateTone, describeSource, fmtNumber, fmtTime, isOpenAlert } from '@/lib/format';
import { useAuth } from '@/context/AuthProvider';
import { useAction } from '@/hooks/useAction';
import type { Alert } from '@/lib/types';
import { ConfirmDialog } from './ConfirmDialog';
import { StatusBadge } from './StatusBadge';

const SNOOZE_OPTIONS = [15, 30, 60, 120] as const;

export function AlertRow({
  alert,
  onChanged,
  showFlatLink = true,
}: {
  alert: Alert;
  onChanged: () => void;
  showFlatLink?: boolean;
}) {
  const { user, can } = useAuth();
  const { busy, error, run, clearError } = useAction();
  const [snooze, setSnooze] = useState<(typeof SNOOZE_OPTIONS)[number]>(30);
  const [overriding, setOverriding] = useState(false);
  const who = user?.username ?? '';
  const state = String(alert.state);
  const open = isOpenAlert(alert);

  const act = async (fn: () => Promise<unknown>) => {
    if (!user) return false;
    const ok = await run(fn);
    if (ok) onChanged();
    return ok;
  };

  return (
    <li className="rounded-lg border border-line bg-canvas p-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge tone={alertStateTone(state)}>{alertStateLabel(state)}</StatusBadge>
        <StatusBadge tone="neutral">Tier {String(alert.tier)}</StatusBadge>
        {showFlatLink ? (
          <Link href={`/flat/${encodeURIComponent(String(alert.flat_id))}`} className="text-sm font-semibold text-action">
            Flat {String(alert.flat_id)}
          </Link>
        ) : null}
        <time dateTime={alert.created_at} className="ml-auto text-xs text-ink-2">
          {fmtTime(alert.created_at)}
        </time>
      </div>
      <p className="mt-3">{alert.message ?? 'No message provided.'}</p>
      <p className="mt-1 text-xs text-ink-2">
        Severity {fmtNumber(alert.severity_score, 2)} · Message: {describeSource(alert.message_source)}
      </p>

      {open ? (
        <div className="mt-4 flex flex-wrap items-center gap-2" role="group" aria-label="Alert actions">
          {can.acknowledge && canTransition('acknowledge', state) ? (
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void act(() => api.acknowledge(alert.id, { acknowledged_by: who }))}>
              Acknowledge
            </button>
          ) : null}
          {can.snooze && canTransition('snooze', state) ? (
            <span className="inline-flex items-center gap-1">
              <label className="sr-only" htmlFor={`snooze-${alert.id}`}>
                Snooze duration
              </label>
              <select id={`snooze-${alert.id}`} className="field !min-h-[40px] !w-auto" value={snooze} onChange={(e) => setSnooze(Number(e.target.value) as (typeof SNOOZE_OPTIONS)[number])}>
                {SNOOZE_OPTIONS.map((m) => (
                  <option key={m} value={m}>
                    {m} min
                  </option>
                ))}
              </select>
              <button type="button" className="btn" disabled={busy} onClick={() => void act(() => api.snooze(alert.id, { duration_minutes: snooze, snoozed_by: who }))}>
                Snooze
              </button>
            </span>
          ) : null}
          {can.resolve && canTransition('resolve', state) ? (
            <button type="button" className="btn" disabled={busy} onClick={() => void act(() => api.resolve(alert.id, { resolved_by: who }))}>
              Resolve
            </button>
          ) : null}
          {can.override && canTransition('override', state) ? (
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => {
                clearError();
                setOverriding(true);
              }}
            >
              Override
            </button>
          ) : null}
          {can.controlValve ? (
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() =>
                void act(() => api.openValve(alert.flat_id, { source: 'manual_dashboard', issued_by: who }))
              }
            >
              Reopen valve
            </button>
          ) : null}
        </div>
      ) : null}
      {error && !overriding ? (
        <p role="alert" className="mt-2 text-sm text-error">
          {error}
        </p>
      ) : null}
      {overriding ? (
        <ConfirmDialog
          title="Override this alert?"
          description="The alert is marked as overridden and automatic action is suppressed. This is recorded against your account."
          confirmLabel="Override alert"
          danger
          reasonLabel="Reason (required)"
          reasonRequired
          busy={busy}
          error={error}
          onCancel={() => setOverriding(false)}
          onConfirm={(reason) =>
            void act(() => api.override(alert.id, { reason, overridden_by: who })).then((ok) => {
              if (ok) setOverriding(false);
            })
          }
        />
      ) : null}
    </li>
  );
}
