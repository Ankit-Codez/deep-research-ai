import { useEffect, useState } from 'react';
import { api, clearAuthSession } from './api';

/** Cookie-authenticated SSE; no bearer tokens or provider keys in URLs. */
export function useRunEvents(id: string, active: boolean, refresh: () => void) {
  const [live, setLive] = useState(false);
  useEffect(() => {
    if (!active || typeof EventSource === 'undefined') { setLive(false); return; }
    const source = new EventSource(api.eventsURL(id), { withCredentials: true });
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false); // Browser reconnects; REST polling continues.
    source.addEventListener('progress', () => refresh());
    source.addEventListener('end', () => { source.close(); setLive(false); refresh(); });
    source.addEventListener('deleted', () => { source.close(); setLive(false); refresh(); });
    source.addEventListener('unavailable', () => { setLive(false); });
    source.addEventListener('auth_required', () => {
      source.close(); clearAuthSession(); window.dispatchEvent(new Event('auth-required'));
    });
    return () => { source.close(); };
  }, [id, active, refresh]);
  return live;
}
