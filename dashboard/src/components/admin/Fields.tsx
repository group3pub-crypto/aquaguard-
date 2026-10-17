import type { ReactNode } from 'react';

interface BaseProps {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  required?: boolean;
}

function describedBy(id: string, hint?: string, error?: string): string | undefined {
  const ids = [hint ? `${id}-hint` : '', error ? `${id}-err` : ''].filter(Boolean).join(' ');
  return ids || undefined;
}

function Shell({ id, label, hint, error, required, children }: BaseProps & { children: ReactNode }) {
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
        {required ? <span aria-hidden="true"> *</span> : null}
      </label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="mt-1 text-xs text-ink-2">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-err`} className="mt-1 text-sm text-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function TextField(
  p: BaseProps & {
    value: string;
    onChange: (v: string) => void;
    type?: 'text' | 'password' | 'number';
    autoComplete?: string;
    maxLength?: number;
    disabled?: boolean;
    inputMode?: 'numeric' | 'text';
  },
) {
  return (
    <Shell {...p}>
      <input
        id={p.id}
        type={p.type ?? 'text'}
        className="field"
        value={p.value}
        required={p.required}
        disabled={p.disabled}
        maxLength={p.maxLength}
        autoComplete={p.autoComplete ?? 'off'}
        inputMode={p.inputMode}
        aria-invalid={p.error ? true : undefined}
        aria-describedby={describedBy(p.id, p.hint, p.error)}
        onChange={(e) => p.onChange(e.target.value)}
      />
    </Shell>
  );
}

export function SelectField(
  p: BaseProps & {
    value: string;
    onChange: (v: string) => void;
    options: { value: string; label: string }[];
    disabled?: boolean;
  },
) {
  return (
    <Shell {...p}>
      <select
        id={p.id}
        className="field"
        value={p.value}
        required={p.required}
        disabled={p.disabled}
        aria-invalid={p.error ? true : undefined}
        aria-describedby={describedBy(p.id, p.hint, p.error)}
        onChange={(e) => p.onChange(e.target.value)}
      >
        {p.options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </Shell>
  );
}

/** Server-side error summary, announced to assistive tech. */
export function FormError({ id, message }: { id: string; message: string | null }) {
  return message ? (
    <p id={id} role="alert" className="rounded-md border border-error bg-error-soft p-3 text-sm text-error">
      {message}
    </p>
  ) : null;
}
