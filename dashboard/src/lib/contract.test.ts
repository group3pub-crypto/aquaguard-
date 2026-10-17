// Contract test: the dashboard API client and its fixtures are checked against the
// backend's OpenAPI document (src/lib/openapi.snapshot.json, regenerate with
// `npm run snapshot:openapi`). The validator is intentionally lightweight.
import { describe, expect, it } from 'vitest';
import snapshot from './openapi.snapshot.json';
import { api, refreshSession } from './api';
import { setTokens } from './session';
import * as fx from '@/test/fixtures';
import { mockFetch } from '@/test/utils';

type Json = Record<string, unknown>;
const spec = snapshot as unknown as {
  paths: Record<string, Record<string, Json>>;
  components: { schemas: Record<string, Json> };
};

function resolve(schema: Json): Json {
  const ref = schema.$ref;
  if (typeof ref === 'string') {
    const name = ref.replace('#/components/schemas/', '');
    const target = spec.components.schemas[name];
    if (!target) throw new Error(`unresolved ref ${ref}`);
    return resolve(target);
  }
  return schema;
}

/** Returns problems; empty means the value conforms (required keys, types, enums, no unknown keys). */
function validate(schemaIn: Json, value: unknown, path = '$'): string[] {
  const schema = resolve(schemaIn);
  const alts = (schema.anyOf ?? schema.oneOf) as Json[] | undefined;
  if (alts) {
    const results = alts.map((a) => validate(a, value, path));
    return results.some((r) => r.length === 0) ? [] : [`${path}: matches no alternative (${results.flat().join(' | ')})`];
  }
  if ('const' in schema) return value === schema.const ? [] : [`${path}: expected const ${String(schema.const)}`];
  if (Array.isArray(schema.enum)) return schema.enum.includes(value) ? [] : [`${path}: ${String(value)} not in enum`];
  switch (schema.type) {
    case 'null':
      return value === null ? [] : [`${path}: expected null`];
    case 'string': {
      if (typeof value !== 'string') return [`${path}: expected string`];
      if (typeof schema.pattern === 'string' && !new RegExp(schema.pattern).test(value)) return [`${path}: pattern mismatch`];
      if (schema.format === 'uuid' && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) return [`${path}: not a uuid`];
      if (schema.format === 'date-time' && Number.isNaN(Date.parse(value))) return [`${path}: not a date-time`];
      return [];
    }
    case 'integer':
      return Number.isInteger(value) ? [] : [`${path}: expected integer`];
    case 'number':
      return typeof value === 'number' ? [] : [`${path}: expected number`];
    case 'boolean':
      return typeof value === 'boolean' ? [] : [`${path}: expected boolean`];
    case 'array': {
      if (!Array.isArray(value)) return [`${path}: expected array`];
      return value.flatMap((v, i) => validate((schema.items ?? {}) as Json, v, `${path}[${i}]`));
    }
    default: {
      if (schema.type !== 'object' && !schema.properties) return []; // unconstrained
      if (typeof value !== 'object' || value === null || Array.isArray(value)) return [`${path}: expected object`];
      const obj = value as Json;
      const props = (schema.properties ?? {}) as Record<string, Json>;
      const errs: string[] = [];
      for (const req of (schema.required ?? []) as string[]) if (!(req in obj)) errs.push(`${path}: missing required "${req}"`);
      if (schema.properties) {
        for (const k of Object.keys(obj)) if (!(k in props)) errs.push(`${path}: unknown key "${k}" (not in backend schema)`);
      }
      for (const [k, sub] of Object.entries(props)) if (k in obj) errs.push(...validate(sub, obj[k], `${path}.${k}`));
      return errs;
    }
  }
}

function responseSchema(path: string, method: string, status = '200'): Json {
  const op = spec.paths[path]?.[method];
  if (!op) throw new Error(`${method.toUpperCase()} ${path} not in OpenAPI snapshot`);
  const responses = op.responses as Record<string, Json>;
  const content = (responses[status]?.content as Record<string, Json> | undefined)?.['application/json'];
  return (content?.schema ?? {}) as Json;
}
function bodySchema(path: string, method: string): Json {
  const op = spec.paths[path]?.[method] as Json;
  const content = ((op.requestBody as Json | undefined)?.content as Record<string, Json> | undefined)?.['application/json'];
  return (content?.schema ?? {}) as Json;
}

/** Finds the snapshot path template matching a concrete URL path. */
function templateFor(pathname: string, method: string): string | undefined {
  return Object.keys(spec.paths).find((tpl) => {
    if (!spec.paths[tpl]?.[method]) return false;
    return new RegExp(`^${tpl.replace(/\{[^}]+\}/g, '[^/]+')}$`).test(pathname);
  });
}

const F = fx.IDS.flat;

describe('api client matches the backend OpenAPI snapshot', () => {
  const calls: { name: string; run: () => Promise<unknown> }[] = [
    { name: 'login', run: () => api.login({ username: 'a', password: 'b' }) },
    { name: 'register', run: () => api.register({ username: 'abc', password: 'passw0rdx', name: 'A' }) },
    { name: 'me', run: () => api.me() },
    { name: 'wsTicket', run: () => api.wsTicket() },
    { name: 'changePassword', run: () => api.changePassword({ current_password: 'old-pass1', new_password: 'new-pass2' }) },
    { name: 'createSociety', run: () => api.createSociety({ name: 'A', slug: 'a-b', address: null }) },
    { name: 'updateSociety', run: () => api.updateSociety(fx.IDS.society, { name: 'B', address: 'x' }) },
    { name: 'deleteSociety', run: () => api.deleteSociety(fx.IDS.society) },
    { name: 'createFlat', run: () => api.createFlat({ society_id: fx.IDS.society, unit_number: 'A-1', floor: 2, residents: 3, sensor_id: 'esp-1' }) },
    { name: 'updateFlat', run: () => api.updateFlat(F, { floor: 1, residents: 2, sensor_id: 'esp-2' }) },
    { name: 'deleteFlat', run: () => api.deleteFlat(F) },
    { name: 'users', run: () => api.users({ role: 'resident', is_active: true, q: 'al', limit: 50, offset: 0 }) },
    { name: 'createUser', run: () => api.createUser({ username: 'bob', name: 'Bob', password: 'passw0rdx', role: 'resident', flat_id: F, society_id: null }) },
    { name: 'updateUser', run: () => api.updateUser(fx.userOut.id, { is_active: false }) },
    { name: 'setUserBinding', run: () => api.setUserBinding(fx.userOut.id, { flat_id: F, society_id: null }) },
    { name: 'resetUserPassword', run: () => api.resetUserPassword(fx.userOut.id, 'Passw0rdxyz') },
    { name: 'logoutUserEverywhere', run: () => api.logoutUserEverywhere(fx.userOut.id) },
    { name: 'societies', run: () => api.societies() },
    { name: 'societyStats', run: () => api.societyStats(fx.IDS.society) },
    { name: 'flats', run: () => api.flats(fx.IDS.society) },
    { name: 'flat', run: () => api.flat(F) },
    { name: 'readings', run: () => api.readings(F, { limit: 10, from: '2026-01-01T00:00:00Z' }) },
    { name: 'baseline', run: () => api.baseline(F) },
    { name: 'forecast', run: () => api.forecast(F) },
    { name: 'anomalies', run: () => api.anomalies({ flat_id: F, active_only: true }) },
    { name: 'alerts', run: () => api.alerts({ flat_id: F, state: 'ticket', limit: 500 }) },
    { name: 'acknowledge', run: () => api.acknowledge(fx.IDS.alert, { acknowledged_by: 'x' }) },
    { name: 'snooze', run: () => api.snooze(fx.IDS.alert, { duration_minutes: 30, snoozed_by: 'x' }) },
    { name: 'override', run: () => api.override(fx.IDS.alert, { reason: 'r', overridden_by: 'x' }) },
    { name: 'resolve', run: () => api.resolve(fx.IDS.alert, { resolved_by: 'x' }) },
    { name: 'valve', run: () => api.valve(F) },
    { name: 'openValve', run: () => api.openValve(F, { source: 'manual_dashboard', issued_by: 'x', reason: 'r' }) },
    { name: 'closeValve', run: () => api.closeValve(F, { source: 'manual_dashboard', issued_by: 'x' }) },
  ];

  it.each(calls)('$name uses an existing path, method, query params and body', async ({ run }) => {
    const { calls: seen } = mockFetch(() => ({ body: {} }));
    await run().catch(() => undefined);
    const c = seen[0];
    expect(c).toBeDefined();
    if (!c) return;
    const method = (c.init.method ?? 'GET').toLowerCase();
    const tpl = templateFor(c.url.pathname, method);
    expect(tpl, `${method.toUpperCase()} ${c.url.pathname} missing from OpenAPI`).toBeDefined();
    if (!tpl) return;
    const op = spec.paths[tpl]?.[method] as Json;
    const params = (op.parameters ?? []) as { name: string; in: string; schema?: Json }[];
    for (const [k, v] of c.url.searchParams) {
      const p = params.find((x) => x.in === 'query' && x.name === k);
      expect(p, `query param "${k}" not accepted by ${tpl}`).toBeDefined();
      if (p?.schema) {
        const alts = ((p.schema.anyOf as Json[] | undefined) ?? [p.schema]).map((a) => a.type);
        const typed = alts.includes('integer') ? Number(v) : alts.includes('boolean') ? v === 'true' : v;
        expect(validate(p.schema, typed)).toEqual([]);
      }
    }
    if (c.body !== undefined) expect(validate(bodySchema(tpl, method), c.body)).toEqual([]);
  });

  it('refresh and logout use the documented paths and bodies', async () => {
    setTokens({ access_token: 'a', refresh_token: 'r'.repeat(24), expires_in: 1 }, 0);
    const { calls: seen } = mockFetch(() => ({ body: fx.tokenResponse }));
    await refreshSession();
    await api.logout();
    for (const c of seen) {
      const tpl = templateFor(c.url.pathname, 'post');
      expect(tpl, c.url.pathname).toBeDefined();
      if (tpl) expect(validate(bodySchema(tpl, 'post'), c.body)).toEqual([]);
    }
    expect(seen.map((c) => c.url.pathname)).toEqual(['/api/v1/auth/refresh', '/api/v1/auth/logout']);
  });

  it('does not call any demo endpoint', () => {
    expect(Object.keys(api).some((k) => /fault|speed|demo/i.test(k))).toBe(false);
    expect(Object.keys(spec.paths).some((p) => p.includes('/demo'))).toBe(false);
  });
});

describe('fixtures validate against backend response schemas', () => {
  const cases: [string, string, string, unknown][] = [
    ['society', '/api/v1/societies', 'get', [fx.society]],
    ['society stats', '/api/v1/societies/{society_id}/stats', 'get', fx.societyStats],
    ['flat list', '/api/v1/flats', 'get', [fx.flat]],
    ['flat detail', '/api/v1/flats/{flat_id}', 'get', fx.flatDetail],
    ['readings', '/api/v1/flats/{flat_id}/readings', 'get', [fx.reading]],
    ['baseline', '/api/v1/flats/{flat_id}/baseline', 'get', fx.baseline],
    ['forecast', '/api/v1/flats/{flat_id}/forecast', 'get', fx.forecast],
    ['anomalies', '/api/v1/anomalies', 'get', [fx.anomaly, fx.ruleAnomaly]],
    ['alerts', '/api/v1/alerts', 'get', [fx.alert]],
    ['user', '/api/v1/users', 'get', [fx.userOut]],
    ['login tokens', '/api/v1/auth/login', 'post', fx.tokenResponse],
    ['refresh tokens', '/api/v1/auth/refresh', 'post', fx.tokenResponse],
    ['change-password tokens', '/api/v1/auth/change-password', 'post', fx.tokenResponse],
    ['ws ticket', '/api/v1/auth/ws-ticket', 'post', { ticket: 'abc', expires_in: 30 }],
    ['valve status', '/api/v1/valves/{flat_id}', 'get', fx.valveInfo],
    ['valve command', '/api/v1/valves/{flat_id}/close', 'post', fx.valveCommand],
  ];
  it.each(cases)('%s', (_n, path, method, value) => {
    expect(validate(responseSchema(path, method), value)).toEqual([]);
  });

  it('the validator catches invented fields and missing keys', () => {
    const statsSchema = responseSchema('/api/v1/societies/{society_id}/stats', 'get');
    expect(validate(statsSchema, { total_consumption: 1 }).length).toBeGreaterThan(0);
    expect(validate(statsSchema, { ...fx.societyStats, losses: 3 })).toEqual(expect.arrayContaining([expect.stringContaining('unknown key')]));
  });

  it('GET /alerts rejects the nonexistent "active" state while real states are accepted', () => {
    const op = spec.paths['/api/v1/alerts']?.get as Json;
    const p = (op.parameters as { name: string; schema: Json }[]).find((x) => x.name === 'state');
    const schema = (p?.schema ?? {}) as Json;
    expect(validate(schema, 'active')).not.toEqual([]);
    for (const s of ['nudge', 'alert', 'ticket', 'auto_shutoff', 'acknowledged', 'snoozed', 'overridden', 'resolved']) {
      expect(validate(schema, s)).toEqual([]);
    }
  });
});
