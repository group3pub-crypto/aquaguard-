'use client';
import { getConfig } from '@/lib/config';
import { fmtCountdown, shutoffPhase } from '@/lib/shutoff';
import { useNow } from '@/hooks/useNow';
import type { Alert } from '@/lib/types';

/**
 * Prominent banner for the pre-shut-off warning window. It states what WILL be attempted, never
 * that water IS shut off (only a confirmed `auto_shutoff` alert may say that).
 */
export function ShutoffWarning({ alert, canAct }: { alert: Alert; canAct: boolean }) {
  const active = !!alert.shutoff_warned_at && !alert.shutoff_gave_up_at;
  const now = useNow(active);
  const phase = shutoffPhase(alert, now, getConfig().shutoffWarningSeconds);
  if (!phase) return null;

  if (phase.kind === 'gave_up') {
    return (
      <div role="alert" className="mt-3 rounded-md border-2 border-error bg-error-soft p-3 text-sm">
        <p className="font-semibold text-error">Automatic action was not confirmed by the device.</p>
        <p>The valve may still be open. Check the flat manually and close the valve if needed.</p>
      </div>
    );
  }
  return (
    <div role="alert" className="mt-3 rounded-md border-2 border-error bg-error-soft p-3 text-sm" data-testid="shutoff-warning">
      {phase.kind === 'countdown' ? (
        <>
          <p className="font-semibold text-error">
            Automatic valve close will be attempted in{' '}
            <time aria-label={`${phase.remainingS} seconds`}>{fmtCountdown(phase.remainingS)}</time>
          </p>
          <p>
            {canAct
              ? 'Acknowledge or override this alert now to cancel it.'
              : 'Contact your building maintenance team if this is not a real leak.'}
          </p>
        </>
      ) : (
        <p className="font-semibold text-error">Automatic valve close is being attempted. Waiting for the device to confirm.</p>
      )}
    </div>
  );
}
