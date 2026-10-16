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

export function severityTone(severity: string | number | null | undefined): Tone {
  if (typeof severity === 'number') return severity >= 0.7 ? 'error' : severity >= 0.4 ? 'warn' : 'safe';
  const s = (severity ?? '').toLowerCase();
  if (/(crit|high|severe|burst)/.test(s)) return 'error';
  if (/(med|warn|moder)/.test(s)) return 'warn';
  if (s) return 'safe';
  return 'neutral';
}

export function alertStateTone(state: string): Tone {
  switch (state) {
    case 'active':
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

export function valveTone(v: ValveState): Tone {
  return v === 'open' ? 'safe' : v === 'closed' ? 'warn' : 'neutral';
}

export function isOpenAlert(a: Alert): boolean {
  return a.state !== 'resolved';
}

export function describeSource(source: string): string {
  const s = source.toLowerCase();
  if (/(llm|ai|claude|gpt|model)/.test(s)) return `AI-generated (${source})`;
  if (/(template|rule|fallback)/.test(s)) return `Template (${source})`;
  return source;
}

/** Aria-friendly relative sort: newest first. */
export function byNewest<T extends { created_at?: string; detected_at?: string }>(a: T, b: T): number {
  const ta = Date.parse(a.created_at ?? a.detected_at ?? '') || 0;
  const tb = Date.parse(b.created_at ?? b.detected_at ?? '') || 0;
  return tb - ta;
}

import type { Flat } from './types';

export function flatStatus(flat: Flat, hasActiveAlert: boolean): { tone: Tone; label: string } {
  if (hasActiveAlert) return { tone: 'error', label: 'Alert' };
  if (/(off|fail|error|lost|stale)/i.test(flat.sensor_status)) return { tone: 'warn', label: 'Sensor issue' };
  if (flat.valve_state === 'closed') return { tone: 'warn', label: 'Valve closed' };
  if (/(learn|train|pending|insufficient)/i.test(flat.baseline_status)) return { tone: 'neutral', label: 'Learning' };
  return { tone: 'safe', label: 'Normal' };
}
