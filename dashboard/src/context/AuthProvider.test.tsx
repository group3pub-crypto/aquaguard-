import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useRouter } from 'next/navigation';
import { describe, expect, it } from 'vitest';
import { AuthProvider, useAuth } from './AuthProvider';
import { AuthGuard } from '@/components/AuthGuard';
import { setToken } from '@/lib/session';
import { mockFetch } from '@/test/utils';

function Probe() {
  const { status, user, login, logout } = useAuth();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="user">{user?.username ?? 'none'}</span>
      <button onClick={() => void login('bob', 'pw')}>login</button>
      <button onClick={logout}>logout</button>
    </div>
  );
}

describe('AuthProvider + AuthGuard', () => {
  it('redirects to /login when there is no session', async () => {
    mockFetch(() => ({ body: {} }));
    render(
      <AuthProvider>
        <AuthGuard>
          <p>secret</p>
        </AuthGuard>
      </AuthProvider>,
    );
    await waitFor(() => expect(useRouter().replace).toHaveBeenCalledWith('/login'));
    expect(screen.queryByText('secret')).not.toBeInTheDocument();
  });

  it('restores a session from sessionStorage and renders protected content', async () => {
    setToken('tok');
    const { calls } = mockFetch(() => ({ body: { id: 1, username: 'bob', name: 'Bob', role: 'admin' } }));
    render(
      <AuthProvider>
        <AuthGuard>
          <p>secret</p>
        </AuthGuard>
      </AuthProvider>,
    );
    expect(await screen.findByText('secret')).toBeInTheDocument();
    expect(calls[0]?.headers.Authorization).toBe('Bearer tok');
  });

  it('clears the session and redirects when /auth/me returns 401', async () => {
    setToken('bad');
    mockFetch(() => ({ status: 401, body: { detail: 'expired' } }));
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('unauthenticated'));
    expect(useRouter().replace).toHaveBeenCalledWith('/login');
    expect(window.sessionStorage.getItem('aquaguard.session')).toBeNull();
  });

  it('logs in, then logs out and redirects', async () => {
    mockFetch((url) =>
      url.pathname.endsWith('/auth/login')
        ? { body: { access_token: 't1', token_type: 'bearer', role: 'maintenance', expires_in: 60 } }
        : { body: { id: 2, username: 'bob', name: 'Bob', role: 'maintenance' } },
    );
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('unauthenticated'));
    await userEvent.click(screen.getByText('login'));
    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent('bob'));
    expect(window.sessionStorage.getItem('aquaguard.session')).toBe('t1');
    await userEvent.click(screen.getByText('logout'));
    expect(screen.getByTestId('status')).toHaveTextContent('unauthenticated');
    expect(window.sessionStorage.getItem('aquaguard.session')).toBeNull();
    expect(useRouter().replace).toHaveBeenCalledWith('/login');
  });
});
