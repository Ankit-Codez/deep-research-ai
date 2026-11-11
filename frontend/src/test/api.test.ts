import { describe, expect, it, vi } from 'vitest';
import { api } from '../api';
import { RUN_ID, makeRun } from './fixtures';

describe('same-origin API client', () => {
  it('posts only topic, instructions and depth to the existing API', async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json(makeRun()));
    vi.stubGlobal('fetch', fetcher);
    const payload = { topic: 'A focused question', instructions: null, depth: 'quick' as const };
    await api.create(payload);
    expect(fetcher).toHaveBeenCalledWith('/research', expect.objectContaining({ method: 'POST', body: JSON.stringify(payload), redirect: 'error' }));
    expect(fetcher.mock.calls[0][1].headers).toEqual({ 'Content-Type': 'application/json' });
  });
  it('does not expose backend exception bodies', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('secret-backend-key', { status: 500 })));
    await expect(api.get(RUN_ID)).rejects.toThrow('The backend could not complete this request.');
    await expect(api.get(RUN_ID)).rejects.not.toThrow('secret');
  });
  it('reports connection failures clearly', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('secret network URL')));
    await expect(api.list()).rejects.toThrow('Check that FastAPI is running');
  });
  it('rejects an invalid run ID before sending anything', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    expect(() => api.get('../../private')).toThrow('Invalid research run ID');
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('rejects unreadable JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not json')));
    await expect(api.health()).rejects.toThrow('unreadable response');
  });
  it('downloads the server-sanitized Markdown endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('# Safe report', { headers: { 'Content-Type': 'text/markdown' } })));
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn().mockReturnValue('blob:test-report') });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function(this: HTMLAnchorElement) {
      expect(this.download).toBe(`research-${RUN_ID}.md`);
      expect(this.href).toBe('blob:test-report');
    });
    await api.download(RUN_ID);
    expect(fetch).toHaveBeenCalledWith(`/research/${RUN_ID}/report.md`, expect.anything());
    expect(clicked).toHaveBeenCalledOnce();
  });
});
