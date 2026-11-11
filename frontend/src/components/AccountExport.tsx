import { useState } from 'react';
import { api } from '../api';

export function AccountExport() {
  const [from, setFrom] = useState('');
  const [through, setThrough] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  async function download() {
    if (from && through && from > through) { setError('Choose a start date before the end date.'); return; }
    setBusy(true); setError(''); setDone(false);
    try { await api.exportAccount(from,through); setDone(true); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Account export failed.'); }
    finally { setBusy(false); }
  }
  return <section className="panel usage-panel"><h2>Export your account</h2><p className="usage-note">Download a ZIP containing your profile, research, sources, Markdown reports and numeric usage. Passwords, sessions, API keys and internal execution records are excluded. Optional dates filter research using UTC; usage includes all retained records.</p>
    <div className="history-controls export-controls"><label>Research from<input type="date" aria-label="Export research from" value={from} disabled={busy} onChange={event => setFrom(event.target.value)} /></label><label>Research through<input type="date" aria-label="Export research through" value={through} disabled={busy} onChange={event => setThrough(event.target.value)} /></label><button className="button secondary" disabled={busy} onClick={download}>{busy ? 'Preparing export...' : 'Download account ZIP'}</button></div>
    {error && <p className="inline-error" role="alert">{error}</p>}{done && <p className="usage-note" role="status">Account export download started.</p>}
  </section>;
}
