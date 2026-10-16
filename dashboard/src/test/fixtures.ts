// Backend-shaped fixtures. Every shape here is validated against the committed OpenAPI
// snapshot in lib/contract.test.ts, so these cannot drift into invented fields.
import type {
  Alert,
  Anomaly,
  Baseline,
  Flat,
  FlatDetail,
  Forecast,
  Reading,
  Society,
  SocietyStats,
  ValveCommandRecord,
  ValveInfo,
} from '@/lib/types';

export const IDS = {
  society: '0b1c3a52-6d1f-4f5e-9a39-0d1b5a2f7c11',
  flat: '3f8e4a6c-2b6d-4c2e-8f55-7a1d9e0b4c22',
  flat2: '5a9d1e7b-4c3f-4d6a-9b66-8b2e0f1c5d33',
  alert: '7c2f6b8d-5d4a-4e7b-8c77-9c3f1a2d6e44',
  anomaly: '9e4a8d1f-7f6c-4a9d-8e88-1d5a3c4f8b55',
  cmd: 'b1c5e2a3-9a8d-4b1e-9f99-3e7c5d6a0f66',
} as const;

export const society: Society = {
  id: IDS.society,
  name: 'Green Meadows',
  slug: 'green-meadows',
  address: null,
  total_flats: 2,
  created_at: '2026-01-01T00:00:00Z',
};

export const societyStats: SocietyStats = {
  society_id: IDS.society,
  flats: 2,
  active_alerts: 1,
  volume_24h_l: 1234.5,
  sensors_offline: 0,
};

/** GET /flats item: no current_flow, no sensor_status. */
export const flat: Flat = {
  id: IDS.flat,
  society_id: IDS.society,
  unit_number: 'A-301',
  floor: 3,
  residents: 2,
  sensor_id: 'esp32-a301',
  valve_state: 'open',
  baseline_status: 'ready',
};

/** GET /flats/{id}: sensor_status is an OBJECT. */
export const flatDetail: FlatDetail = {
  ...flat,
  current_flow: 1.5,
  last_reading_at: '2026-01-01T10:00:00Z',
  sensor_status: { status: 'online', last_seen: '2026-01-01T10:00:00Z', missed_readings: 0 },
};

export const reading: Reading = {
  id: 1,
  flat_id: IDS.flat,
  timestamp: '2026-01-01T10:00:00Z',
  flow_rate: 2.5,
  volume: 100.2,
  pressure: null,
  temperature: null,
  battery_pct: 90,
  rssi: -60,
};

export const baseline: Baseline = {
  id: 'c2d6f3b4-0b9e-4c2f-8faa-4f8d6e7b1a77',
  flat_id: IDS.flat,
  version: 3,
  slots: Array.from({ length: 168 }, (_, i) => ({
    slot: i,
    median: i / 10,
    mad: 0.1,
    p25: i / 10 - 0.2,
    p75: i / 10 + 0.2,
    p95: i / 10 + 0.5,
    sample_count: 4,
  })),
  trained_from: '2025-12-01T00:00:00Z',
  trained_to: '2025-12-31T00:00:00Z',
  sample_days: 30,
  is_active: true,
};

export const forecast: Forecast = {
  id: 'd3e7a4c5-1c0f-4d3a-9abb-5a9e7f8c2b88',
  flat_id: IDS.flat,
  generated_at: '2026-01-01T10:00:00Z',
  horizon_hours: 24,
  predictions: [{ hour: '2026-01-01T11:00:00+00:00', predicted_lpm: 1.2, lower: 0.8, upper: 1.6 }],
  model_version: 'baseline-median',
  mape: null,
};

export const anomaly: Anomaly = {
  id: IDS.anomaly,
  flat_id: IDS.flat,
  detected_at: '2026-01-01T10:00:00Z',
  anomaly_type: 'leak',
  confidence: 0.87,
  severity: 0.62,
  features: { night_min: 0.4 },
  shap_values: [{ feature: 'night_min', value: 0.4, shap: 0.31 }],
  explanation: {
    generated_by: 'shap_tree_explainer',
    classifier: 'ml',
    top_factors: [{ feature: 'night_min', value: 0.4, shap: 0.31 }],
    class_probabilities: { leak: 0.87, unknown: 0.13 },
    iso_score: 0.55,
  },
  baseline_version: 3,
  window_start: '2026-01-01T09:00:00Z',
  window_end: '2026-01-01T10:00:00Z',
  est_loss_lpm: 0.4,
  resolved_at: null,
};

/** Rule-based explanation: top_factors carry `note`, not `shap`. */
export const ruleAnomaly: Anomaly = {
  ...anomaly,
  id: '8d3b7c0e-6e5b-4b8c-9d77-0c4f2b3e7a99',
  shap_values: null,
  explanation: {
    generated_by: 'rule_heuristic',
    classifier: 'rules',
    top_factors: [{ feature: 'night_min', value: 0.4, note: 'constant night flow' }],
    class_probabilities: null,
    iso_score: null,
  },
};

export const alert: Alert = {
  id: IDS.alert,
  flat_id: IDS.flat,
  anomaly_id: IDS.anomaly,
  state: 'alert',
  tier: 2,
  severity_score: 0.82,
  message: 'Possible burst pipe',
  message_source: 'template',
  escalated_at: null,
  snoozed_until: null,
  acknowledged_by: null,
  acknowledged_at: null,
  resolved_at: null,
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
};

export const valveCommand: ValveCommandRecord = {
  id: IDS.cmd,
  flat_id: IDS.flat,
  alert_id: null,
  command: 'close',
  source: 'manual_dashboard',
  issued_by: 'alice',
  issued_at: '2026-01-01T10:00:00Z',
  confirmed_at: null,
  status: 'pending',
};

export const valveInfo: ValveInfo = {
  flat_id: IDS.flat,
  valve_state: 'open',
  pending: [valveCommand],
  recent: [valveCommand],
};

/** Real WebSocket frames, as broadcast by the backend. */
export const wsFrames = {
  reading: { type: 'reading', ...reading },
  alert: { type: 'alert', event: 'acknowledge', alert },
  valveCommand: { type: 'valve', event: 'command', command: valveCommand },
  valveState: { type: 'valve', event: 'state', flat_id: IDS.flat, state: 'closed' },
  anomaly: { type: 'anomaly', flat_id: IDS.flat, anomaly_id: IDS.anomaly, anomaly_type: 'leak', severity: 0.62 },
  sensor: { type: 'sensor', flat_id: IDS.flat, status: 'offline' },
} as const;
