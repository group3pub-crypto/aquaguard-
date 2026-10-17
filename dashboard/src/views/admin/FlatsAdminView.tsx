'use client';
import { useState } from 'react';
import type { FormEvent } from 'react';
import { api } from '@/lib/api';
import { validateFlat, type FlatForm } from '@/lib/validation';
import type { Flat, Society } from '@/lib/types';
import { usePolling } from '@/hooks/usePolling';
import { useAction } from '@/hooks/useAction';
import { AdminOnly } from '@/components/admin/AdminOnly';
import { FormError, SelectField, TextField } from '@/components/admin/Fields';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataState } from '@/components/States';
import { adminErrorMessage } from './shared';

function FlatFormPanel({
  societies,
  editing,
  defaultSociety,
  onDone,
  onCancel,
}: {
  societies: Society[];
  editing: Flat | null;
  defaultSociety: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [v, setV] = useState<FlatForm>(
    editing
      ? {
          society_id: String(editing.society_id),
          unit_number: editing.unit_number,
          floor: editing.floor === null ? '' : String(editing.floor),
          residents: String(editing.residents),
          sensor_id: editing.sensor_id,
        }
      : { society_id: defaultSociety, unit_number: '', floor: '', residents: '0', sensor_id: '' },
  );
  const [errors, setErrors] = useState<ReturnType<typeof validateFlat>>({});
  const { busy, error, run } = useAction();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const errs = validateFlat(v, !editing);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const floor = v.floor.trim() ? Number(v.floor) : null;
    const residents = v.residents.trim() ? Number(v.residents) : 0;
    const sensor = v.sensor_id.trim();
    const ok = await run(async () => {
      try {
        if (editing) await api.updateFlat(editing.id, { floor, residents, ...(sensor ? { sensor_id: sensor } : {}) });
        else
          await api.createFlat({
            society_id: v.society_id,
            unit_number: v.unit_number.trim(),
            floor,
            residents,
            sensor_id: sensor || null,
          });
      } catch (err) {
        throw new Error(adminErrorMessage(err));
      }
    });
    if (ok) onDone();
  };

  return (
    <form onSubmit={submit} noValidate aria-labelledby="flat-form-h" className="card space-y-4">
      <h2 id="flat-form-h" className="text-xl font-semibold">
        {editing ? `Edit flat ${editing.unit_number}` : 'New flat'}
      </h2>
      <SelectField
        id="flat-society"
        label="Society"
        required
        disabled={!!editing}
        value={v.society_id}
        error={errors.society_id}
        options={[{ value: '', label: 'Choose a society…' }, ...societies.map((s) => ({ value: String(s.id), label: s.name }))]}
        onChange={(society_id) => setV({ ...v, society_id })}
      />
      <TextField
        id="flat-unit"
        label="Unit number"
        required
        disabled={!!editing}
        maxLength={50}
        hint="Letters, digits, hyphens and underscores."
        value={v.unit_number}
        error={errors.unit_number}
        onChange={(unit_number) => setV({ ...v, unit_number })}
      />
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField id="flat-floor" label="Floor" inputMode="numeric" hint="-5 to 300, optional." value={v.floor} error={errors.floor} onChange={(floor) => setV({ ...v, floor })} />
        <TextField id="flat-residents" label="Residents" inputMode="numeric" hint="0 to 50." value={v.residents} error={errors.residents} onChange={(residents) => setV({ ...v, residents })} />
      </div>
      <TextField
        id="flat-sensor"
        label="Sensor id"
        maxLength={100}
        hint="Device id of the ESP32 sensor. Must be unique."
        value={v.sensor_id}
        error={errors.sensor_id}
        onChange={(sensor_id) => setV({ ...v, sensor_id })}
      />
      <FormError id="flat-form-error" message={error} />
      <div className="flex gap-2">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Saving…' : editing ? 'Save changes' : 'Create flat'}
        </button>
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function FlatsAdminView() {
  const societies = usePolling<Society[]>((signal) => api.societies({ signal }), { key: 'admin:societies' });
  const [filter, setFilter] = useState('');
  const flats = usePolling<Flat[]>((signal) => api.flats(filter || undefined, { signal }), { key: `admin:flats:${filter}` });
  const [form, setForm] = useState<{ editing: Flat | null } | null>(null);
  const [deleting, setDeleting] = useState<Flat | null>(null);
  const del = useAction();
  const socList = societies.data ?? [];
  const socName = (id: unknown) => socList.find((s) => String(s.id) === String(id))?.name ?? '–';

  const confirmDelete = async () => {
    if (!deleting) return;
    const target = deleting;
    const ok = await del.run(async () => {
      try {
        await api.deleteFlat(target.id);
      } catch (err) {
        throw new Error(adminErrorMessage(err));
      }
    });
    if (ok) {
      setDeleting(null);
      flats.refetch();
    }
  };

  return (
    <AdminOnly what="flat administration">
      <div className="space-y-6">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-3xl font-semibold">Flats</h1>
            <p className="mt-1 text-ink-2">Register flats and assign their sensors.</p>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label className="label" htmlFor="flat-filter">Society</label>
              <select id="flat-filter" className="field" value={filter} onChange={(e) => setFilter(e.target.value)}>
                <option value="">All societies</option>
                {socList.map((s) => (
                  <option key={String(s.id)} value={String(s.id)}>{s.name}</option>
                ))}
              </select>
            </div>
            <button type="button" className="btn btn-primary" onClick={() => setForm({ editing: null })}>
              New flat
            </button>
          </div>
        </div>

        {form ? (
          <FlatFormPanel
            key={form.editing ? String(form.editing.id) : 'new'}
            societies={socList}
            editing={form.editing}
            defaultSociety={filter}
            onCancel={() => setForm(null)}
            onDone={() => {
              setForm(null);
              flats.refetch();
            }}
          />
        ) : null}

        <DataState
          data={flats.data}
          loading={flats.loading}
          error={flats.error}
          onRetry={flats.refetch}
          emptyTitle="No flats"
          emptyHint="Create a flat to start monitoring it."
          loadingLabel="Loading flats"
        >
          {(rows) => (
            <div className="overflow-x-auto rounded-lg border border-line">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Flats</caption>
                <thead className="bg-panel text-ink-2">
                  <tr>
                    <th scope="col" className="p-3">Unit</th>
                    <th scope="col" className="p-3">Society</th>
                    <th scope="col" className="p-3">Floor</th>
                    <th scope="col" className="p-3">Residents</th>
                    <th scope="col" className="p-3">Sensor</th>
                    <th scope="col" className="p-3">Valve</th>
                    <th scope="col" className="p-3">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((f) => (
                    <tr key={String(f.id)} className="border-t border-line">
                      <th scope="row" className="p-3 font-semibold">{f.unit_number}</th>
                      <td className="p-3">{socName(f.society_id)}</td>
                      <td className="p-3">{f.floor ?? '–'}</td>
                      <td className="p-3">{f.residents}</td>
                      <td className="p-3">{f.sensor_id}</td>
                      <td className="p-3">{f.valve_state}</td>
                      <td className="p-3">
                        <div className="flex flex-wrap gap-2">
                          <button type="button" className="btn !min-h-[32px]" aria-label={`Edit flat ${f.unit_number}`} onClick={() => setForm({ editing: f })}>
                            Edit
                          </button>
                          <button
                            type="button"
                            className="btn !min-h-[32px]"
                            aria-label={`Delete flat ${f.unit_number}`}
                            onClick={() => {
                              del.clearError();
                              setDeleting(f);
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
            title={`Delete flat ${deleting.unit_number}?`}
            description="This permanently removes the flat and its link to the sensor. The valve must be open first."
            confirmLabel="Delete flat"
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
