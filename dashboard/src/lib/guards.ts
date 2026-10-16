import type { Alert, Id, Role, ValveState, WsMessage } from './types';

export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}
export function isId(v: unknown): v is Id {
  return (typeof v === 'string' && v.length > 0) || (typeof v === 'number' && Number.isFinite(v));
}
function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}
function isIsoDate(v: unknown): v is string {
  return typeof v === 'string' && !Number.isNaN(Date.parse(v));
}

export function toRole(v: unknown): Role {
  return v === 'admin' || v === 'maintenance' ? v : 'resident';
}

export function toValveState(v: unknown): ValveState {
  if (typeof v !== 'string') return 'unknown';
  const s = v.toLowerCase();
  if (s === 'open' || s === 'opened') return 'open';
  if (s === 'closed' || s === 'close') return 'closed';
  return 'unknown';
}

export function isAlert(v: unknown): v is Alert {
  return (
    isRecord(v) &&
    isId(v.id) &&
    isId(v.flat_id) &&
    typeof v.state === 'string' &&
    (typeof v.tier === 'string' || isNum(v.tier)) &&
    typeof v.message === 'string' &&
    (v.created_at === undefined || typeof v.created_at === 'string')
  );
}

/** Normalises an alert received over the wire, filling optional fields. */
function normaliseAlert(v: Alert): Alert {
  const created = typeof v.created_at === 'string' ? v.created_at : new Date().toISOString();
  return {
    ...v,
    anomaly_id: v.anomaly_id ?? null,
    severity_score: isNum(v.severity_score) ? v.severity_score : 0,
    message_source: typeof v.message_source === 'string' ? v.message_source : 'unknown',
    created_at: created,
    updated_at: typeof v.updated_at === 'string' ? v.updated_at : created,
  };
}

/** Validates an unknown WebSocket payload. Returns null for anything malformed. */
export function parseWsMessage(raw: unknown): WsMessage | null {
  let data: unknown = raw;
  if (typeof raw === 'string') {
    try {
      data = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!isRecord(data) || typeof data.type !== 'string') return null;
  switch (data.type) {
    case 'reading':
      if (isId(data.flat_id) && isNum(data.flow_rate) && isIsoDate(data.timestamp)) {
        return {
          type: 'reading',
          flat_id: data.flat_id,
          flow_rate: data.flow_rate,
          timestamp: data.timestamp,
        };
      }
      return null;
    case 'alert': {
      // Accept either {type, alert:{...}} or the alert fields inlined next to `type`.
      const candidate = isRecord(data.alert) ? data.alert : data;
      return isAlert(candidate) ? { type: 'alert', alert: normaliseAlert(candidate) } : null;
    }
    case 'valve': {
      const state = toValveState(data.state ?? data.valve_state);
      return isId(data.flat_id) ? { type: 'valve', flat_id: data.flat_id, state } : null;
    }
    default:
      return null;
  }
}
