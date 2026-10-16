import { describe, expect, it } from 'vitest';
import { buildChartRows, mergeReadings, slotFor } from './chart';
import { permissionsFor } from './permissions';
import { baseline as fxBaseline, forecast as fxForecast } from '@/test/fixtures';

describe('chart helpers', () => {
  it('maps dates to baseline slots', () => {
    expect(slotFor(new Date('2026-01-04T13:30:00Z'), 24, 'UTC')).toBe(13);
    expect(slotFor(new Date('2026-01-04T13:30:00Z'), 48, 'UTC')).toBe(27);
  });

  it('uses Monday=0 hour-of-week in the backend timezone (matches backend baseline.hour_of_week)', () => {
    // 2026-01-05 is a Monday, 2026-01-04 a Sunday.
    expect(slotFor(new Date('2026-01-05T00:00:00Z'), 168, 'UTC')).toBe(0);
    expect(slotFor(new Date('2026-01-05T05:30:00Z'), 168, 'UTC')).toBe(5);
    expect(slotFor(new Date('2026-01-04T05:00:00Z'), 168, 'UTC')).toBe(6 * 24 + 5); // Sunday
    expect(slotFor(new Date('2026-01-04T23:59:00Z'), 168, 'UTC')).toBe(167);
    // Asia/Kolkata is UTC+5:30: Sunday 20:00Z is Monday 01:30 local => slot 1.
    expect(slotFor(new Date('2026-01-04T20:00:00Z'), 168, 'Asia/Kolkata')).toBe(1);
    // America/New_York is UTC-5 in January: Monday 02:00Z is Sunday 21:00 local => slot 6*24+21.
    expect(slotFor(new Date('2026-01-05T02:00:00Z'), 168, 'America/New_York')).toBe(6 * 24 + 21);
    // An invalid timezone falls back to UTC instead of throwing.
    expect(slotFor(new Date('2026-01-05T03:00:00Z'), 168, 'Not/AZone')).toBe(3);
  });

  it('builds sorted rows combining actual, baseline and forecast', () => {
    const baseline = fxBaseline;
    const rows = buildChartRows(
      [{ timestamp: '2026-01-05T10:00:00Z', flow_rate: 4, volume: 0 }], // Monday 10:00 UTC => slot 10
      baseline,
      { ...fxForecast, predictions: [{ hour: '2026-01-05T11:00:00Z', predicted_lpm: 5, lower: 4, upper: 6 }] },
      'UTC',
    );
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ actual: 4, median: 1, band: [0.8, 1.5] });
    expect(rows[1]).toMatchObject({ forecast: 5, forecastBand: [4, 6] });
  });

  it('merges live readings without duplicates', () => {
    const a = { timestamp: '2026-01-01T00:00:00Z', flow_rate: 1, volume: 0 };
    const merged = mergeReadings([a], [{ ...a, flow_rate: 2 }, { ...a, timestamp: '2026-01-01T00:01:00Z' }]);
    expect(merged).toHaveLength(2);
    expect(merged[0]?.flow_rate).toBe(2);
  });
});

describe('permissions', () => {
  it('gates by role', () => {
    expect(permissionsFor('admin')).toMatchObject({ controlValve: true, override: true });
    expect(permissionsFor('maintenance')).toMatchObject({ acknowledge: true, resolve: true, controlValve: false, override: false });
    expect(permissionsFor('resident')).toMatchObject({ acknowledge: false, controlValve: false });
    expect(permissionsFor(null).resolve).toBe(false);
  });
});
