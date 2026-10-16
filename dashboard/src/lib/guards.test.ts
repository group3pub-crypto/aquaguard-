import { describe, expect, it } from 'vitest';
import { parseWsMessage } from './guards';
import { IDS, wsFrames } from '@/test/fixtures';

describe('parseWsMessage with real backend frames', () => {
  it('accepts a reading (object or JSON string), including the extra ReadingOut fields', () => {
    const msg = parseWsMessage(JSON.stringify(wsFrames.reading));
    expect(msg).toMatchObject({ type: 'reading', flat_id: IDS.flat, flow_rate: 2.5, volume: 100.2 });
    expect(parseWsMessage(wsFrames.reading)).toEqual(msg);
  });

  it('accepts alert frames {type, event, alert}', () => {
    const msg = parseWsMessage(wsFrames.alert);
    expect(msg?.type === 'alert' && msg.alert.state).toBe('alert');
    expect(msg?.type === 'alert' && msg.event).toBe('acknowledge');
  });

  it('accepts valve COMMAND frames, which have no top-level flat_id', () => {
    const msg = parseWsMessage(wsFrames.valveCommand);
    expect('flat_id' in wsFrames.valveCommand).toBe(false);
    expect(msg).toMatchObject({ type: 'valve', event: 'command', command: { flat_id: IDS.flat, command: 'close', status: 'pending' } });
  });

  it('accepts valve STATE acks', () => {
    expect(parseWsMessage(wsFrames.valveState)).toEqual({ type: 'valve', event: 'state', flat_id: IDS.flat, state: 'closed' });
  });

  it('accepts anomaly and sensor frames', () => {
    expect(parseWsMessage(wsFrames.anomaly)).toMatchObject({ type: 'anomaly', anomaly_type: 'leak', severity: 0.62 });
    expect(parseWsMessage(wsFrames.sensor)).toEqual({ type: 'sensor', flat_id: IDS.flat, status: 'offline' });
  });

  it('safely ignores unknown types and malformed payloads', () => {
    expect(parseWsMessage({ type: 'something_new', x: 1 })).toBeNull();
    expect(parseWsMessage('pong')).toBeNull();
    expect(parseWsMessage('not json')).toBeNull();
    expect(parseWsMessage(null)).toBeNull();
    expect(parseWsMessage([])).toBeNull();
    expect(parseWsMessage({ type: 'reading', flat_id: IDS.flat, flow_rate: 'fast', timestamp: 'x' })).toBeNull();
    expect(parseWsMessage({ type: 'reading', flow_rate: 1, timestamp: '2026-01-01T00:00:00Z' })).toBeNull();
    expect(parseWsMessage({ type: 'alert', alert: { id: 1 } })).toBeNull();
    expect(parseWsMessage({ type: 'valve' })).toBeNull();
    expect(parseWsMessage({ type: 'valve', event: 'command', command: { nope: true } })).toBeNull();
    expect(parseWsMessage({ type: 'sensor', flat_id: IDS.flat, status: 'weird' })).toBeNull();
  });
});
