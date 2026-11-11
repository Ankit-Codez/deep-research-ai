import { useState } from 'react';
import { Trash2 } from 'lucide-react';
import { api } from '../api';

/** Inline confirmation keeps destructive actions explicit without browser dialogs. */
export function DeleteResearch({ id, topic, onDeleted }: { id: string; topic: string; onDeleted: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function remove() {
    setBusy(true); setError('');
    try { await api.deleteRun(id); onDeleted(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Research could not be deleted.'); }
    finally { setBusy(false); }
  }
  if (!confirming) return <button className="text-button danger-text" aria-label={`Delete research: ${topic}`} onClick={() => setConfirming(true)}><Trash2 size={14} />Delete research</button>;
  return <section className="delete-confirmation" aria-label="Delete research confirmation"><strong>Delete this investigation?</strong><p>The report, evidence and private source snapshots will be removed. Active work will stop at its next checkpoint. Numeric usage still counts toward your allowance.</p>
    {error && <p role="alert" className="inline-error">{error}</p>}<div className="action-row"><button className="button secondary" disabled={busy} onClick={() => setConfirming(false)}>Keep research</button><button className="button danger" disabled={busy} onClick={remove}>{busy ? 'Deleting...' : 'Permanently delete research'}</button></div></section>;
}
