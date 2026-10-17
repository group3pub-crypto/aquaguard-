'use client';
import { useEffect, useId, useRef, useState } from 'react';
import type { ReactNode } from 'react';

export interface ConfirmDialogProps {
  title: string;
  description: string;
  confirmLabel: string;
  danger?: boolean;
  /** When set, shows a textarea; confirm is disabled until it is non-empty if reasonRequired. */
  reasonLabel?: string;
  reasonRequired?: boolean;
  busy?: boolean;
  /** Extra controls (e.g. a select) rendered under the description. */
  children?: ReactNode;
  confirmDisabled?: boolean;
  error?: string | null;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}

export function ConfirmDialog(p: ConfirmDialogProps) {
  const titleId = useId();
  const descId = useId();
  const [reason, setReason] = useState('');
  const ref = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    cancelRef.current?.focus();
    return () => previous?.focus?.();
  }, []);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      p.onCancel();
      return;
    }
    if (e.key !== 'Tab' || !ref.current) return;
    const focusable = ref.current.querySelectorAll<HTMLElement>('button:not(:disabled), textarea, input, select');
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const disabled = p.busy || p.confirmDisabled || (p.reasonLabel && p.reasonRequired ? reason.trim().length === 0 : false);

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim p-4">
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        onKeyDown={onKeyDown}
        className="w-full max-w-md rounded-lg border border-line bg-canvas p-6"
      >
        <h2 id={titleId} className="text-lg font-semibold">
          {p.title}
        </h2>
        <p id={descId} className="mt-2 text-sm text-ink-2">
          {p.description}
        </p>
        {p.children ? <div className="mt-4">{p.children}</div> : null}
        {p.reasonLabel ? (
          <div className="mt-4">
            <label className="label" htmlFor={`${titleId}-reason`}>
              {p.reasonLabel}
            </label>
            <textarea
              id={`${titleId}-reason`}
              className="field py-2"
              rows={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>
        ) : null}
        {p.error ? (
          <p role="alert" className="mt-3 text-sm text-error">
            {p.error}
          </p>
        ) : null}
        <div className="mt-6 flex justify-end gap-2">
          <button ref={cancelRef} type="button" className="btn" onClick={p.onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className={`btn ${p.danger ? 'btn-danger' : 'btn-primary'}`}
            disabled={disabled}
            onClick={() => p.onConfirm(reason.trim())}
          >
            {p.busy ? 'Working…' : p.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
