import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LiveProvider, useLive } from './LiveProvider';
import { ConnectionBanner } from '@/components/ConnectionBanner';
import { WithAuth, mockFetch } from '@/test/utils';
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
  mockFetch((url) =>
    url.pathname.endsWith('/auth/ws-ticket') ? { body: { ticket: 'tkt-123', expires_in: 30 } } : undefined,
  );
});

const send = (obj: unknown) => act(() => MockWS.instances[0]?.onmessage?.({ data: JSON.stringify(obj) }));
const flush = () => act(() => Promise.resolve());

describe('LiveProvider', () => {
  it('builds the WS url in one place from a POST /auth/ws-ticket ticket, never a JWT', async () => {
    const url = await buildWsUrl();
    expect(url).toContain('/api/v1/ws/live?ticket=tkt-123');
    expect(url).not.toContain('token=');
  });

  it('handles every real frame type and ignores unknown ones', async () => {
    render(
      <WithAuth role="admin">
        <LiveProvider>
          <Probe />
        </LiveProvider>
      </WithAuth>,
    );
    await flush();
    expect(MockWS.instances[0]?.url).toContain('ticket=tkt-123');
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

  it('shows a banner with the reason on 4403 and does not reconnect', async () => {
    render(
      <WithAuth role="admin">
        <LiveProvider>
          <ConnectionBanner />
        </LiveProvider>
      </WithAuth>,
    );
    await flush();
    act(() => MockWS.instances[0]?.onclose?.({ code: 4403 }));
    expect(screen.getByRole('alert')).toHaveTextContent(/no live-feed access/i);
    expect(screen.queryByRole('button', { name: 'Reconnect' })).not.toBeInTheDocument();
    expect(MockWS.instances).toHaveLength(1);
  });

  it('does not connect at all for an unbound account', async () => {
    render(
      <WithAuth role="resident" user={{ flat_id: null }}>
        <LiveProvider>
          <Probe />
        </LiveProvider>
      </WithAuth>,
    );
    await flush();
    expect(MockWS.instances).toHaveLength(0);
  });

  it('requests a fresh ticket for every reconnect', async () => {
    let n = 0;
    const { calls } = mockFetch((url) =>
      url.pathname.endsWith('/auth/ws-ticket') ? { body: { ticket: `t${++n}`, expires_in: 30 } } : undefined,
    );
    vi.useFakeTimers();
    render(
      <WithAuth role="admin">
        <LiveProvider>
          <Probe />
        </LiveProvider>
      </WithAuth>,
    );
    await flush();
    act(() => MockWS.instances[0]?.onclose?.({ code: 1006 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(calls.filter((c) => c.url.pathname.endsWith('/ws-ticket')).map((c) => c.init.method)).toEqual(['POST', 'POST']);
    expect(MockWS.instances.map((w) => w.url.split('ticket=')[1])).toEqual(['t1', 't2']);
  });
});
