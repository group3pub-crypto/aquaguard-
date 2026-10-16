export function Sparkline({ values, label }: { values: number[]; label?: string }) {
  const w = 120;
  const h = 32;
  if (values.length < 2) {
    return <div className="h-8 text-xs text-ink-2" aria-hidden="true" />;
  }
  const max = Math.max(...values, 0.1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const pts = values
    .map((v, i) => `${((i / (values.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)}`)
    .join(' ');
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className="h-8 w-full"
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      preserveAspectRatio="none"
    >
      <polyline points={pts} fill="none" stroke="var(--action)" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}
