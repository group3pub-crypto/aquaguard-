'use client';
import { useMemo } from 'react';
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { buildChartRows } from '@/lib/chart';
import type { Baseline, Forecast, Reading } from '@/lib/types';

const fmtTick = (t: number) => new Date(t).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });

export function FlowChart({
  readings,
  baseline,
  forecast,
}: {
  readings: Reading[];
  baseline: Baseline | null;
  forecast: Forecast | null;
}) {
  const rows = useMemo(() => buildChartRows(readings, baseline, forecast), [readings, baseline, forecast]);
  return (
    <div>
      <div role="img" aria-label="Line chart of flow rate in litres per minute: actual readings, baseline band and 24 hour forecast" className="h-72 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
            <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
            <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={fmtTick} stroke="var(--ink-2)" fontSize={12} />
            <YAxis stroke="var(--ink-2)" fontSize={12} unit="" />
            <Tooltip
              labelFormatter={(t: number) => new Date(t).toLocaleString()}
              contentStyle={{ background: 'var(--canvas)', border: '1px solid var(--line)', borderRadius: 8, color: 'var(--ink)' }}
            />
            <Area type="monotone" dataKey="band" name="Baseline range" stroke="none" fill="var(--safe)" fillOpacity={0.18} isAnimationActive={false} />
            <Area type="monotone" dataKey="forecastBand" name="Forecast range" stroke="none" fill="var(--warn)" fillOpacity={0.15} isAnimationActive={false} />
            <Line type="monotone" dataKey="median" name="Baseline median" stroke="var(--safe)" strokeDasharray="4 4" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="forecast" name="Forecast (L/min)" stroke="var(--warn)" strokeDasharray="2 3" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="actual" name="Actual (L/min)" stroke="var(--action)" strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <ul className="mt-2 flex flex-wrap gap-4 text-xs text-ink-2" aria-label="Chart legend">
        <li><span className="mr-1 inline-block h-0.5 w-4 bg-action align-middle" />Actual</li>
        <li><span className="mr-1 inline-block h-2 w-4 bg-safe opacity-40 align-middle" />Baseline range (p25–p95)</li>
        <li><span className="mr-1 inline-block h-0.5 w-4 bg-warn align-middle" />24h forecast</li>
      </ul>
    </div>
  );
}
