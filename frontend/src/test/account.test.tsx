import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { api } from '../api';
import { AccountView } from '../components/AccountView';
import { DeleteResearch } from '../components/DeleteResearch';
import { HistoryView } from '../components/HistoryView';
import { ResearchForm } from '../components/ResearchForm';
import { makeRun, makeUsage, RUN_ID } from './fixtures';

const user = { id: 'user-one', email: 'alex@example.com', name: 'Alex' };
const session = { user, csrf_token: 'test-session-csrf' };

describe('personal usage and deletion', () => {
  it('shows remaining allowances, known usage, unknown cost, and all-time totals', async () => {
    const usage = makeUsage(); usage.today.estimated_cost_usd = null;
    render(<AccountView user={user} usage={usage} error="" refresh={vi.fn()} onDeleted={vi.fn()} />);
    expect(screen.getByLabelText('Remaining research allowance')).toHaveTextContent('18 runs remaining');
    expect(screen.getByLabelText('Remaining research allowance')).toHaveTextContent(`${(847000).toLocaleString()} tokens available`);
    expect(screen.getByText('Unavailable')).toBeInTheDocument();
    expect(screen.getByText('1,200')).toBeInTheDocument();
    expect(screen.getByText(/Known totals may be partial/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Usage period'),'lifetime');
    expect(screen.getByText('9,000')).toBeInTheDocument();
  });

  it('requires account deletion confirmation, sends CSRF, then clears the private workspace', async () => {
    const fetcher = vi.fn(async (path: string, options: RequestInit = {}) => {
      if (path === '/auth/me' && options.method === 'DELETE') return new Response(null,{ status:204 });
      if (path === '/auth/me') return Response.json(session);
      if (path === '/account/usage') return Response.json(makeUsage());
      if (path === '/health') return Response.json({ status:'ok' });
      if (path.startsWith('/research?')) return Response.json({ items:[makeRun()],total:1,offset:0,limit:20 });
      throw new Error('Unexpected endpoint');
    });
    vi.stubGlobal('fetch',fetcher); render(<App />);
    await userEvent.click(await screen.findByRole('button',{ name:'Usage & account' }));
    await userEvent.click(await screen.findByRole('button',{ name:'Delete my account' }));
    await userEvent.click(screen.getByRole('button',{ name:'Permanently delete my account' }));
    expect(screen.getByRole('alert')).toHaveTextContent('type DELETE');
    expect(fetcher.mock.calls.some(call => call[1]?.method === 'DELETE')).toBe(false);
    await userEvent.type(screen.getByLabelText('Current password'),'A private passphrase!');
    await userEvent.type(screen.getByLabelText('Account deletion confirmation'),'DELETE');
    await userEvent.click(screen.getByRole('button',{ name:'Permanently delete my account' }));
    expect(await screen.findByRole('button',{ name:/^Sign in$/ })).toBeInTheDocument();
    expect(screen.queryByRole('button',{ name:/Open research:/ })).not.toBeInTheDocument();
    const request = fetcher.mock.calls.find(call => call[1]?.method === 'DELETE')!;
    expect(request[1]!.headers).toMatchObject({ 'X-CSRF-Token':'test-session-csrf' });
    expect(JSON.parse(request[1]!.body as string)).toEqual({ password:'A private passphrase!',confirmation:'DELETE' });
  });

  it('keeps the account visible after a wrong deletion password and clears the password input', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ error:{ message:'secret-error' } },{ status:401 })));
    const onDeleted = vi.fn();
    render(<AccountView user={user} usage={makeUsage()} error="" refresh={vi.fn()} onDeleted={onDeleted} />);
    await userEvent.click(screen.getByRole('button',{ name:'Delete my account' }));
    await userEvent.type(screen.getByLabelText('Current password'),'wrong password');
    await userEvent.type(screen.getByLabelText('Account deletion confirmation'),'DELETE');
    await userEvent.click(screen.getByRole('button',{ name:'Permanently delete my account' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('current password is incorrect');
    expect(screen.getByLabelText('Current password')).toHaveValue('');
    expect(onDeleted).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain('secret-error');
  });

  it('deletes research only after confirmation and handles failure without hiding it', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(null,{ status:500 })).mockResolvedValueOnce(new Response(null,{ status:204 }));
    vi.stubGlobal('fetch',fetcher);
    const onDeleted = vi.fn(); render(<DeleteResearch id={RUN_ID} topic="A question" onDeleted={onDeleted} />);
    await userEvent.click(screen.getByRole('button',{ name:'Delete research: A question' }));
    expect(fetcher).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button',{ name:'Keep research' }));
    expect(fetcher).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button',{ name:'Delete research: A question' }));
    await userEvent.click(screen.getByRole('button',{ name:'Permanently delete research' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('backend could not complete');
    expect(onDeleted).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button',{ name:'Permanently delete research' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledOnce());
  });
});

describe('server history filters and safe submissions', () => {
  it('searches the server across pages and combines status, depth and date filters', async () => {
    const fetcher = vi.fn(async (path: string) => {
      const url = new URL(path,'http://localhost');
      const searched = url.searchParams.get('q') === 'buried source';
      return Response.json({ items:[makeRun({ topic:searched ? 'Buried source result' : 'Recent question' })],
        total:searched ? 1 : 41,offset:Number(url.searchParams.get('offset')),limit:20 });
    });
    vi.stubGlobal('fetch',fetcher); render(<HistoryView onOpen={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByRole('button',{ name:'Recent question' });
    await userEvent.click(screen.getByRole('button',{ name:'Older' }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith(expect.stringContaining('offset=20'),expect.anything()));
    await userEvent.type(screen.getByLabelText('Search all research history'),'buried source');
    expect(await screen.findByRole('button',{ name:'Buried source result' })).toBeInTheDocument();
    expect(screen.getByRole('button',{ name:'Newer' })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText('Filter by status'),'completed');
    await userEvent.selectOptions(screen.getByLabelText('Filter by depth'),'quick');
    await userEvent.type(screen.getByLabelText('Created from'),'2026-01-01');
    await userEvent.type(screen.getByLabelText('Created through'),'2026-11-10');
    await waitFor(() => expect(fetcher.mock.calls.at(-1)![0]).toContain('created_to=2026-11-10'));
    const last = fetcher.mock.calls.at(-1)![0];
    expect(last).toContain('status=completed'); expect(last).toContain('depth=quick'); expect(last).toContain('offset=0');
  });

  it('reuses an idempotency key after a lost response and changes it when the payload changes', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('Lost response'));
    vi.stubGlobal('fetch',fetcher); render(<ResearchForm onCreated={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('Research topic'),'A focused question');
    await userEvent.click(screen.getByRole('button',{ name:/Start research/ }));
    await screen.findByRole('alert');
    await userEvent.click(screen.getByRole('button',{ name:/Start research/ }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    expect(fetcher.mock.calls[0][1].headers['Idempotency-Key']).toBe(fetcher.mock.calls[1][1].headers['Idempotency-Key']);
    await userEvent.type(screen.getByLabelText('Research topic'),' changed');
    await userEvent.click(screen.getByRole('button',{ name:/Start research/ }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
    expect(fetcher.mock.calls[2][1].headers['Idempotency-Key']).not.toBe(fetcher.mock.calls[0][1].headers['Idempotency-Key']);
  });

  it('shows a safe actionable quota error rather than a login error or raw server text', async () => {
    vi.stubGlobal('fetch',vi.fn(async () => Response.json({ error:{ code:'daily_token_limit',message:'secret-provider-detail' } },{ status:429 })));
    await expect(api.create({ topic:'A question',instructions:null,depth:'deep' })).rejects.toThrow('Choose a lower depth');
    render(<ResearchForm onCreated={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('Research topic'),'A question');
    await userEvent.click(screen.getByRole('button',{ name:/Start research/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('daily token allowance');
    expect(document.body.textContent).not.toContain('secret-provider-detail');
  });
});
