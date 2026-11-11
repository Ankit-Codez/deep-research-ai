import { useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowUpRight, Check, Eye, EyeOff, LoaderCircle, LockKeyhole, Search, ShieldCheck } from 'lucide-react';
import { api } from '../api';
import type { AuthSession } from '../types';

export function AuthView({ onAuthenticated, connectionError }: { onAuthenticated: (session: AuthSession) => void; connectionError: string }) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault(); if (busy) return;
    if (!email.trim() || !password || (mode === 'register' && !name.trim())) { setError('Complete all fields to continue.'); return; }
    if (mode === 'register' && password.length < 15) { setError('Choose a passphrase with at least 15 characters.'); return; }
    setBusy(true); setError('');
    try {
      const session = await api.authenticate(mode, { email: email.trim(), password, ...(mode === 'register' ? { name: name.trim() } : {}) });
      setPassword(''); onAuthenticated(session);
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Sign-in could not be completed.'); }
    finally { setBusy(false); }
  }
  return <div className="auth-page"><aside className="auth-story"><div className="auth-brand"><Search size={26} />DeepResearch<span>AI</span></div><div className="auth-story-content"><span className="eyebrow">A LITTLE MORE CERTAINTY.</span><h1>Your curiosity.<br /><em>Your research.</em><br />Your workspace.</h1><p>Follow the evidence, keep the sources, and return to the questions that matter.</p><div className="auth-principle"><ShieldCheck size={19} /><span>Your research library belongs to you.</span></div></div><span className="auth-story-footer">CURIOSITY, WITH A PAPER TRAIL.</span></aside>
    <main className="auth-main"><form className="auth-form" onSubmit={submit} noValidate><span className="icon-tile"><LockKeyhole size={21} /></span><span className="eyebrow">YOUR RESEARCH STARTS HERE</span><h2>{mode === 'login' ? 'Welcome back.' : 'A workspace of your own.'}</h2><p>{mode === 'login' ? 'Sign in to continue your investigations.' : 'Create an account to save your questions, sources, and reports.'}</p>
      {mode === 'register' && <label>Your name<input autoComplete="name" value={name} onChange={event => setName(event.target.value)} maxLength={80} disabled={busy} /></label>}
      <label>Email address<input type="email" autoComplete="email" value={email} onChange={event => setEmail(event.target.value)} maxLength={254} disabled={busy} /></label>
      <label>Password<span className="password-field"><input type={visible ? 'text' : 'password'} autoComplete={mode === 'register' ? 'new-password' : 'current-password'} value={password} onChange={event => setPassword(event.target.value)} maxLength={128} disabled={busy} /><button type="button" aria-label={visible ? 'Hide password' : 'Show password'} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={17} /> : <Eye size={17} />}</button></span></label>
      {mode === 'register' && <span className="password-hint"><Check size={12} />Use a memorable passphrase of 15–128 characters.</span>}
      {(error || connectionError) && <p className="inline-error" role="alert">{error || connectionError}</p>}
      <button className="button primary auth-submit" type="submit" disabled={busy}>{busy ? <LoaderCircle size={17} className="spin" /> : null}{busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'}<ArrowUpRight size={17} /></button>
      <div className="auth-switch">{mode === 'login' ? 'New to DeepResearch?' : 'Already have an account?'}<button type="button" disabled={busy} onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(''); setPassword(''); }}>{mode === 'login' ? 'Create an account' : 'Sign in'}</button></div>
      <div className="auth-security"><ShieldCheck size={15} /><span>Secure sessions. A private research history.</span></div>
    </form></main></div>;
}
