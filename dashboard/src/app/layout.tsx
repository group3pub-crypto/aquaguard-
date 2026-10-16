import type { Metadata, Viewport } from 'next';
import { Open_Sans } from 'next/font/google';
import { headers } from 'next/headers';
import type { ReactNode } from 'react';
import './globals.css';
import { AuthProvider } from '@/context/AuthProvider';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { readRuntimeConfig } from '@/lib/serverConfig';

const openSans = Open_Sans({ subsets: ['latin'], variable: '--font-open-sans', display: 'swap' });

export const metadata: Metadata = {
  title: 'AquaGuard',
  description: 'AI water-leak management dashboard',
};
export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  colorScheme: 'light dark',
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Reading request headers opts every page into dynamic rendering, which is required for
  // per-request CSP nonces (set by src/middleware.ts) and runtime configuration.
  const nonce = (await headers()).get('x-nonce') ?? undefined;
  const config = JSON.stringify(readRuntimeConfig()).replace(/</g, '\\u003c');
  return (
    <html lang="en" className={openSans.variable}>
      <body className="min-h-screen font-sans">
        <script
          nonce={nonce}
          suppressHydrationWarning
          dangerouslySetInnerHTML={{ __html: `window.__AQUAGUARD_CONFIG__=${config};` }}
        />
        <ErrorBoundary>
          <AuthProvider>{children}</AuthProvider>
        </ErrorBoundary>
      </body>
    </html>
  );
}
