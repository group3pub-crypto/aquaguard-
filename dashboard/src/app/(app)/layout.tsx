import type { ReactNode } from 'react';
import { AuthGuard } from '@/components/AuthGuard';
import { AppShell } from '@/components/AppShell';
import { LiveProvider } from '@/context/LiveProvider';

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <AuthGuard>
      <LiveProvider>
        <AppShell>{children}</AppShell>
      </LiveProvider>
    </AuthGuard>
  );
}
