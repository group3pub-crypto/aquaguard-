'use client';
import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { WS_URL } from '@/lib/api';
import { parseWsMessage } from '@/lib/guards';
import { useWebSocket } from '@/hooks/useWebSocket';
import type { Alert, ConnectionState, ValveState } from '@/lib/types';
import { useAuth } from './AuthProvider';

export interface LiveReading {
  timestamp: string;
  flow_rate: number;
}
export interface LiveEvent {
  id: number;
  at: string;
  kind: 'alert' | 'valve' | 'info';
  text: string;
}

export interface LiveValue {
  connection: ConnectionState;
  readings: Record<string, LiveReading[]>;
  valves: Record<string, ValveState>;
  lastAlert: Alert | null;
  events: LiveEvent[];
  reconnect: () => void;
  log: (text: string) => void;
}

const MAX_POINTS = 120;
const MAX_EVENTS = 100;

const defaultValue: LiveValue = {
  connection: 'idle',
  readings: {},
  valves: {},
  lastAlert: null,
  events: [],
  reconnect: () => undefined,
  log: () => undefined,
};
const LiveContext = createContext<LiveValue>(defaultValue);

export function useLive(): LiveValue {
  return useContext(LiveContext);
}

let eventSeq = 0;

export function LiveProvider({ children }: { children: ReactNode }) {
  const { token } = useAuth();
  const [readings, setReadings] = useState<Record<string, LiveReading[]>>({});
  const [valves, setValves] = useState<Record<string, ValveState>>({});
  const [lastAlert, setLastAlert] = useState<Alert | null>(null);
  const [events, setEvents] = useState<LiveEvent[]>([]);

  const pushEvent = useCallback((kind: LiveEvent['kind'], text: string) => {
    eventSeq += 1;
    const ev: LiveEvent = { id: eventSeq, at: new Date().toISOString(), kind, text };
    setEvents((prev) => [ev, ...prev].slice(0, MAX_EVENTS));
  }, []);

  const onMessage = useCallback(
    (raw: unknown) => {
      const msg = parseWsMessage(raw);
      if (!msg) return; // malformed payloads are dropped
      switch (msg.type) {
        case 'reading': {
          const key = String(msg.flat_id);
          setReadings((prev) => ({
            ...prev,
            [key]: [...(prev[key] ?? []), { timestamp: msg.timestamp, flow_rate: msg.flow_rate }].slice(
              -MAX_POINTS,
            ),
          }));
          break;
        }
        case 'alert':
          setLastAlert(msg.alert);
          pushEvent('alert', `Alert ${msg.alert.id} on flat ${msg.alert.flat_id}: ${msg.alert.state} - ${msg.alert.message}`);
          break;
        case 'valve':
          setValves((prev) => ({ ...prev, [String(msg.flat_id)]: msg.state }));
          pushEvent('valve', `Valve on flat ${msg.flat_id} is now ${msg.state}`);
          break;
      }
    },
    [pushEvent],
  );

  const url = token ? `${WS_URL}/api/v1/ws/live?token=${encodeURIComponent(token)}` : null;
  const { state, reconnect } = useWebSocket({ url, onMessage });

  const value = useMemo<LiveValue>(
    () => ({
      connection: state,
      readings,
      valves,
      lastAlert,
      events,
      reconnect,
      log: (text: string) => pushEvent('info', text),
    }),
    [state, readings, valves, lastAlert, events, reconnect, pushEvent],
  );
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}
