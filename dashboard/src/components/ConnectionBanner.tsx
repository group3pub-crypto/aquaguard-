'use client';
import { useLive } from '@/context/LiveProvider';
import { WS_FORBIDDEN } from '@/lib/wsAuth';

export function ConnectionBanner() {
  const { connection, notice, reconnect } = useLive();
  if (connection === 'disconnected') {
    const forbidden = notice?.code === WS_FORBIDDEN;
    return (
      <div role="alert" className="border-b border-error bg-error-soft px-4 py-2 text-sm">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3">
          <span className="font-semibold text-error">Live feed disconnected.</span>
          <span>{notice ? notice.message : 'Data may be out of date.'}</span>
          {forbidden ? null : (
            <button type="button" className="btn !min-h-[32px]" onClick={reconnect}>
              Reconnect
            </button>
          )}
        </div>
      </div>
    );
  }
  if (connection === 'reconnecting' || connection === 'connecting') {
    return (
      <div role="status" className="border-b border-warn bg-warn-soft px-4 py-2 text-sm">
        <div className="mx-auto max-w-7xl">
          {connection === 'connecting' ? 'Connecting to live feed…' : 'Live feed interrupted, reconnecting…'}
          {notice ? <span className="ml-2">{notice.message}</span> : null}
        </div>
      </div>
    );
  }
  return null;
}
