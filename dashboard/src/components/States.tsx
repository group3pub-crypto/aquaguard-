import type { ReactNode } from 'react';

export function LoadingState({ label = 'Loading' }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-3 p-6 text-ink-2">
      <span
        aria-hidden="true"
        className="h-4 w-4 animate-spin rounded-full border-2 border-line border-t-action"
      />
      <span>{label}…</span>
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-line bg-panel p-8 text-center">
      <p className="font-semibold">{title}</p>
      {hint ? <p className="mt-1 text-sm text-ink-2">{hint}</p> : null}
    </div>
  );
}

export function ErrorState({
  title = 'Could not load data',
  message,
  onRetry,
}: {
  title?: string;
  message?: string;
  onRetry?: () => void;
}) {
  return (
    <div role="alert" className="rounded-lg border border-error bg-error-soft p-6">
      <p className="font-semibold text-error">{title}</p>
      {message ? <p className="mt-1 text-sm text-ink">{message}</p> : null}
      {onRetry ? (
        <button type="button" className="btn mt-4" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

/** Standard loading / error / empty / content switch for any data view. */
export function DataState<T>({
  data,
  loading,
  error,
  onRetry,
  isEmpty,
  emptyTitle,
  emptyHint,
  loadingLabel,
  children,
}: {
  data: T | null;
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
  isEmpty?: (d: T) => boolean;
  emptyTitle: string;
  emptyHint?: string;
  loadingLabel?: string;
  children: (d: T) => ReactNode;
}) {
  if (data === null && loading) return <LoadingState label={loadingLabel} />;
  if (data === null && error) return <ErrorState message={error.message} onRetry={onRetry} />;
  if (data === null) return <LoadingState label={loadingLabel} />;
  if (isEmpty ? isEmpty(data) : Array.isArray(data) && data.length === 0) {
    return <EmptyState title={emptyTitle} hint={emptyHint} />;
  }
  return <>{children(data)}</>;
}
