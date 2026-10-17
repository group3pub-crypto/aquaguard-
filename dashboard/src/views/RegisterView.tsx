'use client';
import { useState } from 'react';
import type { FormEvent } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthProvider';
import { ApiError } from '@/lib/api';
import { markRegistrationDisabled } from '@/lib/features';
import { PASSWORD_HINT, validateName, validatePassword, validateUsername } from '@/lib/validation';

export function RegisterView() {
  const { register } = useAuth();
  const router = useRouter();
  const [name, setName] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<{ name?: string; username?: string; password?: string }>({});

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const errs = {
      name: validateName(name, 'Full name'),
      username: validateUsername(username),
      password: validatePassword(password),
    };
    setFieldErrors(errs);
    if (errs.name || errs.username || errs.password) return;
    setBusy(true);
    setError(null);
    try {
      await register({ name: name.trim(), username: username.trim(), password });
      router.replace('/');
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        markRegistrationDisabled();
        setDisabled(true);
      } else {
        setError(err instanceof Error ? err.message : 'Registration failed');
      }
    } finally {
      setBusy(false);
    }
  };

  if (disabled) {
    return (
      <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-4 py-12">
        <h1 className="text-3xl font-semibold">Registration is closed</h1>
        <p role="status" className="mt-4 text-ink-2">
          Self-registration is not enabled for this AquaGuard installation. Ask your administrator to create an
          account for you.
        </p>
        <p className="mt-6 text-sm">
          <Link href="/login" className="font-semibold text-action">
            Back to sign in
          </Link>
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-4 py-12">
      <h1 className="text-3xl font-semibold">Create an account</h1>
      <p className="mt-1 text-sm text-ink-2">New accounts are not linked to a home until an administrator links them.</p>
      <form onSubmit={onSubmit} noValidate className="mt-8 space-y-4">
        <div>
          <label className="label" htmlFor="name">
            Full name
          </label>
          <input id="name" className="field" autoComplete="name" required aria-invalid={!!fieldErrors.name} aria-describedby={fieldErrors.name ? 'name-err' : undefined} value={name} onChange={(e) => setName(e.target.value)} />
          {fieldErrors.name ? <p id="name-err" className="mt-1 text-sm text-error">{fieldErrors.name}</p> : null}
        </div>
        <div>
          <label className="label" htmlFor="username">
            Username
          </label>
          <input id="username" className="field" autoComplete="username" required aria-invalid={!!fieldErrors.username} aria-describedby={fieldErrors.username ? 'username-err' : undefined} value={username} onChange={(e) => setUsername(e.target.value)} />
          {fieldErrors.username ? <p id="username-err" className="mt-1 text-sm text-error">{fieldErrors.username}</p> : null}
        </div>
        <div>
          <label className="label" htmlFor="password">
            Password
          </label>
          <input id="password" type="password" className="field" autoComplete="new-password" required aria-invalid={!!fieldErrors.password} aria-describedby={fieldErrors.password ? 'password-hint password-err' : 'password-hint'} value={password} onChange={(e) => setPassword(e.target.value)} />
          <p id="password-hint" className="mt-1 text-xs text-ink-2">{PASSWORD_HINT}</p>
          {fieldErrors.password ? <p id="password-err" className="mt-1 text-sm text-error">{fieldErrors.password}</p> : null}
        </div>
        {error ? (
          <p role="alert" className="text-sm text-error">
            {error}
          </p>
        ) : null}
        <button type="submit" className="btn btn-primary w-full" disabled={busy}>
          {busy ? 'Creating…' : 'Register'}
        </button>
      </form>
      <p className="mt-6 text-sm text-ink-2">
        Already registered?{' '}
        <Link href="/login" className="font-semibold text-action">
          Sign in
        </Link>
      </p>
    </main>
  );
}
