import { clearToken, getToken, setToken } from './session';
import type {
  AcknowledgeBody,
  Alert,
  AlertFilter,
  Anomaly,
  ApiErrorBody,
  Baseline,
  DemoStatus,
  Flat,
  Forecast,
  Id,
  InjectFaultBody,
  LoginRequest,
  LoginResponse,
  OverrideBody,
  Reading,
  RegisterRequest,
  ResolveBody,
  Society,
  SocietyStats,
  SnoozeBody,
  User,
  ValveCommand,
  ValveInfo,
} from './types';

const DEFAULT_API = 'http://localhost:8000';
export const API_URL = (process.env.NEXT_PUBLIC_API_URL || DEFAULT_API).replace(/\/+$/, '');
export const WS_URL = (process.env.NEXT_PUBLIC_WS_URL || API_URL.replace(/^http/, 'ws')).replace(
  /\/+$/,
  '',
);
export const API_BASE = `${API_URL}/api/v1`;
export const DEFAULT_TIMEOUT_MS = 10_000;

export type ApiErrorKind = 'http' | 'network' | 'timeout' | 'aborted';

export class ApiError extends Error {
  readonly status: number;
  readonly kind: ApiErrorKind;
  constructor(message: string, status: number, kind: ApiErrorKind = 'http') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.kind = kind;
  }
}

let unauthorizedHandler: (() => void) | null = null;
/** Registered by AuthProvider: invoked after a 401 on an authenticated request. */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  query?: Query;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** Set false for endpoints that must not send a token or trigger the 401 flow (login). */
  auth?: boolean;
}

function buildUrl(path: string, query?: Query): string {
  const qs = new URLSearchParams();
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
    }
  }
  const s = qs.toString();
  return `${API_BASE}${path}${s ? `?${s}` : ''}`;
}

export function messageFromBody(body: unknown, fallback: string): string {
  if (typeof body === 'object' && body !== null && 'detail' in body) {
    const detail = (body as ApiErrorBody).detail;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail)) {
      const msgs = detail
        .map((d) => (typeof d === 'object' && d !== null && typeof d.msg === 'string' ? d.msg : ''))
        .filter(Boolean);
      if (msgs.length) return msgs.join('; ');
    }
  }
  return fallback;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, timeoutMs = DEFAULT_TIMEOUT_MS, signal, auth = true } = opts;
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onExternalAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', onExternalAbort, { once: true });
  }

  const headers: Record<string, string> = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (auth) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  try {
    let res: Response;
    try {
      res = await fetch(buildUrl(path, query), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        cache: 'no-store',
      });
    } catch (err) {
      if (timedOut) throw new ApiError('Request timed out', 0, 'timeout');
      if (controller.signal.aborted) throw new ApiError('Request aborted', 0, 'aborted');
      throw new ApiError(err instanceof Error ? err.message : 'Network error', 0, 'network');
    }

    let payload: unknown = null;
    const text = await res.text().catch(() => '');
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = null;
      }
    }

    if (!res.ok) {
      if (res.status === 401 && auth) {
        clearToken();
        unauthorizedHandler?.();
      }
      throw new ApiError(
        messageFromBody(payload, res.statusText || `Request failed (${res.status})`),
        res.status,
      );
    }
    return payload as T;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onExternalAbort);
  }
}

const enc = (id: Id) => encodeURIComponent(String(id));
type Sig = { signal?: AbortSignal };

export const api = {
  async login(body: LoginRequest): Promise<LoginResponse> {
    const res = await request<LoginResponse>('/auth/login', { method: 'POST', body, auth: false });
    setToken(res.access_token);
    return res;
  },
  register: (body: RegisterRequest) =>
    request<unknown>('/auth/register', { method: 'POST', body, auth: false }),
  me: (o: Sig = {}) => request<User>('/auth/me', o),

  societies: (o: Sig = {}) => request<Society[]>('/societies', o),
  societyStats: (id: Id, o: Sig = {}) => request<SocietyStats>(`/societies/${enc(id)}/stats`, o),

  flats: (societyId?: Id, o: Sig = {}) =>
    request<Flat[]>('/flats', { ...o, query: { society_id: societyId } }),
  flat: (id: Id, o: Sig = {}) => request<Flat>(`/flats/${enc(id)}`, o),
  readings: (id: Id, q: { from?: string; to?: string; limit?: number } = {}, o: Sig = {}) =>
    request<Reading[]>(`/flats/${enc(id)}/readings`, { ...o, query: q }),
  baseline: (id: Id, o: Sig = {}) => request<Baseline>(`/flats/${enc(id)}/baseline`, o),
  forecast: (id: Id, o: Sig = {}) => request<Forecast>(`/flats/${enc(id)}/forecast`, o),

  anomalies: (q: { flat_id?: Id; active_only?: boolean } = {}, o: Sig = {}) =>
    request<Anomaly[]>('/anomalies', { ...o, query: q }),

  alerts: (q: AlertFilter = {}, o: Sig = {}) => request<Alert[]>('/alerts', { ...o, query: { ...q } }),
  acknowledge: (id: Id, body: AcknowledgeBody) =>
    request<unknown>(`/alerts/${enc(id)}/acknowledge`, { method: 'POST', body }),
  snooze: (id: Id, body: SnoozeBody) =>
    request<unknown>(`/alerts/${enc(id)}/snooze`, { method: 'POST', body }),
  override: (id: Id, body: OverrideBody) =>
    request<unknown>(`/alerts/${enc(id)}/override`, { method: 'POST', body }),
  resolve: (id: Id, body: ResolveBody) =>
    request<unknown>(`/alerts/${enc(id)}/resolve`, { method: 'POST', body }),

  valve: (flatId: Id, o: Sig = {}) => request<ValveInfo>(`/valves/${enc(flatId)}`, o),
  openValve: (flatId: Id, body: ValveCommand) =>
    request<unknown>(`/valves/${enc(flatId)}/open`, { method: 'POST', body }),
  closeValve: (flatId: Id, body: ValveCommand) =>
    request<unknown>(`/valves/${enc(flatId)}/close`, { method: 'POST', body }),

  injectFault: (body: InjectFaultBody) =>
    request<unknown>('/demo/inject-fault', { method: 'POST', body }),
  clearFault: (flatId: Id) =>
    request<unknown>('/demo/clear-fault', { method: 'POST', body: { flat_id: flatId } }),
  setSpeed: (speed: number) => request<unknown>('/demo/set-speed', { method: 'POST', body: { speed } }),
  demoStatus: (o: Sig = {}) => request<DemoStatus>('/demo/status', o),
};
