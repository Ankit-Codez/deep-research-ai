import { useState } from 'react';
import type { FormEvent } from 'react';
import { api } from '../api';
import type { UsageDashboard, User } from '../types';
import { AccountExport } from './AccountExport';

const number = (value: number | null) => value === null ? 'Not reported' : value.toLocaleString();
const dollars = (value: number | null) => value === null ? 'Unavailable' : `$${value.toFixed(4)}`;

export function Allowance({ usage }: { usage: UsageDashboard }) {
  return <section className="panel allowance" aria-label="Remaining research allowance"><strong>Today's allowance</strong><span>{number(usage.remaining.runs)} runs remaining</span><span>{number(usage.remaining.tokens)} tokens available</span><span>{usage.remaining.active_slots} active slots free</span>{usage.remaining.cost_usd !== null && <span>{dollars(usage.remaining.cost_usd)} estimated-cost allowance</span>}<small>Resets {new Date(usage.resets_at).toLocaleString()} (midnight UTC). Active runs reserve allowance.</small></section>;
}

export function AccountView({ user, usage, error, refresh, onDeleted }: {
  user: User; usage: UsageDashboard | null; error: string; refresh: () => void; onDeleted: () => void;
}) {
  const [period, setPeriod] = useState<'today' | 'lifetime'>('today');
  const [deleting, setDeleting] = useState(false);
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  async function remove(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!password || confirmation !== 'DELETE') { setActionError('Enter your current password and type DELETE to confirm.'); return; }
    setBusy(true); setActionError('');
    try { await api.deleteAccount(password, confirmation); setPassword(''); onDeleted(); }
    catch (cause) { setPassword(''); setActionError(cause instanceof Error ? cause.message : 'Account deletion failed.'); }
    finally { setBusy(false); }
  }
  const totals = usage?.[period];
  return <><div className="history-heading"><span className="eyebrow">YOUR PRIVATE WORKSPACE</span><h1>Usage &amp; account</h1><p>{user.email}</p></div>
    {error && <p className="inline-error" role="alert">{error}<button className="text-button" onClick={refresh}>Refresh usage</button></p>}
    {usage ? <><Allowance usage={usage} /><section className="panel usage-panel"><div className="section-heading"><h2>Research usage</h2><label>Period<select aria-label="Usage period" value={period} onChange={event => setPeriod(event.target.value as 'today' | 'lifetime')}><option value="today">Today (UTC)</option><option value="lifetime">All time</option></select></label></div>
      <div className="usage-grid">{[
        ['Research submissions', number(totals!.submissions)], ['Provider-reported tokens', number(totals!.reported_tokens)],
        ['Admitted token estimates', number(totals!.admitted_tokens)], ['Estimated provider cost', dollars(totals!.estimated_cost_usd)],
        ['Model calls', number(totals!.llm_calls)], ['Search calls', number(totals!.searches)],
        ['Settled quota tokens', number(totals!.charged_tokens)], ['Reserved quota tokens', number(totals!.reserved_tokens)],
      ].map(([label,value]) => <div className="usage-metric" key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
      <p className="usage-note">Token allowance uses conservative admission estimates, not a provider bill. Costs use the configured rates saved when each run was submitted. {totals!.runs_without_reported_usage} runs have no reported token usage; {totals!.runs_without_cost} have no cost estimate. Known totals may be partial.</p>
      <p className="usage-note">Queued cancellations release their token and cost reservations. Interrupted, failed or cancelled running jobs retain their full reservation because an in-flight provider call may still be billed. Deleting research does not reset usage.</p>
      <p className="usage-note">Daily limits: {number(usage.limits.daily_runs)} runs, {number(usage.limits.daily_tokens)} quota tokens, {usage.limits.max_active_runs} active runs. Estimated-cost quota: {usage.limits.daily_cost_usd === null ? 'not enabled' : dollars(usage.limits.daily_cost_usd)}.</p>
    </section></> : !error && <p role="status">Loading personal usage...</p>}
    <AccountExport />
    <section className="panel account-danger"><h2>Delete account</h2><p>This permanently removes your account, sessions, research and usage records from the application database. Sources referenced by another user's research stay with that research. Backup copies follow the operator's retention policy.</p>
      {!deleting ? <button className="button danger" onClick={() => setDeleting(true)}>Delete my account</button> : <form onSubmit={remove} noValidate><label>Current password<input type="password" autoComplete="current-password" aria-label="Current password" maxLength={128} disabled={busy} value={password} onChange={event => setPassword(event.target.value)} /></label><label>Type DELETE to confirm<input aria-label="Account deletion confirmation" maxLength={6} disabled={busy} value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>{actionError && <p className="inline-error" role="alert">{actionError}</p>}<div className="action-row"><button className="button secondary" type="button" disabled={busy} onClick={() => { setDeleting(false); setPassword(''); setConfirmation(''); setActionError(''); }}>Keep my account</button><button className="button danger" type="submit" disabled={busy}>{busy ? 'Deleting...' : 'Permanently delete my account'}</button></div></form>}
    </section></>;
}
