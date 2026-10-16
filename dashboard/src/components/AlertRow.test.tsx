import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { AlertRow } from './AlertRow';
import { ValveControls } from './ValveControls';
import { setToken } from '@/lib/session';
import { WithAuth, mockFetch, sampleAlert } from '@/test/utils';
import { IDS } from '@/test/fixtures';
import type { Alert } from '@/lib/types';

const alert = sampleAlert as Alert;

function setup(role: 'admin' | 'maintenance' = 'admin') {
  setToken('jwt-123');
  const onChanged = vi.fn();
  const fetchMock = mockFetch(() => ({ body: { ok: true } }));
  render(
    <WithAuth role={role} username="carol">
      <AlertRow alert={alert} onChanged={onChanged} />
    </WithAuth>,
  );
  return { onChanged, ...fetchMock };
}

describe('alert row actions', () => {
  it('acknowledges using the logged-in user and token', async () => {
    const { calls, onChanged } = setup();
    await userEvent.click(screen.getByRole('button', { name: 'Acknowledge' }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(calls[0]?.url.pathname).toBe(`/api/v1/alerts/${IDS.alert}/acknowledge`);
    expect(calls[0]?.init.method).toBe('POST');
    expect(calls[0]?.body).toEqual({ acknowledged_by: 'carol' });
    expect(calls[0]?.headers.Authorization).toBe('Bearer jwt-123');
  });

  it('snoozes with the chosen duration', async () => {
    const { calls } = setup('maintenance');
    await userEvent.selectOptions(screen.getByLabelText('Snooze duration'), '60');
    await userEvent.click(screen.getByRole('button', { name: 'Snooze' }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.url.pathname).toBe(`/api/v1/alerts/${IDS.alert}/snooze`);
    expect(calls[0]?.body).toEqual({ duration_minutes: 60, snoozed_by: 'carol' });
  });

  it('resolves', async () => {
    const { calls } = setup('maintenance');
    await userEvent.click(screen.getByRole('button', { name: 'Resolve' }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.url.pathname).toBe(`/api/v1/alerts/${IDS.alert}/resolve`);
    expect(calls[0]?.body).toEqual({ resolved_by: 'carol' });
  });

  it('requires confirmation and a reason before override', async () => {
    const { calls } = setup();
    await userEvent.click(screen.getByRole('button', { name: 'Override' }));
    const dialog = screen.getByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Override alert' });
    expect(confirm).toBeDisabled();
    expect(calls).toHaveLength(0);
    await userEvent.type(within(dialog).getByLabelText(/reason/i), 'planned maintenance');
    await userEvent.click(confirm);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.url.pathname).toBe(`/api/v1/alerts/${IDS.alert}/override`);
    expect(calls[0]?.body).toEqual({ reason: 'planned maintenance', overridden_by: 'carol' });
  });

  it('cancelling the override dialog sends nothing', async () => {
    const { calls } = setup();
    await userEvent.click(screen.getByRole('button', { name: 'Override' }));
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it('reopens the valve with manual_dashboard source and user identity', async () => {
    const { calls } = setup();
    await userEvent.click(screen.getByRole('button', { name: 'Reopen valve' }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.url.pathname).toBe(`/api/v1/valves/${IDS.flat}/open`);
    expect(calls[0]?.body).toEqual({ source: 'manual_dashboard', issued_by: 'carol' });
  });

  it.each(['nudge', 'alert', 'ticket', 'auto_shutoff', 'snoozed'])('offers Acknowledge for open state %s', (state) => {
    setToken('t');
    mockFetch(() => ({ body: {} }));
    render(
      <WithAuth role="maintenance">
        <AlertRow alert={{ ...alert, state }} onChanged={() => undefined} />
      </WithAuth>,
    );
    expect(screen.getByRole('button', { name: 'Acknowledge' })).toBeInTheDocument();
  });

  it.each([
    ['acknowledged', 'Acknowledge', false],
    ['auto_shutoff', 'Snooze', false],
    ['resolved', 'Resolve', false],
    ['overridden', 'Resolve', false],
    ['acknowledged', 'Resolve', true],
  ])('state %s: button %s present=%s (mirrors backend transitions)', (state, name, present) => {
    mockFetch(() => ({ body: {} }));
    render(
      <WithAuth role="maintenance">
        <AlertRow alert={{ ...alert, state }} onChanged={() => undefined} />
      </WithAuth>,
    );
    expect(!!screen.queryByRole('button', { name })).toBe(present);
  });

  it('labels nudge and auto_shutoff states and tolerates a null message', () => {
    mockFetch(() => ({ body: {} }));
    const { rerender } = render(
      <WithAuth role="resident">
        <AlertRow alert={{ ...alert, state: 'auto_shutoff', message: null, message_source: null }} onChanged={() => undefined} />
      </WithAuth>,
    );
    expect(screen.getByText('Auto shut-off')).toBeInTheDocument();
    rerender(
      <WithAuth role="resident">
        <AlertRow alert={{ ...alert, state: 'nudge' }} onChanged={() => undefined} />
      </WithAuth>,
    );
    expect(screen.getByText('Nudge')).toBeInTheDocument();
  });

  it('surfaces backend errors from an action', async () => {
    setToken('t');
    mockFetch(() => ({ status: 403, body: { detail: 'Not allowed' } }));
    render(
      <WithAuth role="maintenance">
        <AlertRow alert={alert} onChanged={() => undefined} />
      </WithAuth>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Acknowledge' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Not allowed');
  });
});

describe('valve close confirmation', () => {
  it('only closes the valve after confirming, passing the optional reason', async () => {
    const { calls } = mockFetch(() => ({ body: {} }));
    render(
      <WithAuth role="admin" username="root">
        <ValveControls flatId={IDS.flat} valveState="open" />
      </WithAuth>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Close valve' }));
    expect(calls).toHaveLength(0);
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText(/reason/i), 'leak');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Close valve' }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.url.pathname).toBe(`/api/v1/valves/${IDS.flat}/close`);
    expect(calls[0]?.body).toEqual({ source: 'manual_dashboard', issued_by: 'root', reason: 'leak' });
  });
});
