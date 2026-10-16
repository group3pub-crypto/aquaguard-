'use client';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAuth } from '@/context/AuthProvider';
import { ConnectionBanner } from './ConnectionBanner';
import { ErrorBoundary } from './ErrorBoundary';

type Theme = 'auto' | 'light' | 'dark';
const THEMES: Theme[] = ['auto', 'light', 'dark'];

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>('auto');
  useEffect(() => {
    try {
      const saved = window.localStorage.getItem('aquaguard.theme');
      if (saved === 'light' || saved === 'dark') setTheme(saved);
    } catch {
      /* ignore */
    }
  }, []);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'auto') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', theme);
    try {
      window.localStorage.setItem('aquaguard.theme', theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length] ?? 'auto';
  return (
    <button type="button" className="btn !min-h-[32px]" onClick={() => setTheme(next)}>
      Theme: {theme}
    </button>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const pathname = usePathname();
  const links = [
    { href: '/', label: 'Overview' },
    { href: '/alerts', label: 'Alerts' },
  ];
  return (
    <>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-action focus:px-4 focus:py-2 focus:text-action-ink"
      >
        Skip to content
      </a>
      <header className="border-b border-line bg-canvas">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-8 gap-y-2 px-4 py-3 sm:px-6">
          <Link href="/" className="text-lg font-bold">
            AquaGuard
          </Link>
          <nav aria-label="Primary" className="flex gap-1">
            {links.map((l) => {
              const current = l.href === '/' ? pathname === '/' : pathname.startsWith(l.href);
              return (
                <Link
                  key={l.href}
                  href={l.href}
                  aria-current={current ? 'page' : undefined}
                  className={`rounded-pill px-3 py-1.5 text-sm font-semibold ${
                    current ? 'bg-panel text-action' : 'text-ink-2 hover:text-ink'
                  }`}
                >
                  {l.label}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="hidden text-ink-2 sm:inline">
              {user?.name} · {user?.role}
            </span>
            <ThemeToggle />
            <button type="button" className="btn !min-h-[32px]" onClick={logout}>
              Log out
            </button>
          </div>
        </div>
      </header>
      <ConnectionBanner />
      <main id="main" tabIndex={-1} className="mx-auto max-w-7xl px-4 py-8 sm:px-6">
        <ErrorBoundary>{children}</ErrorBoundary>
      </main>
    </>
  );
}
