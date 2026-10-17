import type { Role, User } from './types';

// UI gating is a convenience only. The backend enforces authorisation.
export interface Permissions {
  controlValve: boolean;
  override: boolean;
  acknowledge: boolean;
  resolve: boolean;
  snooze: boolean;
}

export function permissionsFor(role: Role | null | undefined): Permissions {
  const admin = role === 'admin';
  const staff = admin || role === 'maintenance';
  return {
    controlValve: admin,
    override: admin,
    acknowledge: staff,
    resolve: staff,
    snooze: staff,
  };
}

/**
 * A resident without a flat (or maintenance without a society) is "unbound": the backend
 * scopes every list to nothing for them. Admins are never unbound.
 */
export function isUnbound(user: Pick<User, 'role' | 'flat_id' | 'society_id'> | null | undefined): boolean {
  if (!user) return false;
  if (user.role === 'admin') return false;
  if (user.role === 'maintenance') return !user.society_id;
  return !user.flat_id;
}
