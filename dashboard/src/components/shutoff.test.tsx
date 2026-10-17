import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AlertRow } from './AlertRow';
import { AlertTimeline } from './AlertTimeline';
import { WithAuth } from '@/test/utils';
import { alert as baseAlert } from '@/test/fixtures';
import { describeSource, safeAlertMessage } from '@/lib/format';
import { fmtCountdown, shutoffPhase } from '@/lib/shutoff';
import type { Alert } from '@/lib/types';

const NOW = Date.parse('2026-01-01T10:00:00Z');

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => vi.useRealTimers());

const row = (a: Alert, role: 'admin' | 'resident' = 'admin') =>
  render(
    <WithAuth role={role}>
      <ul>
        <AlertRow alert={a} onChanged={vi.fn()} />
      </ul>
    </WithAuth>,
  );

describe('shut-off warning countdown', () => {
  const warned = (secondsAgo: number): Alert => ({
    ...baseAlert,
    state: 'ticket',
    shutoff_warned_at: new Date(NOW - secondsAgo * 1000).toISOString(),
  });

  it('shows a ticking countdown (default 300 s window) with Acknowledge and Override available', () => {
    row(warned(60));
    expect(screen.getByTestId('shutoff-warning')).toHaveTextContent(/will be attempted in\s*4:00/);
    expect(screen.getByRole('button', { name: 'Acknowledge' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Override' })).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.getByTestId('shutoff-warning')).toHaveTextContent(/3:55/);
  });

  it('after the window it says the close is being attempted, never that water is shut off', () => {
    row(warned(400));
    expect(screen.getByTestId('shutoff-warning')).toHaveTextContent(/being attempted/i);
    expect(document.body.textContent ?? '').not.toMatch(/water (has been |was )?shut off/i);
  });

  it('warns that the device never confirmed when the backend gave up', () => {
    row({ ...warned(400), shutoff_gave_up_at: new Date(NOW - 10_000).toISOString() });
    expect(screen.getByRole('alert')).toHaveTextContent(/not confirmed by the device/i);
    expect(screen.getByRole('alert')).toHaveTextContent(/check the flat manually/i);
  });

  it('residents get guidance but no staff-only buttons', () => {
    row(warned(30), 'resident');
    expect(screen.getByTestId('shutoff-warning')).toHaveTextContent(/contact your building maintenance/i);
    expect(screen.queryByRole('button', { name: 'Override' })).not.toBeInTheDocument();
  });

  it('shows no warning without shutoff_warned_at, nor for a confirmed auto_shutoff', () => {
    row(baseAlert);
    expect(screen.queryByTestId('shutoff-warning')).not.toBeInTheDocument();
    expect(shutoffPhase({ ...warned(10), state: 'auto_shutoff' }, NOW, 300)).toBeNull();
    expect(shutoffPhase({ ...warned(10), state: 'resolved' }, NOW, 300)).toBeNull();
  });

  it('formats countdowns', () => {
    expect(fmtCountdown(125)).toBe('2:05');
    expect(fmtCountdown(-3)).toBe('0:00');
  });
});

describe('shut-off wording safety', () => {
  const claim = 'Water to your flat has been shut off to prevent damage.';
  const states = ['nudge', 'alert', 'ticket', 'acknowledged', 'snoozed', 'overridden', 'resolved'];

  it.each(states)('does not display a shut-off claim for state %s', (state) => {
    const a = { ...baseAlert, state, message: claim };
    row(a);
    expect(document.body.textContent ?? '').not.toMatch(/shut off|shut-off/i);
    expect(safeAlertMessage(a)).toMatch(/NOT been confirmed/);
  });

  it('shows the message verbatim only for the confirmed auto_shutoff state', () => {
    row({ ...baseAlert, state: 'auto_shutoff', message: claim });
    expect(screen.getByText(claim)).toBeInTheDocument();
  });

  it('applies the same guard in the timeline', () => {
    render(<AlertTimeline alerts={[{ ...baseAlert, state: 'ticket', message: claim }]} />);
    expect(screen.queryByText(claim)).not.toBeInTheDocument();
  });

  it('does not mangle ordinary messages', () => {
    expect(safeAlertMessage({ state: 'alert', message: 'Possible burst pipe' })).toBe('Possible burst pipe');
  });
});

describe('message_source badge', () => {
  it('says AI-written ONLY for anthropic', () => {
    expect(describeSource('anthropic')).toBe('AI-written');
    expect(describeSource('template')).toBe('Template');
    expect(describeSource('template_fallback')).not.toMatch(/AI-written/);
    expect(describeSource(null)).not.toMatch(/AI-written/);
    expect(describeSource('some-model')).not.toMatch(/AI-written/);
  });

  it('renders the subtle source label on the row', () => {
    row({ ...baseAlert, message_source: 'anthropic' });
    expect(screen.getByText(/AI-written/)).toBeInTheDocument();
  });
});
