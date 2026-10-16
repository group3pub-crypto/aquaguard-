'use client';
import { ErrorState } from '@/components/States';

export default function Error({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <main className="mx-auto max-w-xl p-6">
      <ErrorState title="Something went wrong" message={error.message} onRetry={reset} />
    </main>
  );
}
