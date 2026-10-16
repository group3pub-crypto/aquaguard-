'use client';
import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { parseWsMessage } from '@/lib/guards';
import { buildWsUrl } from '@/lib/wsAuth';
import { useWebSocket } from '@/hooks/useWebSocket';
import type {
  Alert,
  AnomalyMessage,
  ConnectionState,
  SensorStatusName,
  ValveCommandRecord,
  ValveState,
} from '@/lib/types';
import { useAuth } from './AuthProvider';

export interface LiveReading {
  timestamp: string;
  flow_rate: number;
}
export interface LiveEvent {
  id: number;
  at: string;
  kind: 'alert' | 'valve' | 'anomaly' | 'sensor' | 'info';
  text: string;
}

export interface LiveValue {
  connection: ConnectionState;
  readings: Record<string, LiveReading[]>;
  valves: Record<string, ValveState>;
  /** Latest valve command per flat (from {type:'valve',event:'command'}). */
  valveCommands: Record<string, ValveCommandRecord>;
  sensors: Record<string, SensorStatusName>;
  lastAlert: Alert | null;
  lastAnomaly: AnomalyMessage | null;
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
  valveCommands: {},
  sensors: {},
  lastAlert: null,
  lastAnomaly: null,
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
  const { token, expireSession } = useAuth();
  const [readings, setReadings] = useState<Record<string, LiveReading[]>>({});
  const [valves, setValves] = useState<Record<string, ValveState>>({});
  const [valveCommands, setValveCommands] = useState<Record<string, ValveCommandRecord>>({});
  const [sensors, setSensors] = useState<Record<string, SensorStatusName>>({});
  const [lastAlert, setLastAlert] = useState<Alert | null>(null);
  const [lastAnomaly, setLastAnomaly] = useState<AnomalyMessage | null>(null);
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
          pushEvent(
            'alert',
            `Alert on flat ${msg.alert.flat_id}: ${msg.alert.state}${msg.alert.message ? ` - ${msg.alert.message}` : ''}`,
          );
          break;
        case 'valve':
          if (msg.event === 'command') {
            const c = msg.command;
            setValveCommands((prev) => ({ ...prev, [String(c.flat_id)]: c }));
            pushEvent('valve', `Valve ${c.command} command ${c.status} for flat ${c.flat_id}`);
          } else {
            setValves((prev) => ({ ...prev, [String(msg.flat_id)]: msg.state }));
            pushEvent('valve', `Valve on flat ${msg.flat_id} is now ${msg.state}`);
          }
          break;
        case 'anomaly':
          setLastAnomaly(msg);
          pushEvent('anomaly', `Anomaly (${msg.anomaly_type}) detected on flat ${msg.flat_id}`);
          break;
        case 'sensor':
          setSensors((prev) => ({ ...prev, [String(msg.flat_id)]: msg.status }));
          pushEvent('sensor', `Sensor on flat ${msg.flat_id} is ${msg.status}`);
          break;
        default:
          break; // unknown types are rejected by parseWsMessage and never reach here
      }
    },
    [pushEvent],
  );

  const url = buildWsUrl(token);
  const { state, reconnect } = useWebSocket({ url, onMessage, onUnauthorized: expireSession });

  const value = useMemo<LiveValue>(
    () => ({
      connection: state,
      readings,
      valves,
      valveCommands,
      sensors,
      lastAlert,
      lastAnomaly,
      events,
      reconnect,
      log: (text: string) => pushEvent('info', text),
    }),
    [state, readings, valves, valveCommands, sensors, lastAlert, lastAnomaly, events, reconnect, pushEvent],
  );
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}
