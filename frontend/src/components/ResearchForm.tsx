import { useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowUpRight, BookOpen, Check, LoaderCircle, Search, SlidersHorizontal, Sparkles, Zap } from 'lucide-react';
import { api } from '../api';
import type { Depth, RunDetail } from '../types';

const depths = [
  { id: 'quick', title: 'Quick', description: 'A focused first look', icon: Zap },
  { id: 'standard', title: 'Standard', description: 'A balanced investigation', icon: Search },
  { id: 'deep', title: 'Deep', description: 'More room to investigate', icon: BookOpen },
] as const;
const suggestions = ['How does retrieval-augmented generation ground an answer?',
  'What are the trade-offs of small language models?', 'How do AI agents verify their sources?'];

export function ResearchForm({ onCreated }: { onCreated: (run: RunDetail) => void }) {
  const [topic, setTopic] = useState('');
  const [instructions, setInstructions] = useState('');
  const [depth, setDepth] = useState<Depth>('standard');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const attempt = useRef<{ payload: string; key: string } | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
    if (!topic.trim()) { setError('Enter a research topic to get started.'); return; }
    setError('');
    setSubmitting(true);
    const payload = { topic: topic.trim(), instructions: instructions.trim() || null, depth };
    const signature = JSON.stringify(payload);
    if (attempt.current?.payload !== signature) attempt.current = { payload: signature, key: crypto.randomUUID() };
    try { onCreated(await api.create(payload, attempt.current!.key)); attempt.current = null; }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Research could not be started.'); }
    finally { setSubmitting(false); }
  }
  return <>
    <div className="intro"><span className="eyebrow"><span className="tiny-line" /> YOUR RESEARCH WORKSPACE</span>
      <h1>Good questions.<br /><em>Grounded answers.</em></h1>
      <p>Go beyond the first answer. Explore a topic with a team of research agents,<br className="desktop-break" /> traceable sources, and evidence you can inspect.</p>
    </div>
    <form className="research-form panel" onSubmit={submit} noValidate>
      <div className="section-heading"><span className="icon-tile"><Sparkles size={18} /></span><div><h2>What would you like to understand?</h2><p>A clear question is the start of a useful investigation.</p></div></div>
      <label className="sr-only" htmlFor="research-topic">Research topic</label>
      <textarea id="research-topic" className="topic-input" value={topic} onChange={event => setTopic(event.target.value)}
        placeholder="Ask a question, explore an idea, or investigate a topic…" maxLength={2000} rows={3} disabled={submitting} aria-describedby="topic-help" />
      <div id="topic-help" className="input-hint"><span>Be specific about what you want to learn.</span><span>{topic.length}/2000</span></div>
      <fieldset className="depth-field"><legend>Research depth</legend><div className="depth-options">
        {depths.map(({ id, title, description, icon: Icon }) => <label key={id} className={`depth-option ${depth === id ? 'selected' : ''}`}>
          <input type="radio" name="depth" value={id} checked={depth === id} onChange={() => setDepth(id)} disabled={submitting} />
          <span className="depth-label"><Icon size={17} /><strong>{title}</strong>{depth === id && <Check size={15} className="depth-check" />}</span><span>{description}</span>
        </label>)}
      </div></fieldset>
      <details className="instructions"><summary><SlidersHorizontal size={15} /> Add instructions <span>Optional</span></summary>
        <label className="sr-only" htmlFor="research-instructions">Optional instructions</label>
        <textarea id="research-instructions" rows={2} maxLength={2000} value={instructions} disabled={submitting}
          onChange={event => setInstructions(event.target.value)} placeholder="For example: focus on primary sources and explain the limitations." />
      </details>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="form-bottom"><span><span className="small-dot" /> Sources checked. Uncertainty included.</span>
        <button className="button primary" type="submit" disabled={submitting}>{submitting ? <LoaderCircle size={17} className="spin" /> : <Sparkles size={16} />}{submitting ? 'Starting research…' : 'Start research'}<ArrowUpRight size={17} /></button>
      </div>
    </form>
    <div className="suggestions"><span>NEED A STARTING POINT?</span><div>{suggestions.map(question => <button key={question} type="button" onClick={() => { setTopic(question); document.getElementById('research-topic')?.focus(); }}>
      {question}<ArrowUpRight size={15} /></button>)}</div></div>
    <div className="principles"><div><span>01</span><strong>Explore with purpose</strong><p>A focused plan breaks your question into useful directions.</p></div><div><span>02</span><strong>Follow the evidence</strong><p>Claims connect to passages in retrieved sources.</p></div><div><span>03</span><strong>Know the limits</strong><p>Disagreements and missing evidence stay visible.</p></div></div>
  </>;
}
