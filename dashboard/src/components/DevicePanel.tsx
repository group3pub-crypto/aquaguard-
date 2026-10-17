import { fmtNumber, fmtTime, humanize } from '@/lib/format';
import type { DeviceInfo } from '@/lib/types';
import { StatusBadge } from './StatusBadge';

/** Human labels for firmware error codes (docs/hardware/README.md, troubleshooting.md). */
export const DEVICE_ERRORS: Record<string, string> = {
  burst: 'Burst-like flow detected by the device',
  leak_cont: 'Continuous flow (possible leak)',
  leak_night: 'Night-time flow (possible leak)',
  close_unconf: 'Valve close not confirmed by flow stop',
  flow_impossible: 'Flow reading impossible (sensor fault?)',
  stuck_line: 'Flow sensor line stuck',
  adc_fault: 'Battery ADC fault',
  bat_crit: 'Battery critically low',
  bat_low: 'Battery low',
  coil_duty: 'Valve coil duty limit reached',
  backfill_drop: 'Buffered readings were dropped after an outage',
  ts_est: 'Device clock estimated (no time sync)',
  store_fail: 'Device storage failure',
  rst_brownout: 'Device restarted after a brown-out',
  rst_wdt: 'Device restarted by watchdog',
};

export function errLabel(code: string): string {
  return DEVICE_ERRORS[code] ?? humanize(code);
}

const SERIOUS = new Set(['burst', 'close_unconf', 'bat_crit', 'flow_impossible', 'store_fail']);

/** Latest device report. `close_unconf` is rendered as a prominent warning. */
export function DevicePanel({ device }: { device: DeviceInfo }) {
  const errs = Array.isArray(device.err) ? device.err.filter((e): e is string => typeof e === 'string') : [];
  return (
    <section aria-labelledby="device-h" className="card">
      <h2 id="device-h" className="mb-4 text-xl font-semibold">
        Device
      </h2>
      {errs.includes('close_unconf') ? (
        <div role="alert" className="mb-4 rounded-md border-2 border-error bg-error-soft p-3 font-semibold text-error">
          Valve close not confirmed by flow stop — check manually
        </div>
      ) : null}
      <dl className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
        <div>
          <dt className="text-ink-2">Firmware</dt>
          <dd className="font-semibold">{device.fw ?? '–'}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Battery</dt>
          <dd className="font-semibold">{typeof device.vbat === 'number' ? `${fmtNumber(device.vbat, 2)} V` : '–'}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Lifetime volume</dt>
          <dd className="font-semibold">{typeof device.tot === 'number' ? `${fmtNumber(device.tot, 0)} L` : '–'}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Valve (as commanded)</dt>
          <dd className="font-semibold">{device.valve_reported ? humanize(device.valve_reported) : '–'}</dd>
        </div>
      </dl>
      <div className="mt-4">
        <h3 className="text-sm text-ink-2">Device reports</h3>
        {errs.length === 0 ? (
          <p className="text-sm">No errors reported.</p>
        ) : (
          <ul className="mt-1 flex flex-wrap gap-2">
            {errs.map((e) => (
              <li key={e}>
                <StatusBadge tone={SERIOUS.has(e) ? 'error' : 'warn'}>{errLabel(e)}</StatusBadge>
              </li>
            ))}
          </ul>
        )}
        {device.updated_at ? <p className="mt-2 text-xs text-ink-2">Reported {fmtTime(device.updated_at)}</p> : null}
      </div>
    </section>
  );
}
