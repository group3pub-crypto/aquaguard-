import { LoadingState } from '@/components/States';

export default function Loading() {
  return (
    <main className="mx-auto max-w-xl p-6">
      <LoadingState label="Loading page" />
    </main>
  );
}
