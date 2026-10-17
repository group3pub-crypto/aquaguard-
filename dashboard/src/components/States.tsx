import type { ReactNode } from 'react';
import { ApiError } from '@/lib/api';

export function isForbidden(e: Error | null | undefined): boolean {
  return e instanceof ApiError && e.status === 403;
}

export function isNotFound(e: Error | null | undefined): boolean {
  return e instanceof ApiError && e.status === 404;
}

/** Out-of-scope ids answer 404; we do not reveal whether the thing exists. */
export function NotFoundState({ what = 'item' }: { what?: string }) {
  return (
    <div role="status" className="rounded-lg border border-dashed border-line bg-panel p-8 text-center">
      <p className="font-semibold">Not found</p>
      <p className="mt-1 text-sm text-ink-2">
        This {what} does not exist or is not available to your account.
      </p>
    </div>
  );
}

/** Shown to residents without a flat (and staff without a society): the API returns nothing for them. */
export function UnboundState() {
  return (
    <div role="status" className="mx-auto max-w-xl rounded-lg border border-line bg-panel p-8 text-center">
      <h1 className="text-2xl font-semibold">Your account is not linked to a home yet — contact your administrator</h1>
      <p className="mt-2 text-ink-2">
        Contact your administrator to have it linked. Once that is done you will see your water usage and alerts here.
      </p>
    </div>
  );
}

export function PermissionState({ what }: { what?: string }) {
  return (
    <div role="alert" className="rounded-lg border border-line bg-panel p-6">
      <p className="font-semibold">You don&apos;t have permission</p>
      <p className="mt-1 text-sm text-ink-2">
        {what ? `Your account cannot access ${what}.` : 'Your account cannot access this.'} Ask an administrator if you need access.
      </p>
    </div>
  );
}

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
  if (data === null && isForbidden(error)) return <PermissionState />;
  if (data === null && isNotFound(error)) return <NotFoundState />;
  if (data === null && error) return <ErrorState message={error.message} onRetry={onRetry} />;
  if (data === null) return <LoadingState label={loadingLabel} />;
  if (isEmpty ? isEmpty(data) : Array.isArray(data) && data.length === 0) {
    return <EmptyState title={emptyTitle} hint={emptyHint} />;
  }
  return <>{children(data)}</>;
}
