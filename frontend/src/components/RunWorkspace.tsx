import { useCallback, useState } from 'react';
import { ArrowLeft, BookOpen, Check, Circle, FileCheck2, GitBranch, LoaderCircle, Quote, RefreshCw, Square, X } from 'lucide-react';
import { api } from '../api';
import { usePolling } from '../hooks';
import type { RunDetail } from '../types';
import { ReportView } from './ReportView';
import { DeleteResearch } from './DeleteResearch';
import { useRunEvents } from '../events';

const stages = [['planner', 'Planner', 'Building a focused research plan'], ['search', 'Search', 'Finding and reading source material'],
  ['evidence', 'Evidence', 'Connecting claims to exact passages'], ['verification', 'Verification', 'Checking support and disagreement'], ['writer', 'Writer', 'Assembling the research report']];
const isActive = (run: RunDetail) => ['queued', 'running'].includes(run.status);

export function RunWorkspace({ id, onBack, onChanged = () => {} }: { id: string; onBack: () => void; onChanged?: () => void }) {
  const load = useCallback((signal: AbortSignal) => api.get(id, signal), [id]);
  const { data: run, error, loading, refresh } = usePolling(load, 2000, isActive);
  const loadSources = useCallback((signal: AbortSignal) => api.sources(id, signal), [id]);
  const sourcesState = usePolling(loadSources, run && !isActive(run) ? 0 : 2000);
  const [cancelling, setCancelling] = useState(false);
  const [actionError, setActionError] = useState('');
  const [resuming, setResuming] = useState(false);
  const progressRefresh = useCallback(() => { refresh(); sourcesState.refresh(); }, [refresh,sourcesState.refresh]);
  const live = useRunEvents(id,Boolean(run && isActive(run)),progressRefresh);
  async function resume() {
    setResuming(true); setActionError('');
    try { await api.resume(id); refresh(); onChanged(); }
    catch (cause) { setActionError(cause instanceof Error ? cause.message : 'This run could not be resumed.'); }
    finally { setResuming(false); }
  }
  async function cancel() {
    setCancelling(true); setActionError('');
    try { await api.cancel(id); refresh(); sourcesState.refresh(); onChanged(); }
    catch (cause) { setActionError(cause instanceof Error ? cause.message : 'Could not cancel research.'); }
    finally { setCancelling(false); }
  }
  if (loading && !run) return <div className="loading-view" role="status"><LoaderCircle className="spin" size={24} /><p>Opening your research…</p></div>;
  if (!run) return <div className="panel error-view" role="alert"><h2>We couldn’t open this research.</h2><p>{error}</p><button className="button secondary" onClick={refresh}><RefreshCw size={15} />Try again</button></div>;
  const sources = run.report?.sources || sourcesState.data || [];
  const report = run.report;
  const active = isActive(run);
  const supported = report?.claims.filter(claim => claim.verdict === 'supported').length;
  const statusLabel = active ? (run.status === 'queued' ? 'In the queue' : 'Research in progress') : run.status === 'completed' ? 'Research finished' : `Research ${run.status}`;
  return <>
    <button className="back-button" onClick={onBack}><ArrowLeft size={15} />Back to workspace</button>
    <div className="run-heading"><div><span className="eyebrow">{run.depth.toUpperCase()} INVESTIGATION</span><h1>{run.topic}</h1></div><span className={`badge status-${run.status}`}><span className="small-dot" />{statusLabel}</span></div>
    <div className="run-meta"><span>{new Date(run.created_at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}</span><span>Run {run.id.slice(0, 8)}</span><span>Round {Math.max(1, run.iteration)}</span></div>
    <div className="run-delete"><DeleteResearch id={run.id} topic={run.topic} onDeleted={() => { onChanged(); onBack(); }} /></div>
    {active && <p className="stream-status" role="status">{live ? 'Live progress connected' : 'Progress polling active; live stream will reconnect when available.'}</p>}
    {run.status === 'failed' && <section className="panel recovery-panel"><h2>Research recovery</h2>{run.resumable ? <><p>Resume from the saved checkpoint. Completed steps and saved provider results are reused; the original allowance and limits still apply.</p><button className="button secondary" disabled={resuming} onClick={resume}>{resuming ? 'Resuming...' : 'Resume research'}</button></> : <p>{run.resume_blocked_reason || 'No safe resumable checkpoint is available. Start a new investigation explicitly if needed.'}</p>}<small>Resumes used: {run.resume_count || 0}</small></section>}
    {run.instructions && <p className="run-instructions"><strong>Your focus</strong> {run.instructions}</p>}
    {(error || actionError || sourcesState.error) && <div className="inline-error" role="alert">{error || actionError || sourcesState.error}</div>}
    <div className="metric-grid">{[
      { label: 'Retrieved sources', value: sources.length, icon: BookOpen },
      { label: 'Validated passages', value: report ? report.evidence.length : '—', icon: Quote },
      { label: 'Supported claims', value: supported ?? '—', icon: FileCheck2 },
      { label: 'Research rounds', value: Math.max(1, run.iteration), icon: GitBranch },
    ].map(({ label, value, icon: Icon }) => <div className="metric panel" key={label}><span>{label}<Icon size={16} /></span><strong>{value}</strong></div>)}</div>
    <div className="results-layout"><div className="results-main">
      {active && <section className="panel progress-panel" aria-live="polite"><span className="eyebrow">{run.status === 'queued' ? 'READY WHEN YOUR WORKER IS' : 'FOLLOWING THE EVIDENCE'}</span><div className="progress-heading"><h2>{run.status === 'queued' ? 'Your question is in the queue.' : stages.find(([key]) => key === run.current_step)?.[2] || 'Refining the investigation'}</h2><LoaderCircle size={23} className="spin" /></div><p>{run.status === 'queued' ? 'The research worker will pick this up shortly. This page updates automatically.' : 'You can leave this page and return from your research history.'}</p><div className="progress-track" role="progressbar" aria-label="Research progress" aria-valuenow={Math.round(run.progress * 100)} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${Math.max(0, Math.min(100, run.progress * 100))}%` }} /></div><div className="progress-footer"><span>{Math.round(run.progress * 100)}% · {run.current_step || 'Queued'}</span><button className="text-button" onClick={cancel} disabled={cancelling}><Square size={12} />{cancelling ? 'Cancelling…' : 'Cancel research'}</button></div></section>}
      {run.status === 'cancelled' && <section className="panel terminal-message"><Square size={24} /><h2>Research cancelled.</h2><p>This run stays in your history. Start a new investigation whenever you’re ready.</p></section>}
      {run.status === 'failed' && <section className="panel terminal-message" role="alert"><X size={24} /><h2>This investigation couldn’t finish.</h2><p>{run.error || 'The worker could not complete this run. You can start a new investigation.'}</p></section>}
      {report && report.limitations.some(item => /provider|quota|timed out|network|structured/i.test(item)) && <section className="panel provider-notice" aria-label="Provider and allowance notices"><h2>Provider or allowance issue</h2><p>A completed workflow can still lack usable evidence when its providers fail. Review these diagnostics before starting new paid work.</p><ul>{[...new Set(report.limitations.filter(item => /provider|quota|timed out|network|structured/i.test(item)))].slice(0,5).map(item => <li key={item}>{item}</li>)}</ul><p>Check provider availability and quotas, or use mock mode for offline development. A new investigation makes new requests.</p></section>}
      {report && <ReportView key={run.id} report={report} sources={sources} runId={run.id} />}
      {run.status === 'completed' && !report && <section className="panel terminal-message" role="alert"><h2>No report is available.</h2><p>This completed run has no saved report. Start a new investigation or check the worker logs.</p></section>}
    </div><aside className="activity-panel panel"><div className="activity-heading"><span className="eyebrow">THE RESEARCH TEAM</span><span className="team-count">5 agents</span></div><h2>A clear path to an answer.</h2><ol className="agent-list">{stages.map(([key, title, description]) => {
      const status = run.step_status[key] || 'pending';
      const current = run.status === 'running' && run.current_step === key;
      return <li className={`${status} ${current ? 'current' : ''}`} key={key}><span className="agent-node">{current ? <LoaderCircle size={15} className="spin" /> : status === 'completed' ? <Check size={14} /> : status === 'failed' ? <X size={14} /> : <Circle size={10} />}</span><div><strong>{title}<span>{current ? 'Working' : status === 'pending' ? 'Waiting' : status}</span></strong><p>{description}</p></div></li>;
    })}</ol>{run.step_status.retry && !['pending', 'skipped'].includes(run.step_status.retry) && <p className="retry-note"><RefreshCw size={14} />Follow-up research: {run.step_status.retry}</p>}<div className="activity-note"><Quote size={17} /><p>A retrieved page becomes evidence only after its passages are checked.</p></div></aside></div>
  </>;
}
