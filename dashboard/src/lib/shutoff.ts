import type { Alert } from './types';
import { isOpenAlert } from './format';

export type ShutoffPhase =
  /** Warning window running: automatic action will be attempted at `deadline`. */
  | { kind: 'countdown'; remainingS: number; deadline: number }
  /** Window elapsed; the backend is attempting it / waiting for the device to confirm. NOT shut off yet. */
  | { kind: 'pending' }
  /** The backend tried and the device never confirmed. Needs a human. */
  | { kind: 'gave_up' };

/**
 * Derives the pre-shut-off warning UI state from an alert. Returns null when there is nothing
 * to warn about (not warned, already confirmed `auto_shutoff`, or no longer open).
 */
export function shutoffPhase(alert: Alert, nowMs: number, warningSeconds: number): ShutoffPhase | null {
  if (String(alert.state) === 'auto_shutoff' || !isOpenAlert(alert)) return null;
  if (alert.shutoff_gave_up_at) return { kind: 'gave_up' };
  if (!alert.shutoff_warned_at) return null;
  const warned = Date.parse(alert.shutoff_warned_at);
  if (Number.isNaN(warned)) return null;
  const deadline = warned + warningSeconds * 1000;
  const remainingS = Math.ceil((deadline - nowMs) / 1000);
  return remainingS > 0 ? { kind: 'countdown', remainingS, deadline } : { kind: 'pending' };
}

export function fmtCountdown(totalS: number): string {
  const s = Math.max(0, Math.floor(totalS));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
