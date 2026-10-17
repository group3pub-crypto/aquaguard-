'use client';
import { useState } from 'react';
import type { FormEvent } from 'react';
import { api } from '@/lib/api';
import { generateTempPassword } from '@/lib/password';
import { PASSWORD_HINT, validateBinding, validateUser, type UserForm } from '@/lib/validation';
import type { Flat, Role, Society, User } from '@/lib/types';
import { usePolling } from '@/hooks/usePolling';
import { useAction } from '@/hooks/useAction';
import { AdminOnly } from '@/components/admin/AdminOnly';
import { FormError, SelectField, TextField } from '@/components/admin/Fields';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataState } from '@/components/States';
import { StatusBadge } from '@/components/StatusBadge';
import { adminErrorMessage } from './shared';

const PAGE = 50;
const ROLES: { value: Role; label: string }[] = [
  { value: 'resident', label: 'Resident' },
  { value: 'maintenance', label: 'Maintenance' },
  { value: 'admin', label: 'Admin' },
];

function BindingSelects({
  idPrefix,
  role,
  societies,
  flats,
  societyId,
  flatId,
  onSociety,
  onFlat,
  errors,
}: {
  idPrefix: string;
  role: Role;
  societies: Society[];
  flats: Flat[];
  societyId: string;
  flatId: string;
  onSociety: (v: string) => void;
  onFlat: (v: string) => void;
  errors?: { flat_id?: string; society_id?: string };
}) {
  const socName = (id: unknown) => societies.find((s) => String(s.id) === String(id))?.name ?? '';
  if (role === 'admin') return <p className="text-sm text-ink-2">Admin accounts are not bound to a society or flat.</p>;
  if (role === 'maintenance') {
    return (
      <SelectField
        id={`${idPrefix}-society`}
        label="Society"
        hint="Maintenance staff see one society. Leave unset to link later."
        value={societyId}
        error={errors?.society_id}
        options={[{ value: '', label: 'Not linked' }, ...societies.map((s) => ({ value: String(s.id), label: s.name }))]}
        onChange={onSociety}
      />
    );
  }
  return (
    <SelectField
      id={`${idPrefix}-flat`}
      label="Flat"
      hint="Residents see only their own flat. Leave unset to link later."
      value={flatId}
      error={errors?.flat_id}
      options={[
        { value: '', label: 'Not linked' },
        ...flats.map((f) => ({ value: String(f.id), label: `${socName(f.society_id)} / ${f.unit_number}`.replace(/^ \/ /, '') })),
      ]}
      onChange={onFlat}
    />
  );
}

function CreateUserForm({ societies, flats, onDone, onCancel }: { societies: Society[]; flats: Flat[]; onDone: () => void; onCancel: () => void }) {
  const [v, setV] = useState<UserForm>({ username: '', name: '', password: '', role: 'resident', society_id: '', flat_id: '' });
  const [errors, setErrors] = useState<ReturnType<typeof validateUser>>({});
  const [bindErrors, setBindErrors] = useState<{ flat_id?: string; society_id?: string }>({});
  const { busy, error, run } = useAction();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const errs = validateUser(v);
    const be = validateBinding(v.role, v.flat_id, v.society_id);
    setErrors(errs);
    setBindErrors(be);
    if (Object.keys(errs).length || Object.keys(be).length) return;
    const ok = await run(async () => {
      try {
        await api.createUser({
          username: v.username.trim(),
          name: v.name.trim(),
          password: v.password,
          role: v.role,
          flat_id: v.role === 'resident' && v.flat_id ? v.flat_id : null,
          society_id: v.role === 'maintenance' && v.society_id ? v.society_id : null,
        });
      } catch (err) {
        throw new Error(adminErrorMessage(err));
      }
    });
    if (ok) onDone();
  };

  return (
    <form onSubmit={submit} noValidate aria-labelledby="user-form-h" className="card space-y-4">
      <h2 id="user-form-h" className="text-xl font-semibold">New user</h2>
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField id="usr-username" label="Username" required maxLength={50} autoComplete="off" hint="3 to 50 characters." value={v.username} error={errors.username} onChange={(username) => setV({ ...v, username })} />
        <TextField id="usr-name" label="Full name" required maxLength={200} value={v.name} error={errors.name} onChange={(name) => setV({ ...v, name })} />
      </div>
      <div>
        <TextField id="usr-password" label="Initial password" required type="password" autoComplete="new-password" hint={PASSWORD_HINT} value={v.password} error={errors.password} onChange={(password) => setV({ ...v, password })} />
        <button type="button" className="btn mt-2 !min-h-[32px]" onClick={() => setV({ ...v, password: generateTempPassword() })}>
          Generate password
        </button>
      </div>
      <SelectField
        id="usr-role"
        label="Role"
        required
        value={v.role}
        options={ROLES}
        onChange={(role) => setV({ ...v, role: role as Role, flat_id: '', society_id: '' })}
      />
      <BindingSelects
        idPrefix="usr"
        role={v.role}
        societies={societies}
        flats={flats}
        societyId={v.society_id}
        flatId={v.flat_id}
        errors={bindErrors}
        onSociety={(society_id) => setV({ ...v, society_id })}
        onFlat={(flat_id) => setV({ ...v, flat_id })}
      />
      <FormError id="user-form-error" message={error} />
      <div className="flex gap-2">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Creating…' : 'Create user'}
        </button>
        <button type="button" className="btn" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function TempPasswordDialog({ username, password, onClose }: { username: string; password: string; onClose: () => void }) {
  const [copied, setCopied] = useState<'yes' | 'no' | null>(null);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(password);
      setCopied('yes');
    } catch {
      setCopied('no');
    }
  };
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="temp-pw-h"
        aria-describedby="temp-pw-d"
        onKeyDown={(e) => {
          if (e.key === 'Escape') onClose();
        }}
        className="w-full max-w-md rounded-lg border border-line bg-canvas p-6"
      >
        <h2 id="temp-pw-h" className="text-lg font-semibold">Temporary password for {username}</h2>
        <p id="temp-pw-d" className="mt-2 text-sm text-ink-2">
          This is shown <strong>only once</strong>. Give it to the user securely; all their sessions were signed out.
        </p>
        <p className="mt-4 select-all break-all rounded-md border border-line bg-panel p-3 font-mono text-base" data-testid="temp-password">
          {password}
        </p>
        <div className="mt-4 flex items-center gap-3">
          <button type="button" className="btn" autoFocus onClick={() => void copy()}>
            Copy password
          </button>
          <span role="status" className="text-sm text-ink-2">
            {copied === 'yes' ? 'Copied.' : copied === 'no' ? 'Copy failed - select and copy it manually.' : ''}
          </span>
        </div>
        <div className="mt-6 flex justify-end">
          <button type="button" className="btn btn-primary" onClick={onClose}>
            I have saved it
          </button>
        </div>
      </div>
    </div>
  );
}

type Dialog =
  | { kind: 'deactivate'; user: User }
  | { kind: 'reset'; user: User }
  | { kind: 'logout'; user: User }
  | { kind: 'bind'; user: User }
  | null;

export function UsersAdminView() {
  const [role, setRole] = useState('');
  const [active, setActive] = useState('');
  const [q, setQ] = useState('');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const users = usePolling<User[]>(
    (signal) =>
      api.users(
        {
          role: (role || undefined) as Role | undefined,
          is_active: active === '' ? undefined : active === 'true',
          q: query || undefined,
          limit: PAGE,
          offset,
        },
        { signal },
      ),
    { key: `admin:users:${role}:${active}:${query}:${offset}` },
  );
  const societies = usePolling<Society[]>((signal) => api.societies({ signal }), { key: 'admin:societies' });
  const flats = usePolling<Flat[]>((signal) => api.flats(undefined, { signal }), { key: 'admin:flats:' });
  const socList = societies.data ?? [];
  const flatList = flats.data ?? [];

  const [creating, setCreating] = useState(false);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [tempPw, setTempPw] = useState<{ username: string; password: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [bindSociety, setBindSociety] = useState('');
  const [bindFlat, setBindFlat] = useState('');
  const act = useAction();

  const open = (d: Dialog) => {
    act.clearError();
    setNotice(null);
    if (d?.kind === 'bind') {
      setBindSociety(d.user.society_id ? String(d.user.society_id) : '');
      setBindFlat(d.user.flat_id ? String(d.user.flat_id) : '');
    }
    setDialog(d);
  };

  const wrap = (fn: () => Promise<unknown>) => async () => {
    try {
      await fn();
    } catch (err) {
      throw new Error(adminErrorMessage(err));
    }
  };

  const confirm = async () => {
    if (!dialog) return;
    const u = dialog.user;
    if (dialog.kind === 'reset') {
      const password = generateTempPassword();
      const ok = await act.run(wrap(() => api.resetUserPassword(u.id, password)));
      if (ok) {
        setDialog(null);
        setTempPw({ username: u.username, password });
      }
      return;
    }
    const ok = await act.run(
      wrap(() => {
        if (dialog.kind === 'deactivate') return api.updateUser(u.id, { is_active: false });
        if (dialog.kind === 'logout') return api.logoutUserEverywhere(u.id);
        return api.setUserBinding(u.id, {
          flat_id: u.role === 'resident' && bindFlat ? bindFlat : null,
          society_id: u.role === 'maintenance' && bindSociety ? bindSociety : null,
        });
      }),
    );
    if (ok) {
      setNotice(
        dialog.kind === 'deactivate'
          ? `${u.username} was deactivated.`
          : dialog.kind === 'logout'
            ? `${u.username} was signed out everywhere.`
            : `Binding updated for ${u.username}.`,
      );
      setDialog(null);
      users.refetch();
    }
  };

  const reactivate = async (u: User) => {
    setNotice(null);
    const ok = await act.run(wrap(() => api.updateUser(u.id, { is_active: true })));
    if (ok) {
      setNotice(`${u.username} was reactivated.`);
      users.refetch();
    }
  };

  const bindLabel = (u: User): string => {
    if (u.role === 'admin') return 'All';
    if (u.role === 'maintenance') return socList.find((s) => String(s.id) === String(u.society_id))?.name ?? (u.society_id ? 'Unknown society' : 'Not linked');
    const f = flatList.find((x) => String(x.id) === String(u.flat_id));
    return f ? f.unit_number : u.flat_id ? 'Unknown flat' : 'Not linked';
  };

  return (
    <AdminOnly what="user administration">
      <div className="space-y-6">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-3xl font-semibold">Users</h1>
            <p className="mt-1 text-ink-2">Create accounts, link residents to flats and manage access.</p>
          </div>
          <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>New user</button>
        </div>

        {creating ? (
          <CreateUserForm
            societies={socList}
            flats={flatList}
            onCancel={() => setCreating(false)}
            onDone={() => {
              setCreating(false);
              setNotice('User created.');
              users.refetch();
            }}
          />
        ) : null}

        <form
          role="search"
          aria-label="Filter users"
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            setOffset(0);
            setQuery(q.trim());
          }}
        >
          <div>
            <label className="label" htmlFor="uf-q">Search</label>
            <input id="uf-q" className="field" maxLength={50} value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="uf-role">Role</label>
            <select id="uf-role" className="field" value={role} onChange={(e) => { setRole(e.target.value); setOffset(0); }}>
              <option value="">All roles</option>
              {ROLES.map((r) => (<option key={r.value} value={r.value}>{r.label}</option>))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="uf-active">Status</label>
            <select id="uf-active" className="field" value={active} onChange={(e) => { setActive(e.target.value); setOffset(0); }}>
              <option value="">Any</option>
              <option value="true">Active</option>
              <option value="false">Deactivated</option>
            </select>
          </div>
          <button type="submit" className="btn">Search</button>
        </form>

        {notice ? <p role="status" className="rounded-md border border-safe bg-safe-soft p-3 text-sm">{notice}</p> : null}
        {act.error && !dialog ? <FormError id="users-action-error" message={act.error} /> : null}

        <DataState
          data={users.data}
          loading={users.loading}
          error={users.error}
          onRetry={users.refetch}
          emptyTitle="No users match"
          emptyHint="Try different filters."
          loadingLabel="Loading users"
        >
          {(rows) => (
            <>
              <div className="overflow-x-auto rounded-lg border border-line">
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">Users</caption>
                  <thead className="bg-panel text-ink-2">
                    <tr>
                      <th scope="col" className="p-3">Username</th>
                      <th scope="col" className="p-3">Name</th>
                      <th scope="col" className="p-3">Role</th>
                      <th scope="col" className="p-3">Linked to</th>
                      <th scope="col" className="p-3">Status</th>
                      <th scope="col" className="p-3">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((u) => (
                      <tr key={String(u.id)} className="border-t border-line">
                        <th scope="row" className="p-3 font-semibold">{u.username}</th>
                        <td className="p-3">{u.name}</td>
                        <td className="p-3">{u.role}</td>
                        <td className="p-3">{bindLabel(u)}</td>
                        <td className="p-3">
                          <StatusBadge tone={u.is_active === false ? 'neutral' : 'safe'}>{u.is_active === false ? 'Deactivated' : 'Active'}</StatusBadge>
                        </td>
                        <td className="p-3">
                          <div className="flex flex-wrap gap-2">
                            {u.role !== 'admin' ? (
                              <button type="button" className="btn !min-h-[32px]" aria-label={`Set binding for ${u.username}`} onClick={() => open({ kind: 'bind', user: u })}>Binding</button>
                            ) : null}
                            <button type="button" className="btn !min-h-[32px]" aria-label={`Reset password for ${u.username}`} onClick={() => open({ kind: 'reset', user: u })}>Reset password</button>
                            <button type="button" className="btn !min-h-[32px]" aria-label={`Log out ${u.username} everywhere`} onClick={() => open({ kind: 'logout', user: u })}>Log out everywhere</button>
                            {u.is_active === false ? (
                              <button type="button" className="btn !min-h-[32px]" aria-label={`Reactivate ${u.username}`} disabled={act.busy} onClick={() => void reactivate(u)}>Reactivate</button>
                            ) : (
                              <button type="button" className="btn !min-h-[32px]" aria-label={`Deactivate ${u.username}`} onClick={() => open({ kind: 'deactivate', user: u })}>Deactivate</button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <nav aria-label="Pagination" className="flex items-center gap-3">
                <button type="button" className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
                <span className="text-sm text-ink-2">Showing {offset + 1}–{offset + rows.length}</span>
                <button type="button" className="btn" disabled={rows.length < PAGE} onClick={() => setOffset(offset + PAGE)}>Next</button>
              </nav>
            </>
          )}
        </DataState>

        {dialog?.kind === 'deactivate' ? (
          <ConfirmDialog
            title={`Deactivate ${dialog.user.username}?`}
            description="They are signed out everywhere and can no longer log in. You can reactivate the account later."
            confirmLabel="Deactivate"
            danger
            busy={act.busy}
            error={act.error}
            onCancel={() => setDialog(null)}
            onConfirm={() => void confirm()}
          />
        ) : null}
        {dialog?.kind === 'reset' ? (
          <ConfirmDialog
            title={`Reset password for ${dialog.user.username}?`}
            description="A random temporary password is set and shown to you once. All their sessions are signed out and any lockout is cleared."
            confirmLabel="Reset password"
            danger
            busy={act.busy}
            error={act.error}
            onCancel={() => setDialog(null)}
            onConfirm={() => void confirm()}
          />
        ) : null}
        {dialog?.kind === 'logout' ? (
          <ConfirmDialog
            title={`Sign ${dialog.user.username} out everywhere?`}
            description="Every active session and live connection for this user is revoked."
            confirmLabel="Log out everywhere"
            danger
            busy={act.busy}
            error={act.error}
            onCancel={() => setDialog(null)}
            onConfirm={() => void confirm()}
          />
        ) : null}
        {dialog?.kind === 'bind' ? (
          <ConfirmDialog
            title={`Set binding for ${dialog.user.username}`}
            description={dialog.user.role === 'resident' ? 'Residents are linked to one flat.' : 'Maintenance staff are linked to one society.'}
            confirmLabel="Save binding"
            busy={act.busy}
            error={act.error}
            onCancel={() => setDialog(null)}
            onConfirm={() => void confirm()}
          >
            <BindingSelects
              idPrefix="bind"
              role={dialog.user.role}
              societies={socList}
              flats={flatList}
              societyId={bindSociety}
              flatId={bindFlat}
              onSociety={setBindSociety}
              onFlat={setBindFlat}
            />
          </ConfirmDialog>
        ) : null}
        {tempPw ? <TempPasswordDialog username={tempPw.username} password={tempPw.password} onClose={() => setTempPw(null)} /> : null}
      </div>
    </AdminOnly>
  );
}
