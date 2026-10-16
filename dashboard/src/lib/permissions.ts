import type { Role } from './types';

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
