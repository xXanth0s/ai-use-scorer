'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ArrowRight, Check, Cloud, Copy, Download, Layers, LoaderCircle, ShieldCheck } from 'lucide-react';
import { AppNavigation } from './app-navigation';

type Agent = 'codex' | 'claude';
type SetupState = {
  connection: { connected: boolean; userId?: string; dailyLimit?: number; usedToday?: number; collectorLimit?: number };
  collector: { syncedAt: string } | null;
  sessionCount: number;
  reportCount: number;
};
const steps = ['Sign up', 'Choose agent', 'Copy setup message', 'Approve installation', 'Connected'];
async function api<T>(url: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(url, { method, headers: { 'content-type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed.');
  return data;
}
async function provision() {
  await api('/api/setup', 'POST');
  return api<SetupState>('/api/setup');
}

export function HostedSetup() {
  const [state, setState] = useState<SetupState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null);
  const [copied, setCopied] = useState(false);
  const [openedAgent, setOpenedAgent] = useState(false);
  const initialRequest = useRef<Promise<SetupState> | null>(null);

  useEffect(() => {
    let alive = true;
    // Older invitation links remain safe to open; signup no longer needs their code.
    if (window.location.hash) window.history.replaceState(null, '', window.location.pathname);
    initialRequest.current ??= provision();
    initialRequest.current.then(value => { if (alive) setState(value); }).catch(cause => { if (alive) setError(cause.message); });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    if (!state?.connection.connected) return;
    let alive = true;
    const timer = setInterval(() => { api<SetupState>('/api/setup').then(value => { if (alive) setState(value); }).catch(() => {}); }, 15000);
    return () => { alive = false; clearInterval(timer); };
  }, [state?.connection.connected]);

  async function retry() {
    setBusy(true); setError('');
    try { setState(await provision()); } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  async function prepare(agent: Agent) {
    setBusy(true); setError(''); setCopied(false); setOpenedAgent(false);
    try {
      const data = await api<{ message: string }>('/api/plugin-setup', 'POST', { agent });
      setMessage(data.message); setSelectedAgent(agent);
      try { await navigator.clipboard.writeText(data.message); setCopied(true); } catch { /* The explicit copy button and text remain available. */ }
    } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  async function copy() {
    try { await navigator.clipboard.writeText(message); setCopied(true); setError(''); }
    catch { setError('Copy the text under “View setup message”, then paste it into your agent.'); }
  }
  function resetMessage() { setMessage(''); setSelectedAgent(null); setCopied(false); setOpenedAgent(false); setError(''); }

  const connected = state?.connection.connected;
  const seen = !!state?.collector;
  const currentStep = seen ? 4 : openedAgent ? 3 : message ? 2 : 1;
  const agentName = selectedAgent === 'codex' ? 'Codex' : 'Claude Code';
  const heading = !state ? 'Creating your private workspace…' : seen ? 'Your plugin is connected.' : openedAgent ? 'Approve setup in your agent.' : message ? copied ? 'Your setup message is copied.' : 'Copy your setup message.' : 'Choose your agent.';
  const description = seen
    ? `${state!.sessionCount} sessions synced. Last contact ${new Date(state!.collector!.syncedAt).toLocaleString()}.`
    : openedAgent ? `Let ${agentName} install the plugin. Review and approve its native installation and hook permissions when prompted.`
    : message ? `Paste the private setup message into a new ${agentName} chat. Your agent handles installation and secure account pairing.`
    : 'Your account is ready. Choose the agent you use on this computer to get a personalized setup message.';

  return <><AppNavigation active="settings" hosted/><main className="setup-page hosted-onboarding">
    <header className="setup-heading"><span className="section-kicker">HOW DO YOU AI · SETUP</span><h1>{seen ? 'Your sessions are connected.' : 'Connect your AI workspace.'}</h1><p>One account. Your agents. Daily reports that help you understand how you work with AI.</p></header>
    <ol className="onboarding-steps" aria-label="Connection progress">{steps.map((step, index) => <li key={step} className={index < currentStep ? 'is-complete' : index === currentStep ? 'is-current' : ''} aria-current={index === currentStep ? 'step' : undefined}><span className="onboarding-step-index">{index < currentStep ? <Check size={14}/> : index + 1}</span><span>{step}</span></li>)}</ol>
    <div className="setup-layout"><section className="setup-main" aria-label="Hosted setup" aria-busy={busy || (!state && !error)}>
      <div className={`setup-symbol ${seen ? 'is-ready' : ''}`}>{seen ? <Check size={28}/> : !state && !error ? <LoaderCircle className="spin" size={28}/> : openedAgent ? <ShieldCheck size={28}/> : message ? <Copy size={28}/> : <Layers size={28}/>}</div>
      <h2>{heading}</h2><p className="setup-description">{!state ? 'We’re setting up your account with limited daily usage. No invitation or API key needed.' : description}</p>
      {error ? <p className="setup-error" role="alert">{error}</p> : null}
      {(!state || !connected) && error ? <button className="button setup-primary" disabled={busy} onClick={retry}>{busy ? <LoaderCircle className="spin" size={17}/> : null}Try again</button> : null}
      {seen ? <><div className="onboarding-connected" role="status"><span className="connection-dot"/>Connection verified by plugin check-in</div><Link className="button setup-primary" href="/daily">Open daily analysis<ArrowRight size={17}/></Link></> : connected && !message ? <div className="plugin-agent-actions"><button className="button setup-primary" disabled={busy} onClick={() => prepare('codex')}>{busy ? <LoaderCircle className="spin" size={17}/> : <Layers size={17}/>}Connect Codex<ArrowRight size={17}/></button><button className="button secondary setup-primary" disabled={busy} onClick={() => prepare('claude')}>Connect Claude Code<ArrowRight size={17}/></button></div> : null}
      {message && !seen ? <div className="plugin-message onboarding-message">
        {openedAgent ? <><p className="onboarding-waiting" role="status"><LoaderCircle className="spin" size={16}/>Waiting for your plugin to check in</p><p>The dashboard checks every 15 seconds. Approvals happen inside your agent; this page confirms the connection once it receives a sync.</p></> : <><p role="status">{copied ? 'Copied and ready to paste.' : 'Your setup message is ready.'} Open {agentName} on your computer and start a new chat.</p><button className="button setup-primary" onClick={() => setOpenedAgent(true)}>I’ve opened my agent<ArrowRight size={17}/></button></>}
        <div className="onboarding-message-actions"><button className="button secondary" onClick={copy}><Copy size={15}/>{copied ? 'Copy again' : 'Copy setup message'}</button><button className="text-button" onClick={resetMessage}>Choose another agent</button></div>
        <details><summary>View setup message</summary><textarea aria-label="Plugin setup message" readOnly rows={9} value={message}/></details>
        <small>Private pairing · expires in 15 minutes · macOS/Linux · Node.js 24+</small>
      </div> : null}
      <p className="setup-consent">By connecting, you allow the plugin to read local Codex and Claude Code transcripts and run approved capture hooks. It sends summaries, metrics, and bounded evidence excerpts for daily reports. Original transcript files stay on your computer.</p>
      {seen ? <details className="setup-legacy"><summary>Connect another agent</summary><div className="plugin-agent-actions"><button className="button secondary" disabled={busy} onClick={() => prepare('codex')}>Connect Codex</button><button className="button secondary" disabled={busy} onClick={() => prepare('claude')}>Connect Claude Code</button></div>{message ? <div className="plugin-message"><p>Paste this setup message into {agentName}, then approve its native installation. Your existing plugin is already connected.</p><button className="button secondary" onClick={copy}><Copy size={15}/>{copied ? 'Copy again' : 'Copy setup message'}</button><details><summary>View setup message</summary><textarea aria-label="Additional plugin setup message" readOnly rows={9} value={message}/></details><button className="text-button" onClick={resetMessage}>Reset setup message</button></div> : null}<p>Browser sign-in and plugin credentials are separate. To stop capture and sync, ask your agent to disconnect the how do you ai plugin.</p></details> : null}
    </section><aside className="setup-context"><span className="section-kicker">YOUR ACCOUNT</span>
      <div className="onboarding-limits"><ShieldCheck size={21}/><h3>Start with a restricted account</h3><p><strong>{state?.connection.dailyLimit ?? 2} reports per day</strong>{typeof state?.connection.usedToday === 'number' ? ` · ${state.connection.usedToday} used today` : ''}<br/>{state?.connection.collectorLimit ?? 3} plugin connections<br/>1 analysis at a time</p></div>
      <div><Download size={21}/><h3>Your agent handles installation</h3><p>Paste one setup message. Your agent installs and pairs the plugin without asking you for API keys or terminal commands.</p></div>
      <div><Cloud size={21}/><h3>Your dashboard stays in sync</h3><p>Session summaries sync while your agent is active. Daily analysis starts on the next activity after a day finishes.</p></div>
      <p className="setup-boundary"><ShieldCheck size={16}/>You approve installation in your agent. Cloud sessions and computer use are included only when supported transcripts are available locally.</p>
    </aside></div>
  </main></>;
}
