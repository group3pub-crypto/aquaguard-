'use client';
import type { ReactNode } from 'react';
import { useAuth } from '@/context/AuthProvider';
import { PermissionState } from '@/components/States';

/** UI gate only: the backend enforces the admin role on every endpoint. */
export function AdminOnly({ what, children }: { what: string; children: ReactNode }) {
  const { user } = useAuth();
  if (user?.role !== 'admin') return <PermissionState what={what} />;
  return <>{children}</>;
}
