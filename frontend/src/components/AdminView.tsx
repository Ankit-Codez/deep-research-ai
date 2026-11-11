import { useCallback, useState } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks';

const overview = (signal: AbortSignal) => api.adminOverview(signal);
const money = (value: number | null) => value === null ? 'Unavailable' : `$${value.toFixed(4)}`;

export function AdminView() {
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const totals = usePolling(overview,10000);
  const load = useCallback((signal: AbortSignal) => api.adminRuns(offset,status,signal),[offset,status]);
  const jobs = usePolling(load,5000);
  return <><div className="history-heading"><span className="eyebrow">ADMINISTRATOR</span><h1>Research operations</h1><p>Queue, failures and estimated spending. Research topics and source content stay out of this view.</p></div>
    {(totals.error || jobs.error) && <p role="alert" className="inline-error">{totals.error || jobs.error}</p>}
    {totals.data && <><div className="usage-grid">{Object.entries(totals.data.counts).map(([key,value]) => <div className="panel usage-metric" key={key}><span className="capitalize">{key}</span><strong>{value}</strong></div>)}</div>
      <section className="panel usage-panel"><h2>Spending and reliability</h2><div className="usage-grid">{[
        ['Estimated cost today',money(totals.data.estimated_cost_usd)],
        ['Estimated / reserved exposure',money(totals.data.estimated_reserved_exposure_usd)],
        ['Spending alert threshold',money(totals.data.spending_alert_usd)],
        ['Oldest queued run',`${Math.round(totals.data.oldest_queued_seconds)} seconds`],
        ['Failed jobs (24h)',totals.data.failed_last_24h],
        ['Insufficient reports (24h)',totals.data.insufficient_last_24h],
        ['Unknown provider outcomes',totals.data.unknown_provider_calls],
        ['Runs missing pricing',totals.data.unknown_cost_runs],
      ].map(([label,value]) => <div className="usage-metric" key={label}><span>{label}</span><strong>{value}</strong></div>)}</div><p className="usage-note">Amounts are UTC-day estimates and conservative reservations, not provider invoices. Alerts are displayed here; no email or external notifications are sent.</p></section>
      <section className="panel usage-panel" aria-label="Operations alerts"><h2>Alerts</h2>{totals.data.alerts.length ? totals.data.alerts.map(alert => <p className="operations-alert" role="status" key={alert.code}><strong>{alert.severity}</strong> {alert.message}</p>) : <p className="usage-note">No current threshold alerts.</p>}</section></>}
    <section className="panel usage-panel"><div className="section-heading"><h2>Queue and recent runs</h2><label>Status<select aria-label="Admin status filter" value={status} onChange={event => { setStatus(event.target.value); setOffset(0); }}><option value="">All statuses</option>{['queued','running','completed','failed','cancelled'].map(value => <option value={value} key={value}>{value}</option>)}</select></label></div>
      <div className="operations-table"><table><thead><tr><th>Run</th><th>Status</th><th>Step</th><th>Progress</th><th>Failure code</th><th>Resumes</th></tr></thead><tbody>{jobs.data?.items.map(run => <tr key={run.id}><td title={run.id}>{run.id.slice(0,8)}</td><td>{run.status}</td><td>{run.current_step || 'Queued'}</td><td>{Math.round(run.progress*100)}%</td><td>{run.error_code || 'None'}</td><td>{run.resume_count}</td></tr>)}</tbody></table></div>
      {!jobs.loading && !jobs.data?.items.length && <p className="usage-note">No runs match this status.</p>}<div className="action-row"><button className="button secondary" disabled={!offset} onClick={() => setOffset(Math.max(0,offset-50))}>Newer</button><button className="button secondary" disabled={!jobs.data || offset+50 >= jobs.data.total} onClick={() => setOffset(offset+50)}>Older</button></div>
    </section></>;
}
