'use client';
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthProvider';
import { registrationAvailable } from '@/lib/features';

export function LoginView() {
  const { login, status } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Resolved after mount: the runtime config is only on `window` in the browser.
  const [canRegister, setCanRegister] = useState(false);
  useEffect(() => setCanRegister(registrationAvailable()), []);

  useEffect(() => {
    if (status === 'authenticated') router.replace('/');
  }, [status, router]);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
      router.replace('/');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-4 py-12">
      <h1 className="text-3xl font-semibold">Sign in to AquaGuard</h1>
      <p className="mt-1 text-ink-2">Monitor and protect your society&apos;s water supply.</p>
      <form onSubmit={onSubmit} className="mt-8 space-y-4">
        <div>
          <label className="label" htmlFor="username">
            Username
          </label>
          <input id="username" className="field" autoComplete="username" required value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="password">
            Password
          </label>
          <input id="password" type="password" className="field" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error ? (
          <p id="login-error" role="alert" className="text-sm text-error">
            {error}
          </p>
        ) : null}
        <button type="submit" className="btn btn-primary w-full" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      {canRegister ? (
        <p className="mt-6 text-sm text-ink-2">
          No account?{' '}
          <Link href="/register" className="font-semibold text-action">
            Register
          </Link>
        </p>
      ) : (
        <p className="mt-6 text-sm text-ink-2">Need an account? Ask your administrator to create one.</p>
      )}
    </main>
  );
}
