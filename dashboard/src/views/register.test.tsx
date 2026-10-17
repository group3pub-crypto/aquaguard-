import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { LoginView } from './LoginView';
import { RegisterView } from './RegisterView';
import { AppShell } from '@/components/AppShell';
import { AuthContext, AuthProvider } from '@/context/AuthProvider';
import { authValue, WithAuth, mockFetch } from '@/test/utils';
import { resetRegistrationProbe } from '@/lib/features';

beforeEach(() => resetRegistrationProbe());
afterEach(() => {
  delete window.__AQUAGUARD_CONFIG__;
});

const withConfig = (allowSelfRegistration: boolean) => {
  window.__AQUAGUARD_CONFIG__ = { allowSelfRegistration };
};

describe('self-registration', () => {
  it('hides the Register link by default (backend answers 404 unless enabled)', async () => {
    render(<WithAuth role={null}><LoginView /></WithAuth>);
    await waitFor(() => expect(screen.getByText(/ask your administrator/i)).toBeInTheDocument());
    expect(screen.queryByRole('link', { name: 'Register' })).not.toBeInTheDocument();
  });

  it('shows the link when the deployment enables registration', async () => {
    withConfig(true);
    render(<WithAuth role={null}><LoginView /></WithAuth>);
    expect(await screen.findByRole('link', { name: 'Register' })).toHaveAttribute('href', '/register');
  });

  it('handles a 404 from /auth/register gracefully and hides the link afterwards', async () => {
    withConfig(true);
    mockFetch(() => ({ status: 404, body: { detail: 'Not found' } }));
    render(<AuthProvider><RegisterView /></AuthProvider>);
    await userEvent.type(screen.getByLabelText('Full name'), 'Bob Builder');
    await userEvent.type(screen.getByLabelText('Username'), 'bob');
    await userEvent.type(screen.getByLabelText('Password'), 'passw0rdx');
    await userEvent.click(screen.getByRole('button', { name: 'Register' }));
    expect(await screen.findByText(/registration is closed/i)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    render(<WithAuth role={null}><LoginView /></WithAuth>);
    await waitFor(() => expect(screen.getAllByText(/ask your administrator/i).length).toBeGreaterThan(0));
    expect(screen.queryByRole('link', { name: 'Register' })).not.toBeInTheDocument();
  });

  it('validates the password policy with associated error text', async () => {
    const { calls } = mockFetch(() => ({ body: {} }));
    render(<WithAuth role={null}><RegisterView /></WithAuth>);
    await userEvent.type(screen.getByLabelText('Full name'), 'Bob');
    await userEvent.type(screen.getByLabelText('Username'), 'bob');
    await userEvent.type(screen.getByLabelText('Password'), 'onlyletters');
    await userEvent.click(screen.getByRole('button', { name: 'Register' }));
    const pw = screen.getByLabelText('Password');
    expect(pw).toHaveAttribute('aria-invalid', 'true');
    expect(pw.getAttribute('aria-describedby')).toContain('password-err');
    expect(screen.getByText(/letter and one digit/i)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });
});

describe('unbound accounts', () => {
  const shell = (role: 'resident' | 'maintenance', user: Record<string, unknown>) =>
    render(
      <AuthContext.Provider value={authValue(role, 'alice', user)}>
        <AppShell><p>real content</p></AppShell>
      </AuthContext.Provider>,
    );

  it('shows a friendly "not linked to a home" state to a resident without a flat', () => {
    shell('resident', { flat_id: null });
    expect(screen.getByText(/your account is not linked to a home yet/i)).toBeInTheDocument();
    expect(screen.queryByText('real content')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Overview' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Account' })).toBeInTheDocument(); // can still change password
  });

  it('treats maintenance without a society the same way', () => {
    shell('maintenance', { society_id: null });
    expect(screen.getByText(/not linked to a home yet/i)).toBeInTheDocument();
  });

  it('renders normal content for bound users and shows admin links only to admins', () => {
    shell('resident', {});
    expect(screen.getByText('real content')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Users' })).not.toBeInTheDocument();
  });

  it('admins see the admin navigation', () => {
    render(
      <WithAuth role="admin"><AppShell><p>x</p></AppShell></WithAuth>,
    );
    for (const n of ['Societies', 'Flats', 'Users']) expect(screen.getByRole('link', { name: n })).toBeInTheDocument();
  });
});
