import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AlertRow } from './AlertRow';
import { ValveControls } from './ValveControls';
import { WithAuth, sampleAlert } from '@/test/utils';
import type { Alert } from '@/lib/types';

const alert = sampleAlert as Alert;

describe('role-gated rendering', () => {
  it('hides valve controls from residents', () => {
    render(
      <WithAuth role="resident">
        <ValveControls flatId={1} valveState="open" />
      </WithAuth>,
    );
    expect(screen.queryByRole('button', { name: /close valve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /open valve/i })).not.toBeInTheDocument();
  });

  it('hides valve controls from maintenance staff but shows them to admins', () => {
    const { rerender } = render(
      <WithAuth role="maintenance">
        <ValveControls flatId={1} valveState="open" />
      </WithAuth>,
    );
    expect(screen.queryByRole('button', { name: /close valve/i })).not.toBeInTheDocument();
    rerender(
      <WithAuth role="admin">
        <ValveControls flatId={1} valveState="open" />
      </WithAuth>,
    );
    expect(screen.getByRole('button', { name: /close valve/i })).toBeEnabled();
  });

  it('shows alert actions per role', () => {
    const { rerender } = render(
      <WithAuth role="resident">
        <AlertRow alert={alert} onChanged={() => undefined} />
      </WithAuth>,
    );
    expect(screen.queryByRole('button', { name: 'Acknowledge' })).not.toBeInTheDocument();
    rerender(
      <WithAuth role="maintenance">
        <AlertRow alert={alert} onChanged={() => undefined} />
      </WithAuth>,
    );
    expect(screen.getByRole('button', { name: 'Acknowledge' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Override' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reopen valve' })).not.toBeInTheDocument();
    rerender(
      <WithAuth role="admin">
        <AlertRow alert={alert} onChanged={() => undefined} />
      </WithAuth>,
    );
    expect(screen.getByRole('button', { name: 'Override' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reopen valve' })).toBeInTheDocument();
  });
});
