import { useCallback, useEffect, useState } from 'react';
import { ArrowUpRight, ChevronRight, Command, History, LayoutDashboard, Menu, Plus, Search, ShieldCheck, X } from 'lucide-react';
import { api, clearAuthSession } from './api';
import type { AuthSession, User } from './types';
import { AuthView } from './components/AuthView';
import { usePolling } from './hooks';
import { ResearchForm } from './components/ResearchForm';
import { RunWorkspace } from './components/RunWorkspace';
import { HistoryView } from './components/HistoryView';
import { AccountView, Allowance } from './components/AccountView';
import { AdminView } from './components/AdminView';

function readRoute() {
  const route = location.hash.slice(1);
  if (route === 'admin') return { screen: 'admin', id: '' };
  if (route === 'account') return { screen: 'account', id: '' };
  if (route === 'history') return { screen: 'history', id: '' };
  if (/^run\/[a-f\d]{8}(-[a-f\d]{4}){3}-[a-f\d]{12}$/i.test(route)) return { screen: 'run', id: route.slice(4) };
  return { screen: 'workspace', id: '' };
}
const loadHealth = (signal: AbortSignal) => api.health(signal);
const loadUsage = (signal: AbortSignal) => api.usage(signal);

export default function App() {
  const [session, setSession] = useState<AuthSession | null>(null);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    void api.me().then(value => { if (active) setSession(value); })
      .catch(cause => { if (active) setError(cause instanceof Error ? cause.message : 'Could not check your session.'); })
      .finally(() => { if (active) setChecking(false); });
    const expired = () => { clearAuthSession(); setSession(null); };
    window.addEventListener('auth-required', expired);
    return () => { active = false; window.removeEventListener('auth-required', expired); };
  }, []);
  if (checking) return <div className="loading-view" role="status">Opening your workspace?</div>;
  if (!session) return <AuthView connectionError={error} onAuthenticated={value => { setError(''); setSession(value); }} />;
  return <Workspace key={session.user.id} user={session.user} onAccountDeleted={() => { setSession(null); setError(''); location.hash = ''; }} onLogout={async () => {
    await api.logout(); setSession(null); setError(''); location.hash = '';
  }} />;
}

export function Workspace({ user, onLogout, onAccountDeleted }: { user: User; onLogout: () => Promise<void>; onAccountDeleted: () => void }) {
  const [route, setRoute] = useState(readRoute);
  const [mobileMenu, setMobileMenu] = useState(false);
  const [logoutError, setLogoutError] = useState('');
  const [loggingOut, setLoggingOut] = useState(false);
  const [composerRevision, setComposerRevision] = useState(0);
  const loadHistory = useCallback((signal: AbortSignal) => api.list(0, signal), []);
  const history = usePolling(loadHistory, 5000);
  const health = usePolling(loadHealth, 15000);
  const usage = usePolling(loadUsage, 10000);
  function refreshData() { history.refresh(); usage.refresh(); }
  useEffect(() => {
    const change = () => { setRoute(readRoute()); setMobileMenu(false); };
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, []);
  function navigate(path: string) {
    location.hash = path;
    setRoute(readRoute());
    setMobileMenu(false);
    window.scrollTo?.({ top: 0, behavior: 'smooth' });
  }
  function newResearch() { setComposerRevision(value => value + 1); navigate(''); }
  const connected = health.data?.status === 'ok' && !health.error;
  return <div className="app-shell">
    <a className="skip-link" href="#main-content" onClick={event => { event.preventDefault(); document.getElementById('main-content')?.focus(); }}>Skip to content</a>
    {mobileMenu && <button className="sidebar-backdrop" aria-label="Close navigation" onClick={() => setMobileMenu(false)} />}
    <aside className={`sidebar ${mobileMenu ? 'open' : ''}`}>
      <button className="brand" onClick={() => navigate('')} aria-label="DeepResearch AI home"><span className="brand-mark"><Search size={25} /><span /></span><span>DeepResearch<span className="brand-ai">AI</span><small>A LITTLE MORE CERTAINTY.</small></span></button>
      <button className="new-research" onClick={newResearch}><Plus size={17} />New research<span>↗</span></button>
      <div className="nav-label">WORKSPACE</div><nav aria-label="Main navigation"><button className={['workspace','run'].includes(route.screen) ? 'active' : ''} onClick={() => navigate('')}><LayoutDashboard size={17} />Overview<ChevronRight size={14} /></button><button className={route.screen === 'history' ? 'active' : ''} onClick={() => navigate('history')}><History size={17} />Research history{history.data && <span className="nav-count">{history.data.total}</span>}</button><button className={route.screen === 'account' ? 'active' : ''} onClick={() => navigate('account')}><ShieldCheck size={17} />Usage &amp; account</button>{user.is_admin && <button className={route.screen === 'admin' ? 'active' : ''} onClick={() => navigate('admin')}><ShieldCheck size={17} />Administration</button>}</nav>
      <div className="sidebar-recent"><div className="nav-label">SAVED RESEARCH <span>{history.data?.items.length || 0}</span></div>{history.loading && <p className="sidebar-empty">Loading your workspace…</p>}
        {!history.loading && !history.data?.items.length && <p className="sidebar-empty">Your investigations will find a home here.</p>}
        {history.data?.items.slice(0, 7).map(run => <button className={`recent-run ${route.id === run.id ? 'selected' : ''}`} key={run.id} aria-label={`Open research: ${run.topic}`} onClick={() => navigate(`run/${run.id}`)}><span className={`run-dot ${run.status}`} /><span>{run.topic}<small>{run.depth} · {new Date(run.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</small></span></button>)}
        {(history.data?.total || 0) > 7 && <button className="view-all" onClick={() => navigate('history')}>View all research<ArrowUpRight size={13} /></button>}
      </div>
      <div className="sidebar-bottom"><div className="grounding-note"><ShieldCheck size={21} /><strong>Evidence over guesswork.</strong><p>Keep the sources.<br />Understand the uncertainty.</p></div><div className="workspace-owner"><span>DR</span><div>{user.name}<small>PRIVATE RESEARCH LIBRARY</small></div><Command size={15} /></div></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div><button className="mobile-toggle" aria-label={mobileMenu ? 'Close menu' : 'Open menu'} onClick={() => setMobileMenu(!mobileMenu)}>{mobileMenu ? <X size={21} /> : <Menu size={21} />}</button><span>Workspace</span><ChevronRight size={13} /><strong>{route.screen === 'admin' ? 'Administration' : route.screen === 'account' ? 'Usage & account' : route.screen === 'history' ? 'Research history' : route.screen === 'run' ? 'Investigation' : 'Overview'}</strong></div><div className="topbar-right"><span className={`connection ${connected ? 'connected' : ''}`}><span className="small-dot" />{health.loading ? 'Connecting…' : connected ? 'Backend connected' : 'Backend offline'}</span><button className="sign-out-button" disabled={loggingOut} onClick={async () => { setLoggingOut(true); setLogoutError(''); try { await onLogout(); } catch { setLogoutError('Sign-out could not be completed. Please try again.'); } finally { setLoggingOut(false); } }}>{loggingOut ? 'Signing out?' : 'Sign out'}</button><span className="avatar" title={user.name}>{user.name.slice(0,1).toUpperCase()}</span></div></header>
      <main id="main-content" tabIndex={-1}>
        {logoutError && <div className="connection-alert" role="alert">{logoutError}</div>}
        {history.error && <div className="connection-alert" role="alert"><strong>Connection needs attention.</strong><span>{history.error}</span><button onClick={history.refresh}>Try again</button></div>}
        {route.screen === 'workspace' && <>{usage.data && <Allowance usage={usage.data} />}<ResearchForm key={composerRevision} onCreated={run => { refreshData(); navigate(`run/${run.id}`); }} /></>}
        {route.screen === 'run' && <RunWorkspace key={route.id} id={route.id} onChanged={refreshData} onBack={() => navigate('')} />}
        {route.screen === 'history' && <HistoryView onChanged={refreshData} onOpen={id => navigate(`run/${id}`)} />}
        {route.screen === 'account' && <AccountView user={user} usage={usage.data} error={usage.error} refresh={usage.refresh} onDeleted={onAccountDeleted} />}
        {route.screen === 'admin' && (user.is_admin ? <AdminView /> : <p className="inline-error" role="alert">Administrator access is required.</p>)}
        <footer className="page-footer"><span>DeepResearch AI</span><span>Curiosity, with a paper trail.</span></footer>
      </main>
    </div>
  </div>;
}
