import { describe, expect, it } from 'vitest';
import { parseWsMessage } from './guards';

describe('parseWsMessage', () => {
  it('accepts a valid reading (object or JSON string)', () => {
    const msg = { type: 'reading', flat_id: 1, flow_rate: 2.5, timestamp: '2026-01-01T00:00:00Z' };
    expect(parseWsMessage(msg)).toEqual(msg);
    expect(parseWsMessage(JSON.stringify(msg))).toEqual(msg);
  });

  it('rejects malformed payloads', () => {
    expect(parseWsMessage('not json')).toBeNull();
    expect(parseWsMessage(null)).toBeNull();
    expect(parseWsMessage([])).toBeNull();
    expect(parseWsMessage({ type: 'reading', flat_id: 1, flow_rate: 'fast', timestamp: 'x' })).toBeNull();
    expect(parseWsMessage({ type: 'reading', flow_rate: 1, timestamp: '2026-01-01T00:00:00Z' })).toBeNull();
    expect(parseWsMessage({ type: 'reading', flat_id: 1, flow_rate: NaN, timestamp: '2026-01-01T00:00:00Z' })).toBeNull();
    expect(parseWsMessage({ type: 'unknown' })).toBeNull();
    expect(parseWsMessage({ type: 'alert', alert: { id: 1 } })).toBeNull();
    expect(parseWsMessage({ type: 'valve' })).toBeNull();
  });

  it('parses alert (nested or inline) and valve messages', () => {
    const alert = { id: 1, flat_id: 2, state: 'active', tier: 2, message: 'm' };
    const nested = parseWsMessage({ type: 'alert', alert });
    expect(nested?.type).toBe('alert');
    const inline = parseWsMessage({ type: 'alert', ...alert });
    expect(inline?.type === 'alert' && inline.alert.severity_score).toBe(0);
    expect(parseWsMessage({ type: 'valve', flat_id: 2, state: 'CLOSED' })).toEqual({ type: 'valve', flat_id: 2, state: 'closed' });
  });
});
