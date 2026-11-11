import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { api } from '../api';
import { useRunEvents } from '../events';
import { AccountExport } from '../components/AccountExport';
import { AdminView } from '../components/AdminView';
import { RunWorkspace } from '../components/RunWorkspace';
import { makeRun, makeUsage, RUN_ID } from './fixtures';

class FakeSource extends EventTarget {
  static instances: FakeSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();
  constructor(public url: string, public options: { withCredentials?: boolean }) { super(); FakeSource.instances.push(this); }
  send(name: string, value = {}) { this.dispatchEvent(new MessageEvent(name,{ data:JSON.stringify(value) })); }
}

describe('durable recovery and live updates', () => {
  it('uses cookie-authenticated SSE, refreshes on events, falls back, and closes on navigation', async () => {
    FakeSource.instances = []; vi.stubGlobal('EventSource',FakeSource);
    const refresh = vi.fn();
    const { result, unmount } = renderHook(() => useRunEvents(RUN_ID,true,refresh));
    const source = FakeSource.instances[0];
    expect(source.url).toBe(`/research/${RUN_ID}/events`);
    expect(source.options.withCredentials).toBe(true);
    act(() => source.onopen?.()); expect(result.current).toBe(true);
    act(() => source.send('progress')); expect(refresh).toHaveBeenCalledOnce();
    act(() => source.onerror?.()); expect(result.current).toBe(false);
    act(() => source.send('unavailable')); expect(source.close).not.toHaveBeenCalled();
    unmount(); expect(source.close).toHaveBeenCalled();
  });

  it('ends private streaming when the session is revoked', () => {
    FakeSource.instances = []; vi.stubGlobal('EventSource',FakeSource);
    const expired = vi.fn(); window.addEventListener('auth-required',expired);
    const { unmount } = renderHook(() => useRunEvents(RUN_ID,true,vi.fn()));
    act(() => FakeSource.instances[0].send('auth_required'));
    expect(expired).toHaveBeenCalledOnce(); expect(FakeSource.instances[0].close).toHaveBeenCalled();
    unmount(); window.removeEventListener('auth-required',expired);
  });

  it('resumes a safe checkpoint through a CSRF-protected request', async () => {
    let run = makeRun({ status:'failed', report:null, resumable:true, resume_count:0 });
    const fetcher = vi.fn(async (path: string, options: RequestInit = {}) => {
      if (path === '/auth/me') return Response.json({ user:{ id:'user',email:'test@example.com',name:'Test' },csrf_token:'csrf' });
      if (path.endsWith('/resume')) { run = { ...run,status:'queued',resumable:false }; return Response.json(run); }
      if (path.endsWith('/sources')) return Response.json([]);
      return Response.json(run);
    });
    vi.stubGlobal('fetch',fetcher); await api.me();
    render(<RunWorkspace id={RUN_ID} onBack={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button',{ name:'Resume research' }));
    expect(await screen.findByText('Your question is in the queue.')).toBeInTheDocument();
    const request = fetcher.mock.calls.find(call => call[0].endsWith('/resume'))!;
    expect(request[1]!.method).toBe('POST');
    expect(request[1]!.headers).toMatchObject({ 'X-CSRF-Token':'csrf' });
  });

  it('explains why an unknown provider outcome cannot resume', async () => {
    vi.stubGlobal('fetch',vi.fn(async (path: string) => Response.json(path.endsWith('/sources') ? [] :
      makeRun({ status:'failed',report:null,resumable:false,resume_blocked_reason:'A provider request may already be billed. Automatic replay is blocked.' }))));
    render(<RunWorkspace id={RUN_ID} onBack={vi.fn()} />);
    expect(await screen.findByText(/may already be billed/)).toBeInTheDocument();
    expect(screen.queryByRole('button',{ name:'Resume research' })).not.toBeInTheDocument();
  });

  it('shows provider quota diagnostics separately from evidence quality', async () => {
    const run = makeRun(); run.report!.limitations = ['The LLM provider rate or quota limit was reached.'];
    vi.stubGlobal('fetch',vi.fn(async (path: string) => Response.json(path.endsWith('/sources') ? run.report!.sources : run)));
    render(<RunWorkspace id={RUN_ID} onBack={vi.fn()} />);
    expect(await screen.findByRole('heading',{ name:'Provider or allowance issue' })).toBeInTheDocument();
    expect(screen.getByLabelText('Provider and allowance notices')).toHaveTextContent('quota limit was reached');
  });
});

describe('administrator and export flows', () => {
  it('shows queue counts, failure codes and spending alerts with server filters', async () => {
    const fetcher = vi.fn(async (path: string) => path.startsWith('/admin/overview') ? Response.json({
      counts:{ queued:2,running:1,completed:5,failed:3,cancelled:0 },oldest_queued_seconds:150,
      failed_last_24h:3,insufficient_last_24h:1,unknown_provider_calls:1,day_start:'2026-11-10T00:00:00Z',
      estimated_cost_usd:.1,estimated_reserved_exposure_usd:12,unknown_cost_runs:1,spending_alert_usd:10,
      alerts:[{ code:'spending_threshold',severity:'warning',message:'Estimated exposure reached the spending threshold.' }],
    }) : Response.json({ items:[{ id:RUN_ID,status:'failed',depth:'standard',current_step:'planner',progress:.1,
      created_at:'2026-11-10T00:00:00Z',error_code:'provider_outcome_unknown',resume_count:1 }],total:1,offset:0,limit:50 }));
    vi.stubGlobal('fetch',fetcher); render(<AdminView />);
    expect(await screen.findByLabelText('Operations alerts')).toHaveTextContent('spending threshold');
    expect(await screen.findByText('provider_outcome_unknown')).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Admin status filter'),'failed');
    await waitFor(() => expect(fetcher.mock.calls.some(call => call[0].includes('status=failed'))).toBe(true));
  });

  it('does not expose administration to ordinary accounts or load admin data', async () => {
    history.replaceState(null,'','#admin');
    const fetcher = vi.fn(async (path: string) => {
      if (path === '/auth/me') return Response.json({ user:{ id:'user',email:'user@example.com',name:'User',is_admin:false },csrf_token:'csrf' });
      if (path === '/account/usage') return Response.json(makeUsage());
      if (path === '/health') return Response.json({ status:'ok' });
      return Response.json({ items:[],total:0,offset:0,limit:20 });
    });
    vi.stubGlobal('fetch',fetcher); render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Administrator access is required');
    expect(screen.queryByRole('button',{ name:'Administration' })).not.toBeInTheDocument();
    expect(fetcher.mock.calls.some(call => call[0].startsWith('/admin/'))).toBe(false);
  });

  it('downloads an account ZIP with date filters and validates the range first', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('archive',{ headers:{ 'Content-Type':'application/zip' } }));
    vi.stubGlobal('fetch',fetcher);
    Object.defineProperty(URL,'createObjectURL',{ configurable:true,value:vi.fn().mockReturnValue('blob:archive') });
    Object.defineProperty(URL,'revokeObjectURL',{ configurable:true,value:vi.fn() });
    const clicked = vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(function(this: HTMLAnchorElement) {
      expect(this.download).toBe('deepresearch-account.zip');
    });
    render(<AccountExport />);
    await userEvent.type(screen.getByLabelText('Export research from'),'2026-02-01');
    await userEvent.type(screen.getByLabelText('Export research through'),'2026-01-01');
    await userEvent.click(screen.getByRole('button',{ name:'Download account ZIP' }));
    expect(screen.getByRole('alert')).toHaveTextContent('start date before'); expect(fetcher).not.toHaveBeenCalled();
    await userEvent.clear(screen.getByLabelText('Export research through'));
    await userEvent.type(screen.getByLabelText('Export research through'),'2026-11-10');
    await userEvent.click(screen.getByRole('button',{ name:'Download account ZIP' }));
    expect(await screen.findByRole('status')).toHaveTextContent('download started'); expect(clicked).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls[0][0]).toContain('created_from=2026-02-01');
    expect(fetcher.mock.calls[0][0]).toContain('created_to=2026-11-10');
  });

  it('includes retry timing for quota failures without exposing raw errors', async () => {
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(Response.json({ error:{ code:'daily_run_limit',message:'private-provider-details' } },
      { status:429,headers:{ 'Retry-After':'120' } })));
    await expect(api.create({ topic:'Topic',instructions:null,depth:'quick' })).rejects.toThrow('Retry after');
  });
});
