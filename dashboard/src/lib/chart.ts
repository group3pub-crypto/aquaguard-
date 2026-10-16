import { getConfig } from './config';
import type { Baseline, BaselineSlot, Forecast, Reading } from './types';

export interface ChartRow {
  t: number;
  actual?: number;
  median?: number;
  band?: [number, number];
  forecast?: number;
  forecastBand?: [number, number];
}

export interface TzParts {
  /** Monday = 0 ... Sunday = 6, matching the backend's datetime.weekday(). */
  weekday: number;
  hour: number;
  minute: number;
}

const WEEKDAYS: Record<string, number> = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
const fmtCache = new Map<string, Intl.DateTimeFormat>();

function formatterFor(tz: string): Intl.DateTimeFormat {
  const cached = fmtCache.get(tz);
  if (cached) return cached;
  let f: Intl.DateTimeFormat;
  try {
    f = new Intl.DateTimeFormat('en-US', { timeZone: tz, weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  } catch {
    f = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  }
  fmtCache.set(tz, f);
  return f;
}

/** Wall-clock parts of `date` in the backend's timezone (not the browser's). */
export function partsInTz(date: Date, tz: string): TzParts {
  const parts = formatterFor(tz).formatToParts(date);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
  return {
    weekday: WEEKDAYS[get('weekday')] ?? 0,
    hour: Number(get('hour')) % 24,
    minute: Number(get('minute')),
  };
}

/**
 * Maps a timestamp to a baseline slot. Backend (services/baseline.py hour_of_week):
 * slot = weekday()*24 + hour, Monday = 0, in settings.timezone (NEXT_PUBLIC_BACKEND_TZ here).
 */
export function slotFor(date: Date, slotCount: number, tz: string = getConfig().timezone): number {
  if (slotCount <= 0) return 0;
  const p = partsInTz(date, tz);
  if (slotCount === 168) return p.weekday * 24 + p.hour;
  const minutes = p.hour * 60 + p.minute;
  return Math.min(slotCount - 1, Math.floor(minutes / (1440 / slotCount)));
}

function findSlot(baseline: Baseline | null, t: number, tz: string): BaselineSlot | undefined {
  if (!baseline?.slots?.length) return undefined;
  const idx = slotFor(new Date(t), baseline.slots.length, tz);
  return baseline.slots.find((s) => s.slot === idx) ?? baseline.slots[idx];
}

export function buildChartRows(
  readings: Reading[],
  baseline: Baseline | null,
  forecast: Forecast | null,
  tz: string = getConfig().timezone,
): ChartRow[] {
  const rows = new Map<number, ChartRow>();
  const upsert = (t: number): ChartRow => {
    const existing = rows.get(t);
    if (existing) return existing;
    const row: ChartRow = { t };
    const slot = findSlot(baseline, t, tz);
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
