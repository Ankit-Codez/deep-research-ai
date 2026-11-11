import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { ReportView } from '../components/ReportView';
import { makeRun, makeUsage, RUN_ID } from './fixtures';
import type { CreateResearch, RunDetail } from '../types';

function backend(initial: RunDetail[] = [], completeOnPoll = false) {
  const runs = new Map(initial.map(run => [run.id, run]));
  let polls = 0;
  const submitted: CreateResearch[] = [];
  const fetcher = vi.fn(async (path: string, options: RequestInit = {}) => {
    const url = new URL(path, 'http://localhost');
    if (url.pathname === '/auth/me') return Response.json({ user: { id: 'test-user', email: 'tester@example.com', name: 'Test User' }, csrf_token: 'session-csrf' });
    if (url.pathname === '/health') return Response.json({ status: 'ok' });
    if (url.pathname === '/account/usage') return Response.json(makeUsage());
    if (url.pathname === '/research' && options.method === 'POST') {
      const payload = JSON.parse(options.body as string); submitted.push(payload);
      const run = makeRun({ ...payload, status: 'queued', report: null, current_step: null, progress: 0, step_status: {} });
      runs.set(run.id, run); return Response.json(run, { status: 202 });
    }
    if (url.pathname === '/research') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const matches = [...runs.values()].filter(run => run.topic.toLowerCase().includes(q));
      const offset = Number(url.searchParams.get('offset'));
      return Response.json({ items: matches.slice(offset,offset+20), total: matches.length, offset, limit: 20 });
    }
    if (url.pathname.endsWith('/cancel')) {
      const run = { ...runs.get(RUN_ID)!, status: 'cancelled' as const }; runs.set(RUN_ID, run); return Response.json(run);
    }
    if (url.pathname.endsWith('/sources')) return Response.json(runs.get(RUN_ID)?.report?.sources || []);
    if (url.pathname === `/research/${RUN_ID}`) {
      const run = runs.get(RUN_ID);
      if (completeOnPoll && run?.status === 'queued' && ++polls >= 2) runs.set(RUN_ID, makeRun());
      return Response.json(runs.get(RUN_ID));
    }
    throw new Error('Unexpected test endpoint: ' + url.pathname);
  });
  vi.stubGlobal('fetch', fetcher);
  return { submitted, fetcher, runs };
}

describe('dashboard user flows', () => {
  it('requires a topic and does not enqueue blank input', async () => {
    const { submitted } = backend(); render(<App />);
    await userEvent.click(await screen.findByRole('button', { name: /Start research/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a research topic');
    expect(submitted).toEqual([]);
  });
  it('fills a suggestion without automatically starting paid research', async () => {
    const { submitted } = backend(); render(<App />);
    await userEvent.click(await screen.findByRole('button', { name: /How does retrieval-augmented generation/ }));
    expect(screen.getByLabelText('Research topic')).toHaveValue('How does retrieval-augmented generation ground an answer?');
    expect(submitted).toEqual([]);
  });
  it('starts a fresh draft when New research is selected', async () => {
    backend(); render(<App />);
    await userEvent.type(await screen.findByLabelText('Research topic'), 'An unfinished draft');
    await userEvent.click(screen.getByRole('button', { name: /New research/ }));
    expect(screen.getByLabelText('Research topic')).toHaveValue('');
  });
  it('submits instructions and depth, then polls through to a cited report', async () => {
    const { submitted } = backend([], true); render(<App />);
    await userEvent.type(await screen.findByLabelText('Research topic'), 'How does retrieval ground an answer?');
    await userEvent.click(screen.getByText('Add instructions'));
    await userEvent.type(screen.getByLabelText('Optional instructions'), 'Focus on primary evidence.');
    await userEvent.click(screen.getByRole('radio', { name: /Quick/ }));
    await userEvent.click(screen.getByRole('button', { name: /Start research/ }));
    expect(await screen.findByText('Your question is in the queue.')).toBeInTheDocument();
    expect(submitted).toEqual([{ topic: 'How does retrieval ground an answer?', instructions: 'Focus on primary evidence.', depth: 'quick' }]);
    expect(await screen.findByText('Evidence checks passed', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Download Markdown/ })).toBeEnabled();
    expect(screen.getByText('Supported claims')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: 'Sources (2)' }));
    expect(screen.getAllByRole('link')).toHaveLength(3); // Skip-to-content plus two actual sources.
    expect(screen.getByText('Cited [1]')).toBeInTheDocument();
  });
  it('opens previous runs and keeps failed evidence visible', async () => {
    const run = makeRun(); run.report!.status = 'insufficient_evidence';
    run.report!.evidence = []; run.report!.claims = []; run.report!.citations = {};
    run.report!.body_markdown = 'Insufficient evidence.\n\n## Uncertainty\n\n- The provider output was truncated.';
    backend([run]); render(<App />);
    await userEvent.click(await screen.findByRole('button', { name: /Open research:/ }));
    expect(await screen.findByText('Evidence needs attention')).toBeInTheDocument();
    expect(screen.getByText('The provider output was truncated.')).toBeInTheDocument();
    expect(location.hash).toBe(`#run/${RUN_ID}`);
  });
  it('cancels a queued run and retains it in history', async () => {
    const { runs } = backend([makeRun({ status: 'queued', report: null, step_status: {} })]); render(<App />);
    await userEvent.click(await screen.findByRole('button', { name: /Open research:/ }));
    await userEvent.click(await screen.findByRole('button', { name: /Cancel research/ }));
    expect(await screen.findByText('Research cancelled.')).toBeInTheDocument();
    expect(runs.get(RUN_ID)?.status).toBe('cancelled');
  });
  it('shows a safe backend error and remains usable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('sensitive-provider-key')));
    render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Check that FastAPI is running');
    expect(document.body.textContent).not.toContain('sensitive-provider-key');
    expect(screen.getByRole('button', { name: /^Sign in$/ })).toBeInTheDocument();
  });
  it('lists and filters research history', async () => {
    backend([makeRun()]); render(<App />);
    await userEvent.click(await screen.findByRole('button', { name: /Research history/ }));
    const filter = await screen.findByRole('textbox', { name: /Search all research history/ });
    await userEvent.type(filter, 'no matching question');
    expect(await screen.findByText('No matching research.')).toBeInTheDocument();
    await userEvent.clear(filter);
    const history = screen.getByRole('heading', { name: /Research history/ }).closest('section')!;
    expect(await within(history).findByRole('button', { name: /^How does retrieval/ })).toBeInTheDocument();
  });
  it('shows evidence verdicts and their actual supporting passages', async () => {
    const run = makeRun(); render(<ReportView report={run.report!} runId={run.id} sources={run.report!.sources} />);
    await userEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
    expect(screen.getByRole('tabpanel')).toHaveTextContent(run.report!.claims[0].claim);
    expect(screen.getByRole('tabpanel')).toHaveTextContent(run.report!.evidence[0].excerpt);
  });
  it('supports keyboard navigation between report tabs', async () => {
    const run = makeRun(); render(<ReportView report={run.report!} runId={run.id} sources={run.report!.sources} />);
    screen.getByRole('tab', { name: 'Report' }).focus();
    await userEvent.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'Sources (2)' })).toHaveFocus();
    expect(screen.getByRole('tab', { name: 'Sources (2)' })).toHaveAttribute('aria-selected', 'true');
    await userEvent.keyboard('{End}');
    expect(screen.getByRole('tab', { name: 'Evidence' })).toHaveFocus();
  });
});
