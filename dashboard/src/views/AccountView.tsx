'use client';
import { useState } from 'react';
import type { FormEvent } from 'react';
import { api } from '@/lib/api';
import { useAuth } from '@/context/AuthProvider';
import { useAction } from '@/hooks/useAction';
import { PASSWORD_HINT, validatePassword } from '@/lib/validation';
import { FormError, TextField } from '@/components/admin/Fields';

export function AccountView() {
  const { user } = useAuth();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [errors, setErrors] = useState<{ current?: string; next?: string; confirm?: string }>({});
  const [done, setDone] = useState(false);
  const { busy, error, run } = useAction();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setDone(false);
    const errs = {
      current: current ? undefined : 'Enter your current password.',
      next: validatePassword(next) ?? (next === current ? 'The new password must differ from the current one.' : undefined),
      confirm: confirm === next ? undefined : 'The passwords do not match.',
    };
    setErrors(errs);
    if (errs.current || errs.next || errs.confirm) return;
    const ok = await run(() => api.changePassword({ current_password: current, new_password: next }));
    if (ok) {
      setDone(true);
      setCurrent('');
      setNext('');
      setConfirm('');
    }
  };

  return (
    <div className="max-w-md space-y-6">
      <div>
        <h1 className="text-3xl font-semibold">Account</h1>
        <p className="mt-1 text-ink-2">
          Signed in as {user?.username} ({user?.role}).
        </p>
      </div>
      <form onSubmit={submit} noValidate aria-labelledby="pw-h" className="card space-y-4">
        <h2 id="pw-h" className="text-xl font-semibold">Change password</h2>
        <TextField id="pw-current" label="Current password" type="password" required autoComplete="current-password" value={current} error={errors.current} onChange={setCurrent} />
        <TextField id="pw-new" label="New password" type="password" required autoComplete="new-password" hint={PASSWORD_HINT} value={next} error={errors.next} onChange={setNext} />
        <TextField id="pw-confirm" label="Repeat new password" type="password" required autoComplete="new-password" value={confirm} error={errors.confirm} onChange={setConfirm} />
        <FormError id="pw-error" message={error} />
        {done ? (
          <p role="status" className="rounded-md border border-safe bg-safe-soft p-3 text-sm">
            Password changed. Your other sessions were signed out.
          </p>
        ) : null}
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Saving…' : 'Change password'}
        </button>
      </form>
    </div>
  );
}
