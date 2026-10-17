'use client';
import { useState } from 'react';
import type { FormEvent } from 'react';
import { api } from '@/lib/api';
import { fmtTime } from '@/lib/format';
import { validateSociety } from '@/lib/validation';
import type { Society } from '@/lib/types';
import { usePolling } from '@/hooks/usePolling';
import { useAction } from '@/hooks/useAction';
import { AdminOnly } from '@/components/admin/AdminOnly';
import { FormError, TextField } from '@/components/admin/Fields';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataState } from '@/components/States';
import { adminErrorMessage } from './shared';

interface FormState {
  name: string;
  slug: string;
  address: string;
}
const EMPTY: FormState = { name: '', slug: '', address: '' };

function SocietyForm({ editing, onDone, onCancel }: { editing: Society | null; onDone: () => void; onCancel: () => void }) {
  const [v, setV] = useState<FormState>(
    editing ? { name: editing.name, slug: editing.slug, address: editing.address ?? '' } : EMPTY,
  );
  const [errors, setErrors] = useState<ReturnType<typeof validateSociety>>({});
  const { busy, error, run } = useAction();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const errs = validateSociety(v, !editing);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const ok = await run(async () => {
      try {
        if (editing) await api.updateSociety(editing.id, { name: v.name.trim(), address: v.address.trim() || null });
        else await api.createSociety({ name: v.name.trim(), slug: v.slug.trim(), address: v.address.trim() || null });
      } catch (err) {
        throw new Error(adminErrorMessage(err));
      }
    });
    if (ok) onDone();
  };

  return (
    <form onSubmit={submit} noValidate aria-labelledby="society-form-h" className="card space-y-4">
      <h2 id="society-form-h" className="text-xl font-semibold">
        {editing ? `Edit ${editing.name}` : 'New society'}
      </h2>
      <TextField id="soc-name" label="Name" required maxLength={200} value={v.name} error={errors.name} onChange={(name) => setV({ ...v, name })} />
      <TextField
        id="soc-slug"
        label="Slug"
        required
        disabled={!!editing}
        maxLength={64}
        hint="Lowercase letters, digits and single hyphens. Cannot be changed later."
        value={v.slug}
        error={errors.slug}
        onChange={(slug) => setV({ ...v, slug })}
      />
      <TextField id="soc-address" label="Address" maxLength={500} value={v.address} error={errors.address} onChange={(address) => setV({ ...v, address })} />
      <FormError id="society-form-error" message={error} />
      <div className="flex gap-2">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Saving…' : editing ? 'Save changes' : 'Create society'}
        </button>
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function SocietiesAdminView() {
  const list = usePolling<Society[]>((signal) => api.societies({ signal }), { key: 'admin:societies' });
  const [form, setForm] = useState<{ editing: Society | null } | null>(null);
  const [deleting, setDeleting] = useState<Society | null>(null);
  const del = useAction();

  const confirmDelete = async () => {
    if (!deleting) return;
    const target = deleting;
    const ok = await del.run(async () => {
      try {
        await api.deleteSociety(target.id);
      } catch (err) {
        throw new Error(adminErrorMessage(err));
      }
    });
    if (ok) {
      setDeleting(null);
      list.refetch();
    }
  };

  return (
    <AdminOnly what="society administration">
      <div className="space-y-6">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-3xl font-semibold">Societies</h1>
            <p className="mt-1 text-ink-2">Create and maintain the housing societies AquaGuard monitors.</p>
          </div>
          <button type="button" className="btn btn-primary" onClick={() => setForm({ editing: null })}>
            New society
          </button>
        </div>

        {form ? (
          <SocietyForm
            key={form.editing ? String(form.editing.id) : 'new'}
            editing={form.editing}
            onCancel={() => setForm(null)}
            onDone={() => {
              setForm(null);
              list.refetch();
            }}
          />
        ) : null}

        <DataState
          data={list.data}
          loading={list.loading}
          error={list.error}
          onRetry={list.refetch}
          emptyTitle="No societies yet"
          emptyHint="Create the first society to start adding flats."
          loadingLabel="Loading societies"
        >
          {(rows) => (
            <div className="overflow-x-auto rounded-lg border border-line">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Societies</caption>
                <thead className="bg-panel text-ink-2">
                  <tr>
                    <th scope="col" className="p-3">Name</th>
                    <th scope="col" className="p-3">Slug</th>
                    <th scope="col" className="p-3">Address</th>
                    <th scope="col" className="p-3">Flats</th>
                    <th scope="col" className="p-3">Created</th>
                    <th scope="col" className="p-3">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={String(s.id)} className="border-t border-line">
                      <th scope="row" className="p-3 font-semibold">{s.name}</th>
                      <td className="p-3">{s.slug}</td>
                      <td className="p-3">{s.address ?? '–'}</td>
                      <td className="p-3">{s.total_flats}</td>
                      <td className="p-3">{fmtTime(s.created_at)}</td>
                      <td className="p-3">
                        <div className="flex flex-wrap gap-2">
                          <button type="button" className="btn !min-h-[32px]" aria-label={`Edit ${s.name}`} onClick={() => setForm({ editing: s })}>
                            Edit
                          </button>
                          <button
                            type="button"
                            className="btn !min-h-[32px]"
                            aria-label={`Delete ${s.name}`}
                            onClick={() => {
                              del.clearError();
                              setDeleting(s);
                            }}
                          >
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </DataState>

        {deleting ? (
          <ConfirmDialog
            title={`Delete ${deleting.name}?`}
            description="This permanently removes the society. It can only be deleted once it has no flats."
            confirmLabel="Delete society"
            danger
            busy={del.busy}
            error={del.error}
            onCancel={() => setDeleting(null)}
            onConfirm={() => void confirmDelete()}
          />
        ) : null}
      </div>
    </AdminOnly>
  );
}
