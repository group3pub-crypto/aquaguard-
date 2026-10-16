import type { Baseline, BaselineSlot, Forecast, Reading } from './types';

export interface ChartRow {
  t: number;
  actual?: number;
  median?: number;
  band?: [number, number];
  forecast?: number;
  forecastBand?: [number, number];
}

/** Maps a timestamp to a baseline slot: hour-of-week (168), else proportional to minute-of-day. */
export function slotFor(date: Date, slotCount: number): number {
  if (slotCount <= 0) return 0;
  if (slotCount === 168) return date.getDay() * 24 + date.getHours();
  const minutes = date.getHours() * 60 + date.getMinutes();
  return Math.min(slotCount - 1, Math.floor(minutes / (1440 / slotCount)));
}

function findSlot(baseline: Baseline | null, t: number): BaselineSlot | undefined {
  if (!baseline || baseline.slots.length === 0) return undefined;
  const idx = slotFor(new Date(t), baseline.slots.length);
  return baseline.slots.find((s) => s.slot === idx) ?? baseline.slots[idx];
}

export function buildChartRows(
  readings: Reading[],
  baseline: Baseline | null,
  forecast: Forecast | null,
): ChartRow[] {
  const rows = new Map<number, ChartRow>();
  const upsert = (t: number): ChartRow => {
    const existing = rows.get(t);
    if (existing) return existing;
    const row: ChartRow = { t };
    const slot = findSlot(baseline, t);
    if (slot) {
      row.median = slot.median;
      row.band = [slot.p25, slot.p95];
    }
    rows.set(t, row);
    return row;
  };
  for (const r of readings) {
    const t = Date.parse(r.timestamp);
    if (!Number.isNaN(t)) upsert(t).actual = r.flow_rate;
  }
  for (const p of forecast?.predictions ?? []) {
    const t = Date.parse(p.hour);
    if (Number.isNaN(t)) continue;
    const row = upsert(t);
    row.forecast = p.predicted_lpm;
    row.forecastBand = [p.lower, p.upper];
  }
  return [...rows.values()].sort((a, b) => a.t - b.t);
}

/** Merge live readings into a series, de-duplicated by timestamp and capped. */
export function mergeReadings(base: Reading[], live: Reading[], cap = 600): Reading[] {
  const map = new Map<string, Reading>();
  for (const r of base) map.set(r.timestamp, r);
  for (const r of live) map.set(r.timestamp, r);
  return [...map.values()]
    .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))
    .slice(-cap);
}
