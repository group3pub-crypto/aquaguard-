import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { AlertsView } from './AlertsView';
import { FlatView } from './FlatView';
import { OverviewView } from './OverviewView';
import { WithAuth, mockFetch, sampleAlert } from '@/test/utils';
import * as fx from '@/test/fixtures';

vi.mock('@/components/FlowChart', () => ({ FlowChart: () => <div>chart</div> }));

describe('AlertsView', () => {
  it('shows loading, then the empty state', async () => {
    mockFetch(() => ({ body: [] }));
    render(<WithAuth role="admin"><AlertsView /></WithAuth>);
    expect(screen.getByRole('status')).toHaveTextContent(/loading alerts/i);
    expect(await screen.findByText(/no open alerts/i)).toBeInTheDocument();
  });

  it('never requests the nonexistent state=active (backend answers 422)', async () => {
    const { calls } = mockFetch(() => ({ body: [] }));
    render(<WithAuth role="admin"><AlertsView /></WithAuth>);
    await screen.findByText(/no open alerts/i);
    await userEvent.click(screen.getByRole('tab', { name: /^Resolved/ }));
    for (const c of calls) expect(c.url.searchParams.get('state')).not.toBe('active');
  });

  it('shows a friendly permission state on 403', async () => {
    mockFetch(() => ({ status: 403, body: { detail: 'Insufficient permissions' } }));
    render(<WithAuth role="resident"><AlertsView /></WithAuth>);
    expect(await screen.findByText(/you don.t have permission/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
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
    const active = screen.getByRole('tab', { name: /^Open/ });
    expect(active).toHaveAttribute('aria-selected', 'true');
    expect(active).toHaveAttribute('tabindex', '0');
    expect(tabs.filter((t) => t.getAttribute('tabindex') === '-1')).toHaveLength(tabs.length - 1);
    active.focus();
    await userEvent.keyboard('{ArrowRight}');
    const ack = screen.getByRole('tab', { name: /^Nudge/ });
    expect(ack).toHaveAttribute('aria-selected', 'true');
    expect(ack).toHaveFocus();
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', ack.id);
    await userEvent.click(screen.getByRole('tab', { name: /^Resolved/ }));
    expect(screen.getByText('Old one')).toBeInTheDocument();
    expect(screen.queryByText('Possible burst pipe')).not.toBeInTheDocument();
  });
});

describe('OverviewView', () => {
  const routes = (over: Partial<Record<string, unknown>> = {}) => (url: URL) => {
    const p = url.pathname;
    if (p in over) return { body: over[p] };
    if (p.endsWith('/societies')) return { body: [fx.society] };
    if (p.endsWith('/stats')) return { body: fx.societyStats };
    if (p.endsWith('/flats')) return { body: [fx.flat] };
    if (p.endsWith('/alerts')) return { body: [fx.alert] };
    return undefined;
  };

  it('renders the real stats shape and flat tiles with alert status', async () => {
    mockFetch(routes());
    render(<WithAuth role="resident"><OverviewView /></WithAuth>);
    expect(await screen.findByText('Flat A-301')).toBeInTheDocument();
    expect(await screen.findByText('Water used (24 h)')).toBeInTheDocument();
    expect(screen.getByText('1234.5')).toBeInTheDocument();
    expect(screen.getByText('Sensors offline')).toBeInTheDocument();
    expect(screen.queryByText(/water saved|total consumption|losses/i)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('Alert')).toBeInTheDocument());
    expect(screen.getByRole('link', { name: /Flat A-301/ })).toHaveAttribute('href', `/flat/${fx.IDS.flat}`);
  });

  it('does not invent a live flow for flats from the list endpoint', async () => {
    mockFetch(routes());
    render(<WithAuth role="resident"><OverviewView /></WithAuth>);
    await screen.findByText('Flat A-301');
    expect(screen.getByText(/waiting for live data/i)).toBeInTheDocument();
  });

  it('survives empty arrays and null optional fields', async () => {
    mockFetch(routes({
      '/api/v1/flats': [{ ...fx.flat, floor: null }],
      '/api/v1/alerts': [],
      [`/api/v1/societies/${fx.IDS.society}/stats`]: { ...fx.societyStats, volume_24h_l: null },
    }));
    render(<WithAuth role="resident"><OverviewView /></WithAuth>);
    expect(await screen.findByText('Flat A-301')).toBeInTheDocument();
    expect(screen.queryByText(/floor null/i)).not.toBeInTheDocument();
  });

  it('shows an empty state when there are no flats', async () => {
    mockFetch((url) => (url.pathname.endsWith('/societies') ? { body: [fx.society] } : url.pathname.endsWith('/stats') ? { body: fx.societyStats } : { body: [] }));
    render(<WithAuth role="resident"><OverviewView /></WithAuth>);
    expect(await screen.findByText('No flats yet')).toBeInTheDocument();
  });

  it('shows an empty state when there are no societies', async () => {
    mockFetch(() => ({ body: [] }));
    render(<WithAuth role="resident"><OverviewView /></WithAuth>);
    expect(await screen.findByText('No societies found')).toBeInTheDocument();
  });

  it('shows an error state with retry when societies fail', async () => {
    mockFetch(() => ({ status: 500, body: { detail: 'nope' } }));
    render(<WithAuth role="resident"><OverviewView /></WithAuth>);
    expect(await screen.findByText('nope')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});

describe('FlatView', () => {
  it('renders the detail shape (sensor_status object) without crashing, plus anomalies', async () => {
    mockFetch((url) => {
      const p = url.pathname;
      if (p.endsWith('/anomalies')) return { body: [fx.anomaly, fx.ruleAnomaly, { ...fx.anomaly, id: 'x', explanation: null }] };
      if (p.endsWith('/baseline')) return { body: fx.baseline };
      if (p.endsWith('/forecast')) return { body: fx.forecast };
      if (p.endsWith('/readings')) return { body: [fx.reading] };
      if (p.endsWith('/alerts')) return { body: [fx.alert] };
      if (p.includes('/flats/')) return { body: fx.flatDetail };
      return undefined;
    });
    render(<WithAuth role="admin"><FlatView flatId={fx.IDS.flat} /></WithAuth>);
    expect(await screen.findByText('Flat A-301')).toBeInTheDocument();
    expect(await screen.findByText('Online')).toBeInTheDocument();
    expect(await screen.findByText(/SHAP values from the ML classifier/)).toBeInTheDocument();
    expect(screen.getByText(/rule-based heuristic/)).toBeInTheDocument();
    expect(screen.getByText('No explanation available for this anomaly.')).toBeInTheDocument();
    expect(screen.getAllByText(/Severity 62%/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Confidence 87%/).length).toBeGreaterThan(0);
  });

  it('shows a friendly permission state when the flat is forbidden', async () => {
    mockFetch(() => ({ status: 403, body: { detail: 'Insufficient permissions' } }));
    render(<WithAuth role="resident"><FlatView flatId={fx.IDS.flat} /></WithAuth>);
    expect((await screen.findAllByText(/you don.t have permission/i)).length).toBeGreaterThan(0);
  });
});
