'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { WS_FORBIDDEN, WS_IDLE, WS_TOO_MANY, WS_UNAUTHORIZED, wsCloseMessage } from '@/lib/wsAuth';
import type { ConnectionNotice, ConnectionState } from '@/lib/types';

export const BACKOFF_BASE_MS = 1000;
export const BACKOFF_MAX_MS = 30_000;
/** For 4429 / 4408 we never hammer the server: start the backoff at this attempt number. */
export const POLITE_MIN_ATTEMPT = 2;

/** Exponential backoff capped at 30s, with up to 25% downward jitter. */
export function backoffDelay(attempt: number, random: number = Math.random()): number {
  const exp = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** attempt);
  return Math.round(exp * (0.75 + 0.25 * random));
}

export interface UseWebSocketOptions {
  /** Resolves the URL for ONE connection attempt (fetches a fresh single-use ticket). */
  getUrl: (() => Promise<string>) | null;
  onMessage: (data: unknown) => void;
  maxRetries?: number;
  /**
   * Called on close 4401. Should refresh the tokens and resolve true on success (the hook then
   * reconnects once), or end the session and resolve false.
   */
  onAuthExpired?: () => Promise<boolean>;
  /** Called when the session is definitely over (second 4401, or refresh failed). */
  onUnauthorized?: () => void;
}

function isServerPing(data: unknown): boolean {
  if (typeof data !== 'string' || data.length > 64 || !data.includes('ping')) return false;
  try {
    const v: unknown = JSON.parse(data);
    return typeof v === 'object' && v !== null && (v as { type?: unknown }).type === 'ping';
  } catch {
    return false;
  }
}

export function useWebSocket({
  getUrl,
  onMessage,
  maxRetries = 8,
  onAuthExpired,
  onUnauthorized,
}: UseWebSocketOptions): {
  state: ConnectionState;
  notice: ConnectionNotice | null;
  reconnect: () => void;
} {
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;
  const getUrlRef = useRef(getUrl);
  getUrlRef.current = getUrl;
  const expiredRef = useRef(onAuthExpired);
  expiredRef.current = onAuthExpired;
  const unauthRef = useRef(onUnauthorized);
  unauthRef.current = onUnauthorized;
  const [state, setState] = useState<ConnectionState>('idle');
  const [notice, setNotice] = useState<ConnectionNotice | null>(null);
  const [nonce, setNonce] = useState(0);
  const enabled = getUrl !== null;

  useEffect(() => {
    if (!enabled) {
      setState('idle');
      return;
    }
    let disposed = false;
    let attempt = 0;
    let authRetried = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const scheduleRetry = (minAttempt = 0) => {
      if (disposed) return;
      attempt = Math.max(attempt, minAttempt);
      if (attempt >= maxRetries) {
        setState('disconnected');
        return;
      }
      const delay = backoffDelay(attempt);
      attempt += 1;
      setState('reconnecting');
      timer = setTimeout(() => void connect(), delay);
    };

    const connect = async () => {
      if (disposed) return;
      setState(attempt === 0 && !authRetried ? 'connecting' : 'reconnecting');
      let url: string;
      try {
        const resolve = getUrlRef.current;
        if (!resolve) return;
        url = await resolve(); // fresh single-use ticket for THIS attempt
      } catch {
        // ticket endpoint unreachable / rate limited (a dead session already triggered logout)
        scheduleRetry();
        return;
      }
      if (disposed) return;
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
        authRetried = false;
        setNotice(null);
        setState('open');
      };
      ws.onmessage = (ev: MessageEvent) => {
        if (isServerPing(ev.data)) {
          try {
            ws.send('ping'); // any inbound frame resets the server's idle timer
          } catch {
            /* socket closing */
          }
          return;
        }
        handlerRef.current(ev.data);
      };
      ws.onerror = () => {
        /* onclose follows; handled there */
      };
      ws.onclose = (ev?: CloseEvent) => {
        if (disposed || socket !== ws) return;
        socket = null;
        const code = ev?.code;
        const message = code === undefined ? null : wsCloseMessage(code);
        if (code !== undefined && message) setNotice({ code, message });
        if (code === WS_UNAUTHORIZED) {
          if (authRetried || !expiredRef.current) {
            setState('disconnected');
            unauthRef.current?.();
            return;
          }
          authRetried = true;
          setState('reconnecting');
          void expiredRef.current().then(
            (ok) => {
              if (disposed) return;
              if (ok) void connect();
              else {
                setState('disconnected');
                unauthRef.current?.();
              }
            },
            () => {
              if (!disposed) scheduleRetry();
            },
          );
          return;
        }
        if (code === WS_FORBIDDEN) {
          // Retrying cannot help: the account has no data scope.
          setState('disconnected');
          return;
        }
        if (code === WS_TOO_MANY || code === WS_IDLE) {
          scheduleRetry(POLITE_MIN_ATTEMPT);
          return;
        }
        scheduleRetry();
      };
    };

    void connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      if (socket) {
        socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
        socket.close();
      }
    };
  }, [enabled, maxRetries, nonce]);

  const reconnect = useCallback(() => {
    setNotice(null);
    setNonce((n) => n + 1);
  }, []);
  return { state, notice, reconnect };
}
