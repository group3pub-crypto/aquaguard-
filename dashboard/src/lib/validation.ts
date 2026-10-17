// Client-side validation mirroring the backend constraints (schemas/*.py, OpenAPI snapshot).
// This only gives faster feedback: the server is the authority and its `detail` is always shown.
import type { Role } from './types';

export type Errors<K extends string = string> = Partial<Record<K, string>>;

export const PASSWORD_HINT = 'At least 8 characters, with a letter and a digit, at most 72 bytes.';
export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const SLUG_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;
export const UNIT_RE = /^[A-Za-z0-9_-]{1,50}$/;
export const SENSOR_RE = /^[A-Za-z0-9_.-]{1,100}$/;

export function byteLength(s: string): number {
  return new TextEncoder().encode(s).length;
}

export function validatePassword(pw: string): string | undefined {
  if (!pw) return 'Password is required.';
  if (pw.length < 8) return 'Password must be at least 8 characters.';
  if (byteLength(pw) > 72) return 'Password must be at most 72 bytes (long or non-Latin passwords count more).';
  if (!/[A-Za-z]/.test(pw) || !/\d/.test(pw)) return 'Password needs at least one letter and one digit.';
  return undefined;
}

export function validateUsername(u: string): string | undefined {
  const v = u.trim();
  if (v.length < 3) return 'Username must be at least 3 characters.';
  if (v.length > 50) return 'Username must be at most 50 characters.';
  return undefined;
}

export function validateName(n: string, label = 'Name', max = 200): string | undefined {
  const v = n.trim();
  if (!v) return `${label} is required.`;
  if (v.length > max) return `${label} must be at most ${max} characters.`;
  return undefined;
}

export function validateSociety(v: { name: string; slug?: string; address: string }, creating: boolean): Errors<'name' | 'slug' | 'address'> {
  const e: Errors<'name' | 'slug' | 'address'> = {};
  const name = validateName(v.name);
  if (name) e.name = name;
  if (creating) {
    const slug = (v.slug ?? '').trim();
    if (!slug) e.slug = 'Slug is required.';
    else if (slug.length > 64) e.slug = 'Slug must be at most 64 characters.';
    else if (!SLUG_RE.test(slug)) e.slug = 'Use lowercase letters and digits separated by single hyphens (e.g. green-meadows).';
  }
  if (v.address.length > 500) e.address = 'Address must be at most 500 characters.';
  return e;
}

function intInRange(raw: string, min: number, max: number, label: string): string | undefined {
  if (!/^-?\d+$/.test(raw.trim())) return `${label} must be a whole number.`;
  const n = Number(raw);
  if (n < min || n > max) return `${label} must be between ${min} and ${max}.`;
  return undefined;
}

export interface FlatForm {
  society_id: string;
  unit_number: string;
  floor: string;
  residents: string;
  sensor_id: string;
}

export function validateFlat(v: FlatForm, creating: boolean): Errors<keyof FlatForm> {
  const e: Errors<keyof FlatForm> = {};
  if (creating) {
    if (!UUID_RE.test(v.society_id)) e.society_id = 'Choose a society.';
    if (!UNIT_RE.test(v.unit_number.trim())) e.unit_number = 'Unit number: 1-50 letters, digits, hyphens or underscores.';
  }
  if (v.floor.trim()) {
    const m = intInRange(v.floor, -5, 300, 'Floor');
    if (m) e.floor = m;
  }
  if (v.residents.trim()) {
    const m = intInRange(v.residents, 0, 50, 'Residents');
    if (m) e.residents = m;
  } else if (!creating) {
    e.residents = 'Residents is required.';
  }
  if (v.sensor_id.trim() && !SENSOR_RE.test(v.sensor_id.trim())) {
    e.sensor_id = 'Sensor id: 1-100 letters, digits, dots, hyphens or underscores.';
  }
  return e;
}

export interface UserForm {
  username: string;
  name: string;
  password: string;
  role: Role;
  society_id: string;
  flat_id: string;
}

export function validateUser(v: UserForm): Errors<keyof UserForm> {
  const e: Errors<keyof UserForm> = {};
  const u = validateUsername(v.username);
  if (u) e.username = u;
  const n = validateName(v.name, 'Full name');
  if (n) e.name = n;
  const p = validatePassword(v.password);
  if (p) e.password = p;
  return e;
}

/** Binding rules from the backend (services/users.py resolve_binding). */
export function validateBinding(role: Role, flatId: string, societyId: string): { flat_id?: string; society_id?: string } {
  const e: { flat_id?: string; society_id?: string } = {};
  if (role === 'admin' && (flatId || societyId)) e.society_id = 'Admin accounts are not bound to a society or flat.';
  if (role === 'maintenance' && flatId) e.flat_id = 'Maintenance accounts bind to a society, not a flat.';
  return e;
}

/** Friendly text for the failures the admin screens can hit; falls back to the server detail. */
export function friendlyAdminError(status: number, detail: string): string {
  if (status === 409 && /last active admin/i.test(detail)) {
    return 'You cannot demote or deactivate the last active administrator. Promote another admin first.';
  }
  if (status === 403) return 'You do not have permission to do that.';
  if (status === 404) return `Not found - it may have been deleted. (${detail})`;
  return detail;
}
