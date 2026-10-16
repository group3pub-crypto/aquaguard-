// Strict types for the AquaGuard backend contract (REST /api/v1 + WebSocket).

export type Id = string | number;
export type Role = 'admin' | 'maintenance' | 'resident';
export type ValveState = 'open' | 'closed' | 'unknown';
export type FaultType = 'slow_leak' | 'burst' | 'running_toilet' | 'dripping_tap' | 'overuse';

export const FAULT_TYPES: readonly FaultType[] = [
  'slow_leak',
  'burst',
  'running_toilet',
  'dripping_tap',
  'overuse',
];

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
}

export interface Society {
  id: Id;
  name: string;
  slug: string;
  total_flats: number;
}
export interface SocietyStats {
  total_consumption: number;
  losses: number;
  water_saved: number;
  active_alerts: number;
}

export interface Flat {
  id: Id;
  unit_number: string;
  floor: number;
  residents: number;
  valve_state: ValveState;
  baseline_status: string;
  sensor_status: string;
  current_flow: number;
}
export interface Reading {
  timestamp: string;
  flow_rate: number;
  volume: number;
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
  version: string | number;
  slots: BaselineSlot[];
  trained_from: string;
  trained_to: string;
  status: string;
}
export interface ForecastPoint {
  hour: string;
  predicted_lpm: number;
  lower: number;
  upper: number;
}
export interface Forecast {
  generated_at: string;
  predictions: ForecastPoint[];
  model_version: string | number;
  mape: number | null;
}

export interface ExplanationReason {
  feature: string;
  text: string;
}
export interface Explanation {
  reasons: ExplanationReason[];
  generated_by: string;
}
export interface Anomaly {
  id: Id;
  flat_id: Id;
  anomaly_type: string;
  confidence: number;
  severity: string | number;
  detected_at: string;
  est_loss_lpm: number;
  resolved_at: string | null;
  explanation: Explanation | null;
}

export type AlertState = 'active' | 'acknowledged' | 'snoozed' | 'overridden' | 'resolved';
export interface Alert {
  id: Id;
  flat_id: Id;
  anomaly_id: Id | null;
  state: AlertState | string;
  tier: string | number;
  severity_score: number;
  message: string;
  message_source: string;
  created_at: string;
  updated_at: string;
}
export interface AlertFilter {
  flat_id?: Id;
  state?: string;
}

export interface AcknowledgeBody {
  acknowledged_by: string;
}
export interface SnoozeBody {
  duration_minutes: number;
  snoozed_by: string;
}
export interface OverrideBody {
  reason: string;
  overridden_by: string;
}
export interface ResolveBody {
  resolved_by: string;
}

export interface ValveInfo {
  flat_id?: Id;
  state?: ValveState | string;
  valve_state?: ValveState | string;
  updated_at?: string;
}
export interface ValveCommand {
  source: 'manual_dashboard';
  issued_by: string;
  reason?: string;
}

export interface InjectFaultBody {
  flat_id: Id;
  fault_type: FaultType;
}
export interface DemoStatus {
  active_faults: { flat_id: Id; fault_type: string }[];
  speed: number;
  simulator_running: boolean;
}

export interface ApiErrorBody {
  detail?: string | { msg: string }[];
}

// ---- WebSocket messages (validated from `unknown`, see lib/guards.ts) ----
export interface ReadingMessage {
  type: 'reading';
  flat_id: Id;
  flow_rate: number;
  timestamp: string;
}
export interface AlertMessage {
  type: 'alert';
  alert: Alert;
}
export interface ValveMessage {
  type: 'valve';
  flat_id: Id;
  state: ValveState;
}
export type WsMessage = ReadingMessage | AlertMessage | ValveMessage;

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'disconnected';
