'use client';
import { useState } from 'react';
import { api } from '@/lib/api';
import { valveTone } from '@/lib/format';
import { useAuth } from '@/context/AuthProvider';
import { useLive } from '@/context/LiveProvider';
import { useAction } from '@/hooks/useAction';
import type { Id, ValveCommandRecord, ValveState } from '@/lib/types';
import { ConfirmDialog } from './ConfirmDialog';
import { StatusBadge } from './StatusBadge';

export function ValveControls({
  flatId,
  valveState,
  pendingCommand,
  onChanged,
}: {
  flatId: Id;
  valveState: ValveState;
  /** Latest unconfirmed command seen on the WebSocket, if any. */
  pendingCommand?: ValveCommandRecord;
  onChanged?: () => void;
}) {
  const { user, can } = useAuth();
  const { log } = useLive();
  const [confirming, setConfirming] = useState(false);
  const { busy, error, run, clearError } = useAction();

  const issue = async (kind: 'open' | 'close', reason?: string) => {
    if (!user) return;
    const body = {
      source: 'manual_dashboard' as const,
      issued_by: user.username,
      ...(reason ? { reason } : {}),
    };
    const ok = await run(() => (kind === 'close' ? api.closeValve(flatId, body) : api.openValve(flatId, body)));
    if (ok) {
      log(`Valve ${kind} requested for flat ${flatId} by ${user.username}`);
      setConfirming(false);
      onChanged?.();
    }
  };

  return (
    <div>
      <div className="flex items-center gap-3">
        <span className="text-sm text-ink-2">Valve</span>
        <StatusBadge tone={valveTone(valveState)}>{valveState}</StatusBadge>
        {pendingCommand ? <span className="text-xs text-ink-2">{pendingCommand.command} command awaiting device confirmation</span> : null}
      </div>
      {can.controlValve ? (
        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy || valveState === 'closed'}
            onClick={() => {
              clearError();
              setConfirming(true);
            }}
          >
            Close valve
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy || valveState === 'open'}
            onClick={() => void issue('open')}
          >
            Open valve
          </button>
        </div>
      ) : (
        <p className="mt-3 text-sm text-ink-2">Valve control requires administrator access.</p>
      )}
      {error && !confirming ? (
        <p role="alert" className="mt-2 text-sm text-error">
          {error}
        </p>
      ) : null}
      {confirming ? (
        <ConfirmDialog
          title="Close water valve?"
          description="Water supply to this flat will be cut until the valve is reopened."
          confirmLabel="Close valve"
          danger
          reasonLabel="Reason (optional)"
          busy={busy}
          error={error}
          onCancel={() => setConfirming(false)}
          onConfirm={(reason) => void issue('close', reason || undefined)}
        />
      ) : null}
    </div>
  );
}
