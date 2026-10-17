import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { SocietiesAdminView } from './SocietiesAdminView';
import { FlatsAdminView } from './FlatsAdminView';
import { UsersAdminView } from './UsersAdminView';
import { AccountView } from '../AccountView';
import { WithAuth, mockFetch, type Handler } from '@/test/utils';
import * as fx from '@/test/fixtures';
import { getRefreshToken, getToken } from '@/lib/session';
import { generateTempPassword } from '@/lib/password';
import { validatePassword } from '@/lib/validation';

const admin = (ui: React.ReactNode) => render(<WithAuth role="admin">{ui}</WithAuth>);

function routes(extra: Handler): Handler {
  return (url, init) => {
    const r = extra(url, init);
    if (r) return r;
    const p = url.pathname;
    const m = init.method ?? 'GET';
    if (m === 'GET' && p.endsWith('/societies')) return { body: [fx.society] };
    if (m === 'GET' && p.endsWith('/flats')) return { body: [fx.flat] };
    if (m === 'GET' && p.endsWith('/users')) return { body: [fx.userOut, { ...fx.userOut, id: '22222222-2222-4222-8222-222222222222', username: 'root', name: 'Root', role: 'admin', flat_id: null, society_id: null }] };
    return undefined;
  };
}

describe('admin access', () => {
  it.each([
    ['societies', <SocietiesAdminView key="s" />],
    ['flats', <FlatsAdminView key="f" />],
    ['users', <UsersAdminView key="u" />],
  ])('%s screen is not rendered for maintenance or residents', (_n, ui) => {
    const { calls } = mockFetch(() => ({ body: [] }));
    for (const role of ['maintenance', 'resident'] as const) {
      const { unmount } = render(<WithAuth role={role}>{ui}</WithAuth>);
      expect(screen.getByText(/you don.t have permission/i)).toBeInTheDocument();
      unmount();
    }
    expect(calls).toHaveLength(0);
  });
});

describe('societies admin', () => {
  it('validates the slug with an associated error and does not call the API', async () => {
    const { calls } = mockFetch(routes(() => undefined));
    admin(<SocietiesAdminView />);
    await screen.findByText('Green Meadows');
    await userEvent.click(screen.getByRole('button', { name: 'New society' }));
    await userEvent.type(screen.getByLabelText(/^Name/), 'Blue Hills');
    await userEvent.type(screen.getByLabelText(/^Slug/), 'Blue Hills!');
    await userEvent.click(screen.getByRole('button', { name: 'Create society' }));
    const slug = screen.getByLabelText(/^Slug/);
    expect(slug).toHaveAttribute('aria-invalid', 'true');
    const described = slug.getAttribute('aria-describedby') ?? '';
    expect(described).toContain('soc-slug-err');
    expect(document.getElementById('soc-slug-err')).toHaveTextContent(/lowercase/i);
    expect(calls.filter((c) => c.init.method === 'POST')).toHaveLength(0);
  });

  it('creates a society, then refreshes the list (no optimistic row)', async () => {
    const { calls } = mockFetch(routes((url, init) => (init.method === 'POST' ? { status: 201, body: fx.society } : undefined)));
    admin(<SocietiesAdminView />);
    await screen.findByText('Green Meadows');
    await userEvent.click(screen.getByRole('button', { name: 'New society' }));
    await userEvent.type(screen.getByLabelText(/^Name/), 'Blue Hills');
    await userEvent.type(screen.getByLabelText(/^Slug/), 'blue-hills');
    await userEvent.click(screen.getByRole('button', { name: 'Create society' }));
    await waitFor(() => expect(calls.some((c) => c.init.method === 'POST')).toBe(true));
    const post = calls.find((c) => c.init.method === 'POST');
    expect(post?.body).toEqual({ name: 'Blue Hills', slug: 'blue-hills', address: null });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Create society' })).not.toBeInTheDocument());
  });

  it('shows the server detail on a slug conflict (409)', async () => {
    mockFetch(routes((url, init) => (init.method === 'POST' ? { status: 409, body: { detail: 'Slug already in use' } } : undefined)));
    admin(<SocietiesAdminView />);
    await screen.findByText('Green Meadows');
    await userEvent.click(screen.getByRole('button', { name: 'New society' }));
    await userEvent.type(screen.getByLabelText(/^Name/), 'X');
    await userEvent.type(screen.getByLabelText(/^Slug/), 'taken');
    await userEvent.click(screen.getByRole('button', { name: 'Create society' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Slug already in use');
  });

  it('requires confirmation before deleting and surfaces a 409', async () => {
    const { calls } = mockFetch(routes((url, init) => (init.method === 'DELETE' ? { status: 409, body: { detail: 'Society still has flats' } } : undefined)));
    admin(<SocietiesAdminView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Delete Green Meadows' }));
    expect(calls.some((c) => c.init.method === 'DELETE')).toBe(false); // nothing sent yet
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete society' }));
    expect(await within(dialog).findByText('Society still has flats')).toBeInTheDocument();
    expect(screen.getByText('Green Meadows')).toBeInTheDocument(); // still listed
  });

  it('cancel closes the delete dialog without calling the API', async () => {
    const { calls } = mockFetch(routes(() => undefined));
    admin(<SocietiesAdminView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Delete Green Meadows' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(calls.some((c) => c.init.method === 'DELETE')).toBe(false);
  });
});

describe('flats admin', () => {
  it('validates unit number, floor, residents and sensor id against the backend constraints', async () => {
    const { calls } = mockFetch(routes(() => undefined));
    admin(<FlatsAdminView />);
    await screen.findByText('A-301');
    await userEvent.click(screen.getByRole('button', { name: 'New flat' }));
    await userEvent.type(screen.getByLabelText(/^Unit number/), 'bad unit!');
    await userEvent.type(screen.getByLabelText('Floor'), '301');
    await userEvent.clear(screen.getByLabelText('Residents'));
    await userEvent.type(screen.getByLabelText('Residents'), '51');
    await userEvent.type(screen.getByLabelText('Sensor id'), 'bad id');
    await userEvent.click(screen.getByRole('button', { name: 'Create flat' }));
    for (const id of ['flat-society-err', 'flat-unit-err', 'flat-floor-err', 'flat-residents-err', 'flat-sensor-err']) {
      expect(document.getElementById(id), id).not.toBeNull();
    }
    expect(screen.getByLabelText('Floor')).toHaveAttribute('aria-invalid', 'true');
    expect(calls.filter((c) => c.init.method === 'POST')).toHaveLength(0);
  });

  it('creates a flat with society, unit, floor, residents and sensor', async () => {
    const { calls } = mockFetch(routes((url, init) => (init.method === 'POST' ? { status: 201, body: fx.flat } : undefined)));
    admin(<FlatsAdminView />);
    await screen.findByText('A-301');
    await userEvent.click(screen.getByRole('button', { name: 'New flat' }));
    await userEvent.selectOptions(screen.getByLabelText(/^Society/, { selector: '#flat-society' }), fx.IDS.society);
    await userEvent.type(screen.getByLabelText(/^Unit number/), 'B-12');
    await userEvent.type(screen.getByLabelText('Floor'), '4');
    await userEvent.clear(screen.getByLabelText('Residents'));
    await userEvent.type(screen.getByLabelText('Residents'), '3');
    await userEvent.type(screen.getByLabelText('Sensor id'), 'esp32-b12');
    await userEvent.click(screen.getByRole('button', { name: 'Create flat' }));
    await waitFor(() => expect(calls.some((c) => c.init.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.init.method === 'POST')?.body).toEqual({
      society_id: fx.IDS.society,
      unit_number: 'B-12',
      floor: 4,
      residents: 3,
      sensor_id: 'esp32-b12',
    });
  });

  it('edits only the editable fields (PATCH) and shows a 409 sensor conflict', async () => {
    const { calls } = mockFetch(routes((url, init) => (init.method === 'PATCH' ? { status: 409, body: { detail: 'sensor_id already assigned' } } : undefined)));
    admin(<FlatsAdminView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Edit flat A-301' }));
    expect(screen.getByLabelText(/^Unit number/)).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('sensor_id already assigned');
    expect(calls.find((c) => c.init.method === 'PATCH')?.url.pathname).toBe(`/api/v1/flats/${fx.IDS.flat}`);
  });

  it('confirms before deleting a flat and shows the valve-closed 409', async () => {
    const { calls } = mockFetch(routes((url, init) => (init.method === 'DELETE' ? { status: 409, body: { detail: 'Valve is closed: open it before deleting' } } : undefined)));
    admin(<FlatsAdminView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Delete flat A-301' }));
    expect(calls.some((c) => c.init.method === 'DELETE')).toBe(false);
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete flat' }));
    expect(await screen.findByText('Valve is closed: open it before deleting')).toBeInTheDocument();
  });
});

describe('users admin', () => {
  it('validates username, name and password policy before creating', async () => {
    const { calls } = mockFetch(routes(() => undefined));
    admin(<UsersAdminView />);
    await screen.findByText('alice');
    await userEvent.click(screen.getByRole('button', { name: 'New user' }));
    await userEvent.type(screen.getByLabelText(/^Username/), 'ab');
    await userEvent.type(screen.getByLabelText(/^Initial password/), 'alllettersonly');
    await userEvent.click(screen.getByRole('button', { name: 'Create user' }));
    for (const id of ['usr-username-err', 'usr-name-err', 'usr-password-err']) expect(document.getElementById(id), id).not.toBeNull();
    expect(screen.getByLabelText(/^Initial password/).getAttribute('aria-describedby')).toContain('usr-password-hint');
    expect(calls.filter((c) => c.init.method === 'POST')).toHaveLength(0);
  });

  it('creates a resident bound to a flat; a duplicate username (409) shows the server detail', async () => {
    let status = 409;
    const { calls } = mockFetch(
      routes((url, init) =>
        init.method === 'POST' ? (status === 409 ? { status: 409, body: { detail: 'Username already taken' } } : { status: 201, body: fx.userOut }) : undefined,
      ),
    );
    admin(<UsersAdminView />);
    await screen.findByText('alice');
    await userEvent.click(screen.getByRole('button', { name: 'New user' }));
    await userEvent.type(screen.getByLabelText(/^Username/), 'bob');
    await userEvent.type(screen.getByLabelText(/^Full name/), 'Bob');
    await userEvent.type(screen.getByLabelText(/^Initial password/), 'passw0rdx');
    await userEvent.selectOptions(screen.getByLabelText(/^Flat/), fx.IDS.flat);
    await userEvent.click(screen.getByRole('button', { name: 'Create user' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Username already taken');
    status = 201;
    await userEvent.click(screen.getByRole('button', { name: 'Create user' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Create user' })).not.toBeInTheDocument());
    const posts = calls.filter((c) => c.init.method === 'POST');
    expect(posts[1]?.body).toEqual({ username: 'bob', name: 'Bob', password: 'passw0rdx', role: 'resident', flat_id: fx.IDS.flat, society_id: null });
  });

  it('role switch changes the binding control: maintenance binds a society, admin none', async () => {
    mockFetch(routes(() => undefined));
    admin(<UsersAdminView />);
    await screen.findByText('alice');
    await userEvent.click(screen.getByRole('button', { name: 'New user' }));
    await userEvent.selectOptions(screen.getByLabelText(/^Role/, { selector: '#usr-role' }), 'maintenance');
    expect(screen.getByLabelText(/^Society/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^Flat/)).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText(/^Role/, { selector: '#usr-role' }), 'admin');
    expect(screen.getByText(/not bound to a society or flat/i)).toBeInTheDocument();
  });

  it('sets a binding via PUT /users/{id}/binding after confirmation', async () => {
    const { calls } = mockFetch(routes((url, init) => (init.method === 'PUT' ? { body: fx.userOut } : undefined)));
    admin(<UsersAdminView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Set binding for alice' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.selectOptions(within(dialog).getByLabelText(/^Flat/), fx.IDS.flat);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save binding' }));
    await waitFor(() => expect(calls.some((c) => c.init.method === 'PUT')).toBe(true));
    const put = calls.find((c) => c.init.method === 'PUT');
    expect(put?.url.pathname).toBe(`/api/v1/users/${fx.userOut.id}/binding`);
    expect(put?.body).toEqual({ flat_id: fx.IDS.flat, society_id: null });
  });

  it('deactivation needs confirmation; the last-admin 409 gets a friendly message', async () => {
    const { calls } = mockFetch(
      routes((url, init) =>
        init.method === 'PATCH' ? { status: 409, body: { detail: 'cannot demote, deactivate or delete the last active admin' } } : undefined,
      ),
    );
    admin(<UsersAdminView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Deactivate root' }));
    expect(calls.some((c) => c.init.method === 'PATCH')).toBe(false);
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Deactivate' }));
    expect(await screen.findByText(/cannot demote or deactivate the last active administrator/i)).toBeInTheDocument();
    expect(calls.find((c) => c.init.method === 'PATCH')?.body).toEqual({ is_active: false });
  });

  it('reset password confirms, sends a policy-compliant temp password, shows it ONCE and offers copy', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const { calls } = mockFetch(routes((url, init) => (init.method === 'POST' ? { status: 204 } : undefined)));
    admin(<UsersAdminView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Reset password for alice' }));
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Reset password' }));
    const shown = (await screen.findByTestId('temp-password')).textContent ?? '';
    const post = calls.find((c) => c.init.method === 'POST');
    expect(post?.url.pathname).toBe(`/api/v1/users/${fx.userOut.id}/reset-password`);
    expect((post?.body as { new_password: string }).new_password).toBe(shown);
    expect(validatePassword(shown)).toBeUndefined();
    await userEvent.click(screen.getByRole('button', { name: 'Copy password' }));
    expect(writeText).toHaveBeenCalledWith(shown);
    await userEvent.click(screen.getByRole('button', { name: 'I have saved it' }));
    expect(screen.queryByTestId('temp-password')).not.toBeInTheDocument();
    expect(screen.queryByText(shown)).not.toBeInTheDocument();
  });

  it('log out everywhere confirms then POSTs logout-all', async () => {
    const { calls } = mockFetch(routes((url, init) => (init.method === 'POST' ? { status: 204 } : undefined)));
    admin(<UsersAdminView />);
    await userEvent.click(await screen.findByRole('button', { name: 'Log out alice everywhere' }));
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Log out everywhere' }));
    await waitFor(() => expect(calls.some((c) => c.url.pathname.endsWith('/logout-all'))).toBe(true));
  });

  it('tolerates an empty user list', async () => {
    mockFetch(() => ({ body: [] }));
    admin(<UsersAdminView />);
    expect(await screen.findByText('No users match')).toBeInTheDocument();
  });

  it('the table is keyboard friendly: captioned, row headers, real buttons', async () => {
    mockFetch(routes(() => undefined));
    admin(<UsersAdminView />);
    const table = await screen.findByRole('table', { name: 'Users' });
    expect(within(table).getAllByRole('rowheader').length).toBeGreaterThan(0);
    await userEvent.tab();
    expect(document.activeElement?.tagName).not.toBe('BODY');
  });
});

describe('temp password generator', () => {
  it('always satisfies the password policy', () => {
    for (let i = 0; i < 200; i++) expect(validatePassword(generateTempPassword())).toBeUndefined();
  });
});

describe('change password', () => {
  it('validates, posts, and keeps the NEW tokens returned by the server', async () => {
    const { calls } = mockFetch(() => ({ body: { ...fx.tokenResponse, access_token: 'after', refresh_token: 'refresh-after-change-00001' } }));
    render(<WithAuth role="resident"><AccountView /></WithAuth>);
    await userEvent.type(screen.getByLabelText(/^Current password/), 'old-pass1');
    await userEvent.type(screen.getByLabelText(/^New password/), 'short');
    await userEvent.type(screen.getByLabelText(/^Repeat new password/), 'different');
    await userEvent.click(screen.getByRole('button', { name: 'Change password' }));
    expect(screen.getByLabelText(/^New password/)).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText(/^Repeat new password/)).toHaveAttribute('aria-invalid', 'true');
    expect(calls).toHaveLength(0);
    await userEvent.clear(screen.getByLabelText(/^New password/));
    await userEvent.clear(screen.getByLabelText(/^Repeat new password/));
    await userEvent.type(screen.getByLabelText(/^New password/), 'new-pass2');
    await userEvent.type(screen.getByLabelText(/^Repeat new password/), 'new-pass2');
    await userEvent.click(screen.getByRole('button', { name: 'Change password' }));
    expect(await screen.findByText(/password changed/i)).toBeInTheDocument();
    expect(calls[0]?.body).toEqual({ current_password: 'old-pass1', new_password: 'new-pass2' });
    expect(getToken()).toBe('after');
    expect(getRefreshToken()).toBe('refresh-after-change-00001');
  });

  it('shows the server error when the current password is wrong (403)', async () => {
    mockFetch(() => ({ status: 403, body: { detail: 'Current password is incorrect' } }));
    render(<WithAuth role="resident"><AccountView /></WithAuth>);
    await userEvent.type(screen.getByLabelText(/^Current password/), 'wrong-pass1');
    await userEvent.type(screen.getByLabelText(/^New password/), 'new-pass2');
    await userEvent.type(screen.getByLabelText(/^Repeat new password/), 'new-pass2');
    await userEvent.click(screen.getByRole('button', { name: 'Change password' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Current password is incorrect');
  });
});
