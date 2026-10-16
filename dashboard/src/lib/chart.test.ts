import { describe, expect, it } from 'vitest';
import { buildChartRows, mergeReadings, slotFor } from './chart';
import { permissionsFor } from './permissions';

describe('chart helpers', () => {
  it('maps dates to baseline slots', () => {
    expect(slotFor(new Date(2026, 0, 4, 13, 30), 24)).toBe(13);
    expect(slotFor(new Date(2026, 0, 4, 13, 30), 48)).toBe(27);
    expect(slotFor(new Date(2026, 0, 4, 5, 0), 168)).toBe(5); // Sunday
  });

  it('builds sorted rows combining actual, baseline and forecast', () => {
    const baseline = {
      version: 1,
      status: 'ready',
      trained_from: '',
      trained_to: '',
      slots: Array.from({ length: 24 }, (_, i) => ({ slot: i, median: i, mad: 0, p25: i - 1, p75: i + 1, p95: i + 2, sample_count: 5 })),
    };
    const rows = buildChartRows(
      [{ timestamp: new Date(2026, 0, 1, 10, 0).toISOString(), flow_rate: 4, volume: 0 }],
      baseline,
      { generated_at: '', model_version: 1, mape: 3, predictions: [{ hour: new Date(2026, 0, 1, 11, 0).toISOString(), predicted_lpm: 5, lower: 4, upper: 6 }] },
    );
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ actual: 4, median: 10, band: [9, 12] });
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
    expect(permissionsFor('admin')).toMatchObject({ controlValve: true, override: true, demo: true });
    expect(permissionsFor('maintenance')).toMatchObject({ acknowledge: true, resolve: true, controlValve: false, override: false });
    expect(permissionsFor('resident')).toMatchObject({ acknowledge: false, controlValve: false, demo: false });
    expect(permissionsFor(null).resolve).toBe(false);
  });
});
