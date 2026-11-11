import type { AdminOverview, AdminRunPage, AuthSession, CreateResearch, HistoryFilters, RunDetail, RunPage, Source, UsageDashboard } from './types';

export class APIError extends Error {
  constructor(message: string, public status?: number, public retryAfter?: number) { super(message); }
}
let csrfToken: string | null = null;
export function clearAuthSession() { csrfToken = null; }
const messages: Record<number, string> = {
  401: 'Your session has expired. Sign in again.',
  403: 'This request could not be authorized. Refresh your session and try again.',
  404: 'This research run could not be found.',
  409: 'This action is unavailable for the current run status. Refresh and try again.',
  422: 'Check your topic, instructions, and research depth, then try again.',
  503: 'Research history is temporarily unavailable. Try again shortly.',
  429: 'Too many sign-in attempts. Please wait before trying again.',
};
const codeMessages: Record<string, string> = {
  active_run_limit: 'Your active research limit is reached. Wait for or cancel a run.',
  daily_run_limit: 'Your daily research allowance is exhausted. Check Usage & account for the reset time.',
  daily_token_limit: 'Not enough daily token allowance for this depth. Choose a lower depth or wait for reset.',
  daily_cost_limit: 'Not enough daily estimated-cost allowance for this research. Check Usage & account.',
  idempotency_conflict: 'This submission key was used for different research. Start a new submission.',
  submission_deleted: 'The original research was deleted. Start a new submission.',
  invalid_date_range: 'Choose a valid date range with the start before the end.',
  resume_blocked: 'This run cannot safely resume. A provider request may already be billed, or no compatible checkpoint exists. Start a new investigation explicitly if needed.',
  resume_limit: 'This investigation reached its resume limit. Start a new investigation if needed.',
  resume_mode_mismatch: 'Resume requires the original mock or real mode. Ask the operator to restore that mode.',
  export_too_large: 'This account export is too large. Select a narrower research date range.',
  admin_required: 'This page requires administrator access.',
};

async function request(path: string, options: RequestInit = {}, signal?: AbortSignal) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener('abort', abort, { once: true });
  const timeout = window.setTimeout(abort, 15000);
  try {
    const response = await fetch(path, {
      ...options, signal: controller.signal, redirect: 'error', credentials: 'same-origin',
      headers: { ...(csrfToken && ['POST', 'DELETE'].includes(options.method || '') ? { 'X-CSRF-Token': csrfToken } : {}),
        ...options.headers, ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
    });
    // Do not display raw server/SDK exception bodies in the browser.
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('/auth/')) {
        clearAuthSession(); window.dispatchEvent(new Event('auth-required'));
      }
      let code = '';
      try { const body = await response.json(); code = typeof body?.error?.code === 'string' ? body.error.code : ''; } catch { /* Never display raw server content. */ }
      const message = path === '/auth/me' && options.method === 'DELETE' && response.status === 401 ? 'The current password is incorrect.'
        : codeMessages[code] || (path === '/auth/login' && response.status === 401 ? 'Invalid email or password.'
        : path === '/auth/register' && response.status === 409 ? 'An account could not be created with these details. Try signing in.'
        : messages[response.status] || 'The backend could not complete this request. Please try again.');
      const value = Number(response.headers.get('Retry-After'));
      const retryAfter = Number.isFinite(value) && value > 0 ? Math.ceil(value) : undefined;
      const guidance = retryAfter && response.status === 429 ? ` Retry after ${new Date(Date.now()+retryAfter*1000).toLocaleString()}.` : '';
      throw new APIError(message+guidance, response.status, retryAfter);
    }
    return response;
  } catch (error) {
    if (signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
    if (error instanceof APIError) throw error;
    throw new APIError(controller.signal.aborted
      ? 'The backend request timed out. Please try again.'
      : 'Cannot connect to the research backend. Check that FastAPI is running.');
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener('abort', abort);
  }
}

function runPath(id: string) {
  if (!/^[a-f\d]{8}(-[a-f\d]{4}){3}-[a-f\d]{12}$/i.test(id)) throw new APIError('Invalid research run ID.');
  return `/research/${id}`;
}

async function json<T>(path: string, options?: RequestInit, signal?: AbortSignal): Promise<T> {
  const response = await request(path, options, signal);
  try { return await response.json() as T; }
  catch { throw new APIError('The backend returned an unreadable response. Please try again.'); }
}

export const api = {
  me: async (): Promise<AuthSession | null> => {
    try { const session = await json<AuthSession>('/auth/me'); csrfToken = session.csrf_token; return session; }
    catch (error) { if (error instanceof APIError && error.status === 401) { clearAuthSession(); return null; } throw error; }
  },
  authenticate: async (mode: 'login' | 'register', payload: { email: string; password: string; name?: string }) => {
    const preauth = await json<{ csrf_token: string }>('/auth/csrf');
    const session = await json<AuthSession>(`/auth/${mode}`, { method: 'POST',
      body: JSON.stringify(payload), headers: { 'X-CSRF-Token': preauth.csrf_token } });
    csrfToken = session.csrf_token;
    return session;
  },
  logout: async () => { await request('/auth/logout', { method: 'POST' }); clearAuthSession(); },
  deleteAccount: async (password: string, confirmation: string) => {
    await request('/auth/me', { method: 'DELETE', body: JSON.stringify({ password, confirmation }) }); clearAuthSession();
  },
  usage: (signal?: AbortSignal) => json<UsageDashboard>('/account/usage', {}, signal),
  adminOverview: (signal?: AbortSignal) => json<AdminOverview>('/admin/overview', {}, signal),
  adminRuns: (offset = 0, status = '', signal?: AbortSignal) => json<AdminRunPage>(`/admin/runs?limit=50&offset=${offset}${status ? '&status='+encodeURIComponent(status) : ''}`, {}, signal),
  eventsURL: (id: string) => `${runPath(id)}/events`,
  resume: (id: string) => json<RunDetail>(`${runPath(id)}/resume`, { method: 'POST' }),
  exportAccount: async (from = '', through = '') => {
    const params = new URLSearchParams();
    if (from) params.set('created_from', from);
    if (through) params.set('created_to', through);
    const response = await request('/account/export'+(params.size ? '?'+params : ''));
    saveBlob(await response.blob(), 'deepresearch-account.zip');
  },
  health: (signal?: AbortSignal) => json<{ status: string }>('/health', {}, signal),
  list: (offset = 0, signal?: AbortSignal, filters?: HistoryFilters) => {
    const params = new URLSearchParams({ limit: '20', offset: String(offset) });
    if (filters) Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    return json<RunPage>(`/research?${params}`, {}, signal);
  },
  get: (id: string, signal?: AbortSignal) => json<RunDetail>(runPath(id), {}, signal),
  sources: (id: string, signal?: AbortSignal) => json<Source[]>(`${runPath(id)}/sources`, {}, signal),
  create: (payload: CreateResearch, idempotencyKey?: string) => json<RunDetail>('/research', { method: 'POST',
    body: JSON.stringify(payload), ...(idempotencyKey ? { headers: { 'Idempotency-Key': idempotencyKey } } : {}) }),
  deleteRun: async (id: string) => { await request(runPath(id), { method: 'DELETE' }); },
  cancel: (id: string) => json<RunDetail>(`${runPath(id)}/cancel`, { method: 'POST' }),
  download: async (id: string) => {
    const response = await request(`${runPath(id)}/report.md`);
    saveBlob(await response.blob(), `research-${id}.md`);
  },
};

function saveBlob(blob: Blob, name: string) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
