'use client';
import { useLive } from '@/context/LiveProvider';

export function ConnectionBanner() {
  const { connection, reconnect } = useLive();
  if (connection === 'disconnected') {
    return (
      <div role="alert" className="border-b border-error bg-error-soft px-4 py-2 text-sm">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3">
          <span className="font-semibold text-error">Live feed disconnected.</span>
          <span>Data may be out of date.</span>
          <button type="button" className="btn !min-h-[32px]" onClick={reconnect}>
            Reconnect
          </button>
        </div>
      </div>
    );
  }
  if (connection === 'reconnecting' || connection === 'connecting') {
    return (
      <div role="status" className="border-b border-warn bg-warn-soft px-4 py-2 text-sm">
        <div className="mx-auto max-w-7xl">
          {connection === 'connecting' ? 'Connecting to live feed…' : 'Live feed interrupted, reconnecting…'}
        </div>
      </div>
    );
  }
  return null;
}
