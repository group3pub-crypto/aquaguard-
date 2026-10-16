'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { WS_UNAUTHORIZED } from '@/lib/wsAuth';
import type { ConnectionState } from '@/lib/types';

export const BACKOFF_BASE_MS = 1000;
export const BACKOFF_MAX_MS = 30_000;

/** Exponential backoff capped at 30s, with up to 25% downward jitter. */
export function backoffDelay(attempt: number, random: number = Math.random()): number {
  const exp = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** attempt);
  return Math.round(exp * (0.75 + 0.25 * random));
}

export interface UseWebSocketOptions {
  /** null/empty url pauses the connection (e.g. no token yet). */
  url: string | null;
  onMessage: (data: unknown) => void;
  maxRetries?: number;
  /** Called when the server closes with 4401 (bad/expired token). No retry is attempted. */
  onUnauthorized?: () => void;
}

export function useWebSocket({ url, onMessage, maxRetries = 8, onUnauthorized }: UseWebSocketOptions): {
  state: ConnectionState;
  reconnect: () => void;
} {
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;
  const unauthRef = useRef(onUnauthorized);
  unauthRef.current = onUnauthorized;
  const [state, setState] = useState<ConnectionState>('idle');
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!url) {
      setState('idle');
      return;
    }
    let disposed = false;
    let attempt = 0;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (disposed) return;
      setState(attempt === 0 ? 'connecting' : 'reconnecting');
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        scheduleRetry();
        return;
      }
      socket = ws;
      ws.onopen = () => {
        if (disposed) return;
        attempt = 0;
        setState('open');
      };
      ws.onmessage = (ev: MessageEvent) => {
        handlerRef.current(ev.data);
      };
      ws.onerror = () => {
        /* onclose follows; handled there */
      };
      ws.onclose = (ev?: CloseEvent) => {
        if (disposed || socket !== ws) return;
        socket = null;
        if (ev?.code === WS_UNAUTHORIZED) {
          // Retrying with the same stale token can never succeed.
          setState('disconnected');
          unauthRef.current?.();
          return;
        }
        scheduleRetry();
      };
    };

    const scheduleRetry = () => {
      if (disposed) return;
      if (attempt >= maxRetries) {
        setState('disconnected');
        return;
      }
      const delay = backoffDelay(attempt);
      attempt += 1;
      setState('reconnecting');
      timer = setTimeout(connect, delay);
    };

    connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      if (socket) {
        socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
        socket.close();
      }
    };
  }, [url, maxRetries, nonce]);

  const reconnect = useCallback(() => setNonce((n) => n + 1), []);
  return { state, reconnect };
}
