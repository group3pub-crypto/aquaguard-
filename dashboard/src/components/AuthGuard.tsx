'use client';
import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthProvider';
import { LoadingState } from './States';

/** Redirects to /login when unauthenticated; renders nothing protected until authenticated. */
export function AuthGuard({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const router = useRouter();
  useEffect(() => {
    if (status === 'unauthenticated') router.replace('/login');
  }, [status, router]);
  if (status !== 'authenticated') return <LoadingState label="Checking session" />;
  return <>{children}</>;
}
