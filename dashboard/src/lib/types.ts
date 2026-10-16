// Strict types for the AquaGuard backend contract (REST /api/v1 + WebSocket).

export type Id = string | number;
export type Role = 'admin' | 'maintenance' | 'resident';
export type ValveState = 'open' | 'closed' | 'unknown';
export interface LoginRequest {
  username: string;
  password: string;
}
export interface LoginResponse {
  access_token: string;
  token_type: string;
  role: Role;
  expires_in: number;
}
export interface RegisterRequest {
  username: string;
  password: string;
  name: string;
}
export interface User {
  id: Id;
  username: string;
  name: string;
  role: Role;
  flat_id?: Id | null;
  society_id?: Id | null;
  created_at?: string;
}

export interface Society {
  id: Id;
  name: string;
  slug: string;
  address: string | null;
  total_flats: number;
  created_at: string;
}
/** GET /societies/{id}/stats */
export interface SocietyStats {
  society_id: Id;
  flats: number;
  active_alerts: number;
  volume_24h_l: number;
  sensors_offline: number;
}

export type SensorStatusName = 'online' | 'late' | 'offline';
export interface SensorStatus {
  status: SensorStatusName;
  last_seen: string | null;
  missed_readings: number;
}
/** GET /flats (list item). Has NO live flow and NO sensor status. */
export interface Flat {
  id: Id;
  society_id: Id;
  unit_number: string;
  floor: number | null;
  residents: number;
  sensor_id: string;
  valve_state: string;
  baseline_status: string;
}
/** GET /flats/{id}. */
export interface FlatDetail extends Flat {
  current_flow: number | null;
  last_reading_at: string | null;
  sensor_status: SensorStatus;
}
export interface Reading {
  id?: number;
  flat_id?: Id;
  timestamp: string;
  flow_rate: number;
  volume: number;
  pressure?: number | null;
  temperature?: number | null;
  battery_pct?: number | null;
  rssi?: number | null;
}
export interface BaselineSlot {
  slot: number;
  median: number;
  mad: number;
  p25: number;
  p75: number;
  p95: number;
  sample_count: number;
}
export interface Baseline {
  id: Id;
  flat_id: Id;
  version: number;
  slots: BaselineSlot[];
  trained_from: string | null;
  trained_to: string | null;
  sample_days: number;
  is_active: boolean;
}
export interface ForecastPoint {
  hour: string;
  predicted_lpm: number;
  lower: number;
  upper: number;
}
export interface Forecast {
  id: Id;
  flat_id: Id;
  generated_at: string;
  horizon_hours: number;
  predictions: ForecastPoint[];
  model_version: string;
  mape: number | null;
}

/** One entry of explanation.top_factors: SHAP contribution or a rule-heuristic reason. */
export interface TopFactor {
  feature?: string;
  value?: number;
  shap?: number;
  note?: string;
}
/** Anomaly.explanation as produced by backend services/detector.py build_explanation. */
export interface Explanation {
  generated_by?: string;
  classifier?: string;
  top_factors?: TopFactor[] | null;
  class_probabilities?: Record<string, number> | null;
  iso_score?: number | null;
}
export interface Anomaly {
  id: Id;
  flat_id: Id;
  detected_at: string;
  anomaly_type: string;
  confidence: number;
  severity: number;
  features?: Record<string, unknown> | null;
  shap_values?: TopFactor[] | null;
  explanation: Explanation | null;
  baseline_version?: number | null;
  window_start?: string | null;
  window_end?: string | null;
  est_loss_lpm: number | null;
  resolved_at: string | null;
}

/** Mirrors backend ALERT_STATES (models/alerts.py). */
export const ALERT_STATES = [
  'nudge',
  'alert',
  'ticket',
  'auto_shutoff',
  'acknowledged',
  'snoozed',
  'overridden',
  'resolved',
] as const;
export type AlertState = (typeof ALERT_STATES)[number];
/** Mirrors backend OPEN_STATES: everything except overridden/resolved. */
export const OPEN_ALERT_STATES: readonly AlertState[] = [
  'nudge',
  'alert',
  'ticket',
  'auto_shutoff',
  'acknowledged',
  'snoozed',
];
export interface Alert {
  id: Id;
  flat_id: Id;
  anomaly_id: Id;
  state: AlertState | string;
  tier: number;
  severity_score: number;
  message: string | null;
  message_source: string | null;
  escalated_at?: string[] | null;
  snoozed_until?: string | null;
  acknowledged_by?: string | null;
  acknowledged_at?: string | null;
  resolved_at?: string | null;
  created_at: string;
  updated_at: string;
}
export interface AlertFilter {
  flat_id?: Id;
  /** Must be one of ALERT_STATES; the backend answers 422 otherwise. */
  state?: AlertState;
  limit?: number;
}

export interface AcknowledgeBody {
  acknowledged_by?: string;
}
export interface SnoozeBody {
  duration_minutes: 15 | 30 | 60 | 120;
  snoozed_by?: string;
}
export interface OverrideBody {
  reason: string;
  overridden_by?: string;
}
export interface ResolveBody {
  resolved_by?: string;
}

export interface ValveCommandRecord {
  id: Id;
  flat_id: Id;
  alert_id: Id | null;
  command: 'open' | 'close' | string;
  source: string;
  issued_by: string | null;
  issued_at: string;
  confirmed_at: string | null;
  status: 'pending' | 'confirmed' | 'failed' | 'timeout' | string;
}
/** GET /valves/{flat_id}. */
export interface ValveInfo {
  flat_id: Id;
  valve_state: string;
  pending: ValveCommandRecord[];
  recent: ValveCommandRecord[];
}
export interface ValveCommand {
  source: 'manual_dashboard';
  issued_by?: string;
  reason?: string;
}

export interface ApiErrorBody {
  detail?: string | { msg: string }[];
}

// ---- WebSocket messages (validated from `unknown`, see lib/guards.ts) ----
// Shapes confirmed against backend services/{ingest,alert_engine,valve_control}.py.
export interface ReadingMessage {
  type: 'reading';
  flat_id: Id;
  flow_rate: number;
  timestamp: string;
  volume?: number;
}
export interface AlertMessage {
  type: 'alert';
  event?: string;
  alert: Alert;
}
/** {type:'valve', event:'command', command:{...}} - note: NO top-level flat_id. */
export interface ValveCommandMessage {
  type: 'valve';
  event: 'command';
  command: ValveCommandRecord;
}
/** Device ack: {type:'valve', event:'state', flat_id, state}. */
export interface ValveStateMessage {
  type: 'valve';
  event: 'state';
  flat_id: Id;
  state: ValveState;
}
export type ValveMessage = ValveCommandMessage | ValveStateMessage;
export interface AnomalyMessage {
  type: 'anomaly';
  flat_id: Id;
  anomaly_id: Id;
  anomaly_type: string;
  severity: number;
}
export interface SensorMessage {
  type: 'sensor';
  flat_id: Id;
  status: SensorStatusName;
}
export type WsMessage =
  | ReadingMessage
  | AlertMessage
  | ValveMessage
  | AnomalyMessage
  | SensorMessage;

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'disconnected';
