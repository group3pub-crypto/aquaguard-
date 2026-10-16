import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LiveProvider, useLive } from './LiveProvider';
import { WithAuth } from '@/test/utils';
import { IDS, wsFrames } from '@/test/fixtures';
import { setToken } from '@/lib/session';
import { buildWsUrl } from '@/lib/wsAuth';

class MockWS {
  static instances: MockWS[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((e?: { code: number }) => void) | null = null;
  constructor(public url: string) {
    MockWS.instances.push(this);
  }
  close() {}
}

function Probe() {
  const live = useLive();
  return (
    <div>
      <span data-testid="valve">{live.valves[IDS.flat] ?? 'none'}</span>
      <span data-testid="cmd">{live.valveCommands[IDS.flat]?.status ?? 'none'}</span>
      <span data-testid="sensor">{live.sensors[IDS.flat] ?? 'none'}</span>
      <span data-testid="flow">{live.readings[IDS.flat]?.length ?? 0}</span>
      <span data-testid="events">{live.events.length}</span>
    </div>
  );
}

beforeEach(() => {
  MockWS.instances = [];
  vi.stubGlobal('WebSocket', MockWS);
  setToken('jwt-abc');
});

const send = (obj: unknown) => act(() => MockWS.instances[0]?.onmessage?.({ data: JSON.stringify(obj) }));

describe('LiveProvider', () => {
  it('builds the WS url in one place, token as query param', () => {
    expect(buildWsUrl('a b')).toContain('/api/v1/ws/live?token=a%20b');
    expect(buildWsUrl(null)).toBeNull();
  });

  it('handles every real frame type and ignores unknown ones', () => {
    render(
      <WithAuth role="admin">
        <LiveProvider>
          <Probe />
        </LiveProvider>
      </WithAuth>,
    );
    expect(MockWS.instances[0]?.url).toContain('token=tok');
    send(wsFrames.reading);
    expect(screen.getByTestId('flow')).toHaveTextContent('1');
    send(wsFrames.valveCommand);
    expect(screen.getByTestId('cmd')).toHaveTextContent('pending');
    expect(screen.getByTestId('valve')).toHaveTextContent('none'); // a command is not a state change
    send(wsFrames.valveState);
    expect(screen.getByTestId('valve')).toHaveTextContent('closed');
    send(wsFrames.sensor);
    expect(screen.getByTestId('sensor')).toHaveTextContent('offline');
    send(wsFrames.anomaly);
    send(wsFrames.alert);
    const before = screen.getByTestId('events').textContent;
    send({ type: 'brand_new_type', foo: 1 });
    expect(screen.getByTestId('events').textContent).toBe(before);
  });
});
