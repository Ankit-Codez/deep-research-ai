import { useCallback, useEffect, useState } from 'react';

/** Sequential polling avoids overlapping requests; cleanup rejects stale updates. */
export function usePolling<T>(load: (signal: AbortSignal) => Promise<T>, interval: number,
                              shouldContinue: (value: T) => boolean = always) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function update() {
      let again = true;
      try {
        const value = await load(controller.signal);
        if (!active) return;
        setData(value);
        setError('');
        again = shouldContinue(value);
      } catch (cause) {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : 'Could not load research data.');
      } finally {
        if (active) {
          setLoading(false);
          if (interval && again) timer = setTimeout(update, interval);
        }
      }
    }
    void update();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [load, interval, revision, shouldContinue]);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  return { data, error, loading, refresh };
}
function always() { return true; }
