import { useState } from 'react';
import { AlertCircle, ArrowUpRight, BookOpen, CheckCheck, Download, FileText, LoaderCircle, Quote, ShieldCheck } from 'lucide-react';
import Markdown from 'react-markdown';
import { api } from '../api';
import { displaySourceTitle, safeSourceURL } from '../safety';
import type { Report, Source } from '../types';

function SourceList({ report, sources }: { report: Report; sources: Source[] }) {
  return <div className="source-list"><p className="muted">Retrieved pages are listed here even when they did not supply supported evidence.</p>
    {sources.length === 0 && <p>No usable pages were retrieved for this run.</p>}
    {sources.map((source, index) => {
      const href = safeSourceURL(source.original_url);
      const citation = Object.entries(report.citations).find(([, id]) => id === source.source_id)?.[0];
      const domain = href ? new URL(href).hostname.replace(/^www\./, '') : 'Link unavailable';
      return <article className="source-card" key={source.source_id}>
        <div className="source-top"><span className="domain-letter">{domain[0]?.toUpperCase()}</span><span>{domain}</span><span className={`source-citation ${citation ? 'cited' : ''}`}>{citation ? `Cited [${citation}]` : `Source ${index + 1}`}</span></div>
        <h3>{href ? <a href={href} target="_blank" rel="noopener noreferrer">{displaySourceTitle(source.title)}<ArrowUpRight size={16} /></a> : displaySourceTitle(source.title)}</h3>
        <div className="source-dates"><span>Published {source.published_at || 'unknown'}</span><span>Retrieved {new Date(source.retrieved_at).toLocaleDateString()}</span>{source.modified_at && <span>Updated {source.modified_at}</span>}</div>
        <code className="source-id">{source.source_id}</code>
        {!href && <p className="inline-error">An unsafe source URL was omitted.</p>}
      </article>;
    })}</div>;
}

export function SafeMarkdown({ report }: { report: Report }) {
  const allowed = new Set(report.sources.flatMap(source => [safeSourceURL(source.original_url), safeSourceURL(source.resolved_url)]).filter(Boolean));
  return <div className="markdown"><Markdown skipHtml disallowedElements={['img']} components={{
    a: ({ href, children }) => {
      const safe = safeSourceURL(href);
      return safe && allowed.has(safe) ? <a href={safe} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>;
    },
  }}>{report.body_markdown}</Markdown></div>;
}

export function ReportView({ report, runId, sources }: { report: Report; runId: string; sources: Source[] }) {
  const [tab, setTab] = useState('report');
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState('');
  const mock = report.uncertainties.some(value => /mock mode|mock sample/i.test(value));
  const notice = {
    complete: { title: 'Evidence checks passed', text: 'Citations connect the findings below to retrieved evidence. Review sources and limitations alongside the report.' },
    partial: { title: 'Supported answer with limitations', text: 'The findings below have checked support from at least two retrieved sources. Some requested details or claims remain unresolved; review the gaps and uncertainty.' },
    insufficient_evidence: { title: 'Evidence needs attention', text: 'This report does not have enough verified support for a reliable answer. Review the uncertainty section for the reasons.' },
  }[report.status];
  const tabs = [{ id: 'report', label: 'Report', icon: FileText },
    { id: 'sources', label: `Sources (${sources.length})`, icon: BookOpen },
    { id: 'evidence', label: 'Evidence', icon: Quote }];
  async function download() {
    setError(''); setDownloading(true);
    try { await api.download(runId); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'The report could not be downloaded.'); }
    finally { setDownloading(false); }
  }
  return <section className="panel report-panel" aria-label="Research results">
    <div className="report-toolbar"><div className="report-tabs" role="tablist" aria-label="Research results">
      {tabs.map(({ id, label, icon: Icon }, index) => <button role="tab" id={`tab-${id}`} aria-controls={`panel-${id}`} aria-selected={tab === id} tabIndex={tab === id ? 0 : -1} key={id} onClick={() => setTab(id)} onKeyDown={event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
          : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        setTab(tabs[next].id);
        document.getElementById(`tab-${tabs[next].id}`)?.focus();
      }}><Icon size={16} />{label}</button>)}
    </div><button className="button secondary download-button" onClick={download} disabled={downloading}>{downloading ? <LoaderCircle size={15} className="spin" /> : <Download size={15} />}<span>Download Markdown</span></button></div>
    {error && <p className="inline-error report-padding" role="alert">{error}</p>}
    <div className="report-content" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
      {tab === 'report' && <>
        <div className="report-label"><span><span className="small-dot" /> RESEARCH REPORT</span>{mock && <span className="mock-label">OFFLINE SAMPLE</span>}</div>
        <h2 className="report-title">{report.title}</h2>
        <div className={`evidence-notice ${report.status === 'complete' ? 'verified' : ''}`}>
          {report.status === 'complete' ? <ShieldCheck size={20} /> : <AlertCircle size={20} />}
          <div><strong>{notice.title}</strong><p>{notice.text}</p></div>
        </div>
        <SafeMarkdown report={report} />
        {Object.keys(report.citations).length > 0 && <div className="cited-sources"><h3>Follow the references</h3><div>{Object.entries(report.citations).map(([label, id]) => {
          const source = report.sources.find(item => item.source_id === id);
          const href = source && safeSourceURL(source.original_url);
          return source && (href ? <a key={label} href={href} target="_blank" rel="noopener noreferrer"><span>[{label}]</span>{displaySourceTitle(source.title)}<ArrowUpRight size={14} /></a>
            : <span className="unavailable-reference" key={label}>[{label}] {displaySourceTitle(source.title)} (unsafe URL omitted)</span>);
        })}</div></div>}
      </>}
      {tab === 'sources' && <><span className="eyebrow">THE SOURCE MATERIAL</span><h2 className="tab-title">A trail you can follow.</h2><SourceList report={report} sources={sources} /></>}
      {tab === 'evidence' && <><span className="eyebrow">CLAIMS & PASSAGES</span><h2 className="tab-title">What supports the answer?</h2>
        {report.claims.length === 0 && <div className="empty-evidence"><Quote size={25} /><h3>No claims were verified</h3><p>Check the report’s uncertainty section for extraction or provider failures.</p></div>}
        {report.claims.map(claim => <article className="claim-card" key={claim.claim_id}><span className={`badge verdict-${claim.verdict}`}><CheckCheck size={13} />{claim.verdict.replaceAll('_', ' ')}</span><h3>{claim.claim}</h3><p>{claim.rationale}</p>
          {claim.evidence_ids.map(id => {
            const item = report.evidence.find(evidence => evidence.evidence_id === id);
            const source = sources.find(source => source.source_id === item?.source_id);
            const assessment = claim.passage_assessments?.find(value => value.evidence_id === id);
            const relation = assessment ? { supports: 'Supports this claim', contradicts: 'Contradicts this claim', irrelevant: 'Not relevant to this claim' }[assessment.relation] : 'Assessment not recorded';
            return item && <blockquote key={id}><strong>{relation}</strong><p>{item.excerpt}</p>{assessment && <p>{assessment.explanation}</p>}<footer>{source ? displaySourceTitle(source.title) : item.source_id}</footer></blockquote>;
          })}</article>)}
      </>}
    </div>
  </section>;
}
