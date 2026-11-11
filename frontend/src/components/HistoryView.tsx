import { ArrowDownLeft, ChevronLeft, ChevronRight, Search } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks';
import type { HistoryFilters } from '../types';
import { DeleteResearch } from './DeleteResearch';

const empty: HistoryFilters = { q: '', status: '', depth: '', created_from: '', created_to: '' };

export function HistoryView({ onOpen, onChanged }: { onOpen: (id: string) => void; onChanged: () => void }) {
  const [filters, setFilters] = useState(empty);
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  useEffect(() => {
    if (search.trim() === filters.q) return;
    const timer = setTimeout(() => { setFilters(current => ({ ...current, q: search.trim() })); setOffset(0); }, 250);
    return () => clearTimeout(timer);
  }, [search, filters.q]);
  const load = useCallback((signal: AbortSignal) => api.list(offset, signal, filters), [offset, filters]);
  const { data: page, error, loading, refresh } = usePolling(load, 5000);
  function change(field: keyof HistoryFilters, value: string) { setFilters(current => ({ ...current, [field]: value })); setOffset(0); }
  const filtered = Object.values(filters).some(Boolean);
  return <><div className="history-heading"><span className="eyebrow">YOUR KNOWLEDGE, KEPT TOGETHER</span><h1>Every question.<br /><em>A place to return to.</em></h1><p>Search all your saved topics and instructions. Date filters use UTC.</p></div>
    <section className="panel history-panel"><div className="history-toolbar"><h2>Research history <span>{page?.total ?? '-'}</span></h2><label className="history-filter"><Search size={16} /><input value={search} onChange={event => setSearch(event.target.value)} aria-label="Search all research history" placeholder="Search all research..." maxLength={200} /></label></div>
      <div className="history-controls"><label>Status<select aria-label="Filter by status" value={filters.status} onChange={event => change('status', event.target.value)}><option value="">All statuses</option>{['queued','running','completed','failed','cancelled'].map(value => <option key={value} value={value}>{value}</option>)}</select></label>
        <label>Depth<select aria-label="Filter by depth" value={filters.depth} onChange={event => change('depth', event.target.value)}><option value="">All depths</option>{['quick','standard','deep'].map(value => <option key={value} value={value}>{value}</option>)}</select></label>
        <label>From (UTC)<input aria-label="Created from" type="date" value={filters.created_from} onChange={event => change('created_from', event.target.value)} /></label>
        <label>Through (UTC)<input aria-label="Created through" type="date" value={filters.created_to} onChange={event => change('created_to', event.target.value)} /></label>
        <button className="button secondary" onClick={() => { setFilters(empty); setSearch(''); setOffset(0); }}>Clear filters</button></div>
      {error && <p className="inline-error" role="alert">{error}<button className="text-button" onClick={refresh}>Try again</button></p>}
      {loading && <p role="status">Loading history...</p>}
      <div className="history-columns"><span>INVESTIGATION</span><span>DEPTH</span><span>STATUS</span><span>CREATED</span><span /></div>
      {(page?.items || []).map(run => <div key={run.id}><div className="history-row"><button className="history-topic history-open" onClick={() => onOpen(run.id)}><span className="history-icon"><ArrowDownLeft size={17} /></span><strong>{run.topic}</strong></button><span className="capitalize">{run.depth}</span><span><span className={`badge status-${run.status}`}><span className="small-dot" />{run.status}</span></span><span className="history-date">{new Date(run.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</span><span /></div>
        <div className="history-delete"><DeleteResearch id={run.id} topic={run.topic} onDeleted={() => { if (page?.items.length === 1 && offset) setOffset(Math.max(0,offset-20)); else refresh(); onChanged(); }} /></div></div>)}
      {!loading && !error && page?.items.length === 0 && <div className="empty-history"><Search size={26} /><h3>{filtered ? 'No matching research.' : 'A fresh page for your questions.'}</h3><p>{filtered ? 'Try another phrase or clear the filters.' : 'Start your first investigation to build your research history.'}</p></div>}
      <div className="pagination"><span>{page?.total ? `${offset + 1}-${Math.min(offset + 20, page.total)} of ${page.total} investigations` : 'No saved investigations'}</span><div><button className="button secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}><ChevronLeft size={15} />Newer</button><button className="button secondary" disabled={!page || offset + 20 >= page.total} onClick={() => setOffset(offset + 20)}>Older<ChevronRight size={15} /></button></div></div>
    </section></>;
}
