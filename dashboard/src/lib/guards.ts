import type { Alert, Id, Role, SensorStatusName, ValveCommandRecord, ValveState, WsMessage } from './types';

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

export function toSensorStatus(v: unknown): SensorStatusName | null {
  return v === 'online' || v === 'late' || v === 'offline' ? v : null;
}

export function isAlert(v: unknown): v is Alert {
  return (
    isRecord(v) &&
    isId(v.id) &&
    isId(v.flat_id) &&
    typeof v.state === 'string' &&
    (typeof v.tier === 'string' || isNum(v.tier)) &&
    (v.message === undefined || v.message === null || typeof v.message === 'string') &&
    (v.created_at === undefined || typeof v.created_at === 'string')
  );
}

/** Normalises an alert received over the wire, filling optional fields. */
function normaliseAlert(v: Alert): Alert {
  const created = typeof v.created_at === 'string' ? v.created_at : new Date().toISOString();
  return {
    ...v,
    anomaly_id: v.anomaly_id ?? '',
    severity_score: isNum(v.severity_score) ? v.severity_score : 0,
    message: typeof v.message === 'string' ? v.message : null,
    message_source: typeof v.message_source === 'string' ? v.message_source : null,
    created_at: created,
    updated_at: typeof v.updated_at === 'string' ? v.updated_at : created,
  };
}

function toValveCommand(v: unknown): ValveCommandRecord | null {
  if (!isRecord(v) || !isId(v.flat_id) || typeof v.command !== 'string') return null;
  return {
    id: isId(v.id) ? v.id : '',
    flat_id: v.flat_id,
    alert_id: isId(v.alert_id) ? v.alert_id : null,
    command: v.command,
    source: typeof v.source === 'string' ? v.source : 'unknown',
    issued_by: typeof v.issued_by === 'string' ? v.issued_by : null,
    issued_at: typeof v.issued_at === 'string' ? v.issued_at : new Date().toISOString(),
    confirmed_at: typeof v.confirmed_at === 'string' ? v.confirmed_at : null,
    status: typeof v.status === 'string' ? v.status : 'pending',
  };
}

/**
 * Validates an unknown WebSocket payload. Returns null for anything malformed OR of an
 * unknown `type` (callers must ignore null), so new backend message types never break us.
 */
export function parseWsMessage(raw: unknown): WsMessage | null {
  let data: unknown = raw;
  if (typeof raw === 'string') {
    try {
      data = JSON.parse(raw);
    } catch {
      return null; // includes the plain-text "pong" keep-alive reply
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
          ...(isNum(data.volume) ? { volume: data.volume } : {}),
        };
      }
      return null;
    case 'alert': {
      // Backend: {type:'alert', event, alert:{...AlertOut}}. Tolerate inlined fields too.
      const candidate = isRecord(data.alert) ? data.alert : data;
      if (!isAlert(candidate)) return null;
      return {
        type: 'alert',
        ...(typeof data.event === 'string' ? { event: data.event } : {}),
        alert: normaliseAlert(candidate),
      };
    }
    case 'valve': {
      if (data.event === 'command') {
        const command = toValveCommand(data.command);
        return command ? { type: 'valve', event: 'command', command } : null;
      }
      if (data.event === 'state' || data.event === undefined) {
        const state = toValveState(data.state ?? data.valve_state);
        return isId(data.flat_id) ? { type: 'valve', event: 'state', flat_id: data.flat_id, state } : null;
      }
      return null;
    }
    case 'anomaly':
      if (isId(data.flat_id)) {
        return {
          type: 'anomaly',
          flat_id: data.flat_id,
          anomaly_id: isId(data.anomaly_id) ? data.anomaly_id : '',
          anomaly_type: typeof data.anomaly_type === 'string' ? data.anomaly_type : 'unknown',
          severity: isNum(data.severity) ? data.severity : 0,
        };
      }
      return null;
    case 'sensor': {
      const status = toSensorStatus(data.status);
      return isId(data.flat_id) && status ? { type: 'sensor', flat_id: data.flat_id, status } : null;
    }
    default:
      return null;
  }
}
