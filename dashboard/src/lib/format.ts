import { OPEN_ALERT_STATES } from './types';
import type { Alert, ValveState } from './types';

export type Tone = 'safe' | 'warn' | 'error' | 'neutral' | 'action';

export function fmtNumber(n: number | null | undefined, digits = 1): string {
  return typeof n === 'number' && Number.isFinite(n) ? n.toFixed(digits) : '–';
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '–';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? '–'
    : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function humanize(s: string): string {
  const t = s.replace(/_/g, ' ');
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export function severityTone(severity: number | string | null | undefined): Tone {
  if (typeof severity === 'number') return severity >= 0.7 ? 'error' : severity >= 0.4 ? 'warn' : 'safe';
  const s = (severity ?? '').toLowerCase();
  if (/(crit|high|severe|burst)/.test(s)) return 'error';
  if (/(med|warn|moder)/.test(s)) return 'warn';
  if (s) return 'safe';
  return 'neutral';
}

/** 0..1 -> "82%" (backend severity/confidence are fractions). */
export function fmtPercent(n: number | null | undefined, digits = 0): string {
  return typeof n === 'number' && Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : '–';
}

export function alertStateTone(state: string): Tone {
  switch (state) {
    case 'nudge':
      return 'action';
    case 'alert':
    case 'ticket':
    case 'auto_shutoff':
      return 'error';
    case 'acknowledged':
    case 'snoozed':
      return 'warn';
    case 'resolved':
      return 'safe';
    case 'overridden':
      return 'action';
    default:
      return 'neutral';
  }
}

const STATE_LABELS: Record<string, string> = {
  nudge: 'Nudge',
  alert: 'Alert',
  ticket: 'Ticket raised',
  auto_shutoff: 'Auto shut-off',
};
export function alertStateLabel(state: string): string {
  return STATE_LABELS[state] ?? humanize(state);
}

export function valveTone(v: ValveState): Tone {
  return v === 'open' ? 'safe' : v === 'closed' ? 'warn' : 'neutral';
}

export function isOpenAlert(a: Alert): boolean {
  return (OPEN_ALERT_STATES as readonly string[]).includes(String(a.state));
}

/**
 * Subtle provenance label for an alert message. "AI-written" is shown ONLY for the backend's
 * `anthropic` source; `template` and `template_fallback` (AI unavailable) are fixed templates.
 */
export function describeSource(source: string | null | undefined): string {
  switch ((source ?? '').toLowerCase()) {
    case 'anthropic':
      return 'AI-written';
    case 'template':
      return 'Template';
    case 'template_fallback':
      return 'Template (AI unavailable)';
    case '':
      return 'Source unknown';
    default:
      return `Source: ${source}`;
  }
}

const SHUTOFF_CLAIM = /(shut\s*-?\s*off|shut\s+down|turned\s+off|cut\s+off|closed)[^.]*\b(water|supply|valve)|\b(water|supply|valve)[^.]*(shut\s*-?\s*off|shut\s+down|turned\s+off|cut\s+off|has\s+been\s+closed|was\s+closed)/i;

/**
 * Safety net: never show a claim that water was shut off unless the alert is in the
 * CONFIRMED `auto_shutoff` state (the device acknowledged the close). Anything else that
 * reads like such a claim is withheld.
 */
export function safeAlertMessage(a: Pick<Alert, 'state' | 'message'>): string {
  const msg = a.message ?? 'No message provided.';
  if (String(a.state) !== 'auto_shutoff' && SHUTOFF_CLAIM.test(msg)) {
    return 'Leak alert. The supply has NOT been confirmed closed - check the flat.';
  }
  return msg;
}

/** Aria-friendly relative sort: newest first. */
export function byNewest<T extends { created_at?: string; detected_at?: string }>(a: T, b: T): number {
  const ta = Date.parse(a.created_at ?? a.detected_at ?? '') || 0;
  const tb = Date.parse(b.created_at ?? b.detected_at ?? '') || 0;
  return tb - ta;
}

import type { Flat, SensorStatusName } from './types';

export function flatStatus(
  flat: Flat,
  hasOpenAlert: boolean,
  sensor?: SensorStatusName | null,
): { tone: Tone; label: string } {
  if (hasOpenAlert) return { tone: 'error', label: 'Alert' };
  if (sensor === 'offline') return { tone: 'warn', label: 'Sensor offline' };
  if (flat.valve_state === 'closed') return { tone: 'warn', label: 'Valve closed' };
  if (flat.baseline_status === 'learning') return { tone: 'neutral', label: 'Learning' };
  return { tone: 'safe', label: 'Normal' };
}
