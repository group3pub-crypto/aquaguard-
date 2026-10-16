import Link from 'next/link';

export default function NotFound() {
  return (
    <main className="mx-auto max-w-xl p-6 py-24 text-center">
      <p className="text-sm font-semibold text-ink-2">404</p>
      <h1 className="mt-2 text-2xl font-semibold">Page not found</h1>
      <p className="mt-2 text-ink-2">The page you are looking for does not exist.</p>
      <Link href="/" className="btn btn-primary mt-6">
        Back to overview
      </Link>
    </main>
  );
}
