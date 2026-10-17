import { ApiError } from '@/lib/api';
import { friendlyAdminError } from '@/lib/validation';

export function adminErrorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.kind === 'http' ? friendlyAdminError(e.status, e.message) : e.message;
  return e instanceof Error ? e.message : 'Something went wrong';
}
