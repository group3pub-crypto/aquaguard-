import { getConfig } from './config';
import { clearToken, getAccessExpiry, getRefreshToken, getToken, setTokens } from './session';
import type {
  AcknowledgeBody,
  Alert,
  AlertFilter,
  Anomaly,
  ApiErrorBody,
  Baseline,
  BindingUpdate,
  ChangePasswordRequest,
  Flat,
  FlatCreate,
  FlatDetail,
  FlatUpdate,
  Forecast,
  Id,
  LoginRequest,
  LoginResponse,
  OverrideBody,
  Reading,
  RegisterRequest,
  ResolveBody,
  Society,
  SocietyCreate,
  SocietyStats,
  SocietyUpdate,
  SnoozeBody,
  User,
  UserCreate,
  UserQuery,
  UserUpdate,
  ValveCommand,
  ValveCommandRecord,
  ValveInfo,
  WsTicketResponse,
} from './types';

/** Resolved lazily so the runtime config injected by the root layout is honoured. */
export const apiBase = (): string => `${getConfig().apiUrl}/api/v1`;
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
/** Registered by AuthProvider: invoked when the session can no longer be kept alive (refresh failed / 401 after retry). */
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
  return `${apiBase()}${path}${s ? `?${s}` : ''}`;
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

/** Access tokens are refreshed this long before they expire. */
export const REFRESH_SKEW_MS = 30_000;

let refreshInFlight: Promise<boolean> | null = null;

async function performRefresh(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) {
    clearToken();
    unauthorizedHandler?.();
    return false;
  }
  try {
    const res = await send<LoginResponse>('/auth/refresh', {
      method: 'POST',
      body: { refresh_token: refreshToken },
      auth: false,
    });
    // Rotation: the presented token is now spent. Always keep the NEW one.
    setTokens(res);
    return true;
  } catch (e) {
    // A definitive 4xx (expired, revoked, or REUSE detected => family revoked) ends the session.
    // Network errors / 5xx / 429 are transient: keep the session and let the caller retry later.
    if (e instanceof ApiError && e.kind === 'http' && e.status >= 400 && e.status < 500 && e.status !== 429) {
      clearToken();
      unauthorizedHandler?.();
      return false;
    }
    throw e;
  }
}

/**
 * Exchanges the refresh token for a new pair. Single-flight: concurrent callers share one
 * network call (a second parallel call would present an already-rotated token and revoke the
 * whole family). Resolves false when the session is over (the unauthorized handler has run).
 */
export function refreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = performRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

async function refreshIfExpiring(): Promise<void> {
  const exp = getAccessExpiry();
  if (exp === null || !getRefreshToken()) return;
  if (Date.now() < exp - REFRESH_SKEW_MS) return;
  try {
    await refreshSession();
  } catch {
    /* transient: try the request with the current token; a 401 will retry the refresh */
  }
}

async function send<T>(path: string, opts: RequestOptions = {}): Promise<T> {
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

/**
 * Authenticated JSON request. Silent refresh: proactively when the access token is about to
 * expire, and once on a 401 (then the original request is retried exactly once).
 */
export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const auth = opts.auth ?? true;
  if (!auth) return send<T>(path, opts);

  await refreshIfExpiring();
  const usedToken = getToken();
  try {
    return await send<T>(path, opts);
  } catch (e) {
    if (!(e instanceof ApiError && e.kind === 'http' && e.status === 401)) throw e;
    // Another request may already have rotated the tokens while this one was in flight.
    const current = getToken();
    let renewed: boolean;
    if (current && current !== usedToken) renewed = true;
    else {
      try {
        renewed = await refreshSession();
      } catch {
        throw e; // transient refresh failure: surface the original 401 without logging out
      }
    }
    if (!renewed) throw e; // session already ended by refreshSession()
    try {
      return await send<T>(path, opts);
    } catch (e2) {
      if (e2 instanceof ApiError && e2.kind === 'http' && e2.status === 401) {
        clearToken();
        unauthorizedHandler?.();
      }
      throw e2;
    }
  }
}

const enc = (id: Id) => encodeURIComponent(String(id));
type Sig = { signal?: AbortSignal };

export const api = {
  async login(body: LoginRequest): Promise<LoginResponse> {
    const res = await request<LoginResponse>('/auth/login', { method: 'POST', body, auth: false });
    setTokens(res);
    return res;
  },
  /** Revokes this device's refresh-token family, then forgets the tokens locally. Never throws. */
  async logout(): Promise<void> {
    const refreshToken = getRefreshToken();
    clearToken();
    if (!refreshToken) return;
    try {
      await send<void>('/auth/logout', { method: 'POST', body: { refresh_token: refreshToken }, auth: false });
    } catch {
      /* best effort: the local session is already gone */
    }
  },
  /** Needs the current password; the backend signs out every other session and returns fresh tokens. */
  async changePassword(body: ChangePasswordRequest): Promise<void> {
    const res = await request<LoginResponse>('/auth/change-password', { method: 'POST', body });
    setTokens(res);
  },
  /** Single-use, 30 s ticket for the WebSocket handshake. */
  wsTicket: () => request<WsTicketResponse>('/auth/ws-ticket', { method: 'POST' }),
  register: (body: RegisterRequest) =>
    request<User>('/auth/register', { method: 'POST', body, auth: false }),
  me: (o: Sig = {}) => request<User>('/auth/me', o),

  societies: (o: Sig = {}) => request<Society[]>('/societies', o),
  societyStats: (id: Id, o: Sig = {}) => request<SocietyStats>(`/societies/${enc(id)}/stats`, o),

  flats: (societyId?: Id, o: Sig = {}) =>
    request<Flat[]>('/flats', { ...o, query: { society_id: societyId, limit: 500 } }),
  flat: (id: Id, o: Sig = {}) => request<FlatDetail>(`/flats/${enc(id)}`, o),
  readings: (id: Id, q: { from?: string; to?: string; limit?: number } = {}, o: Sig = {}) =>
    request<Reading[]>(`/flats/${enc(id)}/readings`, { ...o, query: q }),
  baseline: (id: Id, o: Sig = {}) => request<Baseline>(`/flats/${enc(id)}/baseline`, o),
  forecast: (id: Id, o: Sig = {}) => request<Forecast>(`/flats/${enc(id)}/forecast`, o),

  anomalies: (q: { flat_id?: Id; active_only?: boolean } = {}, o: Sig = {}) =>
    request<Anomaly[]>('/anomalies', { ...o, query: q }),

  alerts: (q: AlertFilter = {}, o: Sig = {}) => request<Alert[]>('/alerts', { ...o, query: { ...q } }),
  acknowledge: (id: Id, body: AcknowledgeBody) =>
    request<Alert>(`/alerts/${enc(id)}/acknowledge`, { method: 'POST', body }),
  snooze: (id: Id, body: SnoozeBody) =>
    request<Alert>(`/alerts/${enc(id)}/snooze`, { method: 'POST', body }),
  override: (id: Id, body: OverrideBody) =>
    request<Alert>(`/alerts/${enc(id)}/override`, { method: 'POST', body }),
  resolve: (id: Id, body: ResolveBody) =>
    request<Alert>(`/alerts/${enc(id)}/resolve`, { method: 'POST', body }),

  valve: (flatId: Id, o: Sig = {}) => request<ValveInfo>(`/valves/${enc(flatId)}`, o),
  openValve: (flatId: Id, body: ValveCommand) =>
    request<ValveCommandRecord>(`/valves/${enc(flatId)}/open`, { method: 'POST', body }),
  closeValve: (flatId: Id, body: ValveCommand) =>
    request<ValveCommandRecord>(`/valves/${enc(flatId)}/close`, { method: 'POST', body }),

  // ---- admin (role admin; the backend enforces it) ----
  createSociety: (body: SocietyCreate) => request<Society>('/societies', { method: 'POST', body }),
  updateSociety: (id: Id, body: SocietyUpdate) =>
    request<Society>(`/societies/${enc(id)}`, { method: 'PATCH', body }),
  deleteSociety: (id: Id) => request<void>(`/societies/${enc(id)}`, { method: 'DELETE' }),

  createFlat: (body: FlatCreate) => request<Flat>('/flats', { method: 'POST', body }),
  updateFlat: (id: Id, body: FlatUpdate) => request<Flat>(`/flats/${enc(id)}`, { method: 'PATCH', body }),
  deleteFlat: (id: Id) => request<void>(`/flats/${enc(id)}`, { method: 'DELETE' }),

  users: (q: UserQuery = {}, o: Sig = {}) => request<User[]>('/users', { ...o, query: { ...q } }),
  createUser: (body: UserCreate) => request<User>('/users', { method: 'POST', body }),
  updateUser: (id: Id, body: UserUpdate) => request<User>(`/users/${enc(id)}`, { method: 'PATCH', body }),
  setUserBinding: (id: Id, body: BindingUpdate) =>
    request<User>(`/users/${enc(id)}/binding`, { method: 'PUT', body }),
  resetUserPassword: (id: Id, newPassword: string) =>
    request<void>(`/users/${enc(id)}/reset-password`, { method: 'POST', body: { new_password: newPassword } }),
  logoutUserEverywhere: (id: Id) => request<void>(`/users/${enc(id)}/logout-all`, { method: 'POST' }),
};
