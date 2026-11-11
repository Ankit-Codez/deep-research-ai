import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { api } from '../api';
import { makeRun, makeUsage } from './fixtures';

const session = { user: { id: 'user-one', email: 'person@example.com', name: 'Alex' }, csrf_token: 'session-csrf' };

function authBackend(loggedIn = false) {
  let authenticated = loggedIn;
  const fetcher = vi.fn(async (path: string, options: RequestInit = {}) => {
    if (path === '/auth/me') return authenticated ? Response.json(session) : Response.json({}, { status: 401 });
    if (path === '/auth/csrf') return Response.json({ csrf_token: 'preauth-csrf' });
    if (path === '/auth/login' || path === '/auth/register') { authenticated = true; return Response.json(session); }
    if (path === '/auth/logout') { authenticated = false; return new Response(null, { status: 204 }); }
    if (path === '/health') return Response.json({ status: 'ok' });
    if (path === '/account/usage') return Response.json(makeUsage());
    if (path.startsWith('/research?')) return Response.json({ items: [makeRun()], total: 1, offset: 0, limit: 20 });
    if (path === '/research' && options.method === 'POST') return Response.json(makeRun());
    throw new Error('Unexpected test endpoint: ' + path);
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

describe('private workspace authentication', () => {
  it('does not request private research before login', async () => {
    const fetcher = authBackend(); render(<App />);
    expect(await screen.findByRole('button', { name: /^Sign in$/ })).toBeInTheDocument();
    expect(screen.queryByLabelText('Research topic')).not.toBeInTheDocument();
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/auth/me']);
  });

  it('logs in with preauth CSRF and keeps tokens out of localStorage', async () => {
    const fetcher = authBackend(); render(<App />);
    await userEvent.type(await screen.findByLabelText('Email address'), 'person@example.com');
    await userEvent.type(screen.getByLabelText('Password'), 'A long private passphrase!');
    await userEvent.click(screen.getByRole('button', { name: /^Sign in$/ }));
    expect(await screen.findByLabelText('Research topic')).toBeInTheDocument();
    const login = fetcher.mock.calls.find(call => call[0] === '/auth/login')!;
    expect(login[1]!.headers).toMatchObject({ 'X-CSRF-Token': 'preauth-csrf' });
    expect(login[1]!.credentials).toBe('same-origin');
    expect(localStorage.length).toBe(0);
    await api.create({ topic: 'A research topic', instructions: null, depth: 'quick' });
    expect(fetcher.mock.calls.find(call => call[0] === '/research')![1]!.headers)
      .toMatchObject({ 'X-CSRF-Token': 'session-csrf' });
  });

  it('validates the registration passphrase before sending credentials', async () => {
    const fetcher = authBackend(); render(<App />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create an account' }));
    await userEvent.type(screen.getByLabelText('Your name'), 'Alex');
    await userEvent.type(screen.getByLabelText('Email address'), 'person@example.com');
    await userEvent.type(screen.getByLabelText('Password'), 'short');
    await userEvent.click(screen.getByRole('button', { name: /^Create account$/ }));
    expect(screen.getByRole('alert')).toHaveTextContent('at least 15 characters');
    expect(fetcher.mock.calls.some(call => call[0] === '/auth/register')).toBe(false);
    await userEvent.clear(screen.getByLabelText('Password'));
    await userEvent.type(screen.getByLabelText('Password'), 'A memorable private passphrase!');
    await userEvent.click(screen.getByRole('button', { name: /^Create account$/ }));
    expect(await screen.findByLabelText('Research topic')).toBeInTheDocument();
    expect(JSON.parse(fetcher.mock.calls.find(call => call[0] === '/auth/register')![1]!.body as string).name).toBe('Alex');
  });

  it('revokes the session and removes private history on logout', async () => {
    const fetcher = authBackend(true); render(<App />);
    expect(await screen.findByRole('button', { name: /Open research:/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(await screen.findByRole('button', { name: /^Sign in$/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Open research:/ })).not.toBeInTheDocument();
    expect(fetcher.mock.calls.find(call => call[0] === '/auth/logout')![1]!.headers)
      .toMatchObject({ 'X-CSRF-Token': 'session-csrf' });
  });

  it('unmounts private data when an authenticated request returns 401', async () => {
    const fetcher = authBackend(true); render(<App />);
    await screen.findByLabelText('Research topic');
    fetcher.mockImplementation(async () => Response.json({}, { status: 401 }));
    await expect(api.list()).rejects.toThrow('session has expired');
    await waitFor(() => expect(screen.queryByLabelText('Research topic')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: /^Sign in$/ })).toBeInTheDocument();
  });

  it('does not display raw server credential errors', async () => {
    authBackend(); render(<App />);
    await userEvent.type(await screen.findByLabelText('Email address'), 'person@example.com');
    await userEvent.type(screen.getByLabelText('Password'), 'A wrong password!');
    vi.stubGlobal('fetch', vi.fn(async (path: string) => path === '/auth/csrf'
      ? Response.json({ csrf_token: 'preauth-csrf' })
      : Response.json({ detail: 'internal-password-secret' }, { status: 401 })));
    await userEvent.click(screen.getByRole('button', { name: /^Sign in$/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid email or password.');
    expect(document.body.textContent).not.toContain('internal-password-secret');
  });
});
