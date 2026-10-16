import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { AlertsView } from './AlertsView';
import { DemoView } from './DemoView';
import { OverviewView } from './OverviewView';
import { WithAuth, mockFetch, sampleAlert } from '@/test/utils';

vi.mock('@/components/FlowChart', () => ({ FlowChart: () => <div>chart</div> }));

describe('AlertsView', () => {
  it('shows loading, then the empty state', async () => {
    mockFetch(() => ({ body: [] }));
    render(<WithAuth role="admin"><AlertsView /></WithAuth>);
    expect(screen.getByRole('status')).toHaveTextContent(/loading alerts/i);
    expect(await screen.findByText(/no active alerts/i)).toBeInTheDocument();
  });

  it('shows an error with retry that recovers', async () => {
    let fail = true;
    mockFetch(() => (fail ? { status: 500, body: { detail: 'db down' } } : { body: [sampleAlert] }));
    render(<WithAuth role="admin"><AlertsView /></WithAuth>);
    expect(await screen.findByRole('alert')).toHaveTextContent('db down');
    fail = false;
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Possible burst pipe')).toBeInTheDocument();
  });

  it('has an accessible tablist with arrow-key navigation and a labelled tabpanel', async () => {
    mockFetch(() => ({ body: [sampleAlert, { ...sampleAlert, id: 8, state: 'resolved', message: 'Old one' }] }));
    render(<WithAuth role="resident"><AlertsView /></WithAuth>);
    await screen.findByText('Possible burst pipe');
    const tabs = screen.getAllByRole('tab');
    expect(screen.getByRole('tablist')).toHaveAccessibleName('Alert state');
    const active = screen.getByRole('tab', { name: /^Active/ });
    expect(active).toHaveAttribute('aria-selected', 'true');
    expect(active).toHaveAttribute('tabindex', '0');
    expect(tabs.filter((t) => t.getAttribute('tabindex') === '-1')).toHaveLength(tabs.length - 1);
    active.focus();
    await userEvent.keyboard('{ArrowRight}');
    const ack = screen.getByRole('tab', { name: /^Acknowledged/ });
    expect(ack).toHaveAttribute('aria-selected', 'true');
    expect(ack).toHaveFocus();
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', ack.id);
    await userEvent.click(screen.getByRole('tab', { name: /^Resolved/ }));
    expect(screen.getByText('Old one')).toBeInTheDocument();
    expect(screen.queryByText('Possible burst pipe')).not.toBeInTheDocument();
  });
});

describe('OverviewView', () => {
  const societies = [{ id: 1, name: 'Green Meadows', slug: 'gm', total_flats: 2 }];
  const stats = { total_consumption: 1200, losses: 30, water_saved: 80, active_alerts: 1 };
  const flat = { id: 3, unit_number: 'A-301', floor: 3, residents: 2, valve_state: 'open', baseline_status: 'ready', sensor_status: 'online', current_flow: 1.5 };

  it('renders stats and flat tiles with alert status', async () => {
    mockFetch((url) => {
      if (url.pathname.endsWith('/societies')) return { body: societies };
      if (url.pathname.endsWith('/stats')) return { body: stats };
      if (url.pathname.endsWith('/flats')) return { body: [flat] };
      if (url.pathname.endsWith('/alerts')) return { body: [sampleAlert] };
      return undefined;
    });
    render(<WithAuth role="resident"><OverviewView /></WithAuth>);
    expect(await screen.findByText('Flat A-301')).toBeInTheDocument();
    expect(await screen.findByText('Total consumption')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('Alert')).toBeInTheDocument());
    expect(screen.getByRole('link', { name: /Flat A-301/ })).toHaveAttribute('href', '/flat/3');
  });

  it('shows an empty state when there are no flats', async () => {
    mockFetch((url) => {
      if (url.pathname.endsWith('/societies')) return { body: societies };
      if (url.pathname.endsWith('/stats')) return { body: stats };
      return { body: [] };
    });
    render(<WithAuth role="resident"><OverviewView /></WithAuth>);
    expect(await screen.findByText('No flats yet')).toBeInTheDocument();
  });

  it('shows an error state with retry when societies fail', async () => {
    mockFetch(() => ({ status: 500, body: { detail: 'nope' } }));
    render(<WithAuth role="resident"><OverviewView /></WithAuth>);
    expect(await screen.findByText('nope')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});

describe('DemoView', () => {
  it('is blocked for non-admins and makes no requests', () => {
    const { fn } = mockFetch(() => ({ body: [] }));
    render(<WithAuth role="maintenance"><DemoView /></WithAuth>);
    expect(screen.getByText(/administrators only/i)).toBeInTheDocument();
    expect(fn).not.toHaveBeenCalled();
  });

  it('lets admins inject a fault via the radiogroup', async () => {
    const { calls } = mockFetch((url) => {
      if (url.pathname.endsWith('/flats')) return { body: [{ id: 3, unit_number: 'A-301', floor: 3, residents: 2, valve_state: 'open', baseline_status: 'ready', sensor_status: 'online', current_flow: 0 }] };
      if (url.pathname.endsWith('/demo/status')) return { body: { active_faults: [], speed: 1, simulator_running: true } };
      return { body: {} };
    });
    render(<WithAuth role="admin"><DemoView /></WithAuth>);
    await screen.findByRole('option', { name: 'Flat A-301' });
    expect(screen.getByRole('radiogroup', { name: 'Fault type' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('radio', { name: 'Burst' }));
    await userEvent.click(screen.getByRole('button', { name: 'Inject fault' }));
    await waitFor(() => expect(calls.some((c) => c.url.pathname.endsWith('/demo/inject-fault'))).toBe(true));
    const inject = calls.find((c) => c.url.pathname.endsWith('/demo/inject-fault'));
    expect(inject?.body).toEqual({ flat_id: '3', fault_type: 'burst' });
  });
});
