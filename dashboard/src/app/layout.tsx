import type { Metadata, Viewport } from 'next';
import { Open_Sans } from 'next/font/google';
import type { ReactNode } from 'react';
import './globals.css';
import { AuthProvider } from '@/context/AuthProvider';
import { ErrorBoundary } from '@/components/ErrorBoundary';

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

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={openSans.variable}>
      <body className="min-h-screen font-sans">
        <ErrorBoundary>
          <AuthProvider>{children}</AuthProvider>
        </ErrorBoundary>
      </body>
    </html>
  );
}
