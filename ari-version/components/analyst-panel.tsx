'use client';
import { useRef, useState, type ReactNode } from 'react';
import { MessageSquare, Send, Wrench } from 'lucide-react';
import './analyst-panel.css';

type Step = { id: string; tool: string; input: string; done: boolean; error?: boolean };
type Turn = { id: string; role: 'user' | 'assistant'; text: string; steps: Step[]; error?: string };
const QUICK = ['Where did I skip verification today?', 'What did I do well?', 'Which corrections could I have prevented?'];
// Evidence ids: "<sessionId>:<line>" or "metric:<day>:<slug>".
const CITE = /\[([A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)+(?:, *[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)+)*)\]/g;
const brief = (v: unknown) => { const s = v && typeof v === 'object' && Object.keys(v).length ? JSON.stringify(v) : ''; return s.length > 60 ? `${s.slice(0, 60)}…` : s; };

export function renderCitations(text: string, onEvidence: (id: string) => void): ReactNode[] {
  const out: ReactNode[] = []; let last = 0;
  for (const m of text.matchAll(CITE)) {
    out.push(text.slice(last, m.index));
    m[1].split(/, */).forEach(id => out.push(<button key={`${m.index}-${id}`} type="button" className="analyst-cite" onClick={() => onEvidence(id)} title={id}>{id.startsWith('metric:') ? 'metric' : 'source'} {id.split(':').pop()}</button>));
    last = (m.index ?? 0) + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

export function AnalystPanel({ reportId, onEvidence }: { reportId: string; onEvidence: (id: string) => void }) {
  const [turns, setTurns] = useState<Turn[]>([]), [input, setInput] = useState(''), [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const patch = (id: string, f: (t: Turn) => Turn) => setTurns(ts => ts.map(t => (t.id === id ? f(t) : t)));

  async function ask(question: string) {
    const q = question.trim(); if (!q || busy) return;
    const user: Turn = { id: crypto.randomUUID(), role: 'user', text: q, steps: [] }, reply: Turn = { id: crypto.randomUUID(), role: 'assistant', text: '', steps: [] };
    const history = [...turns, user].filter(t => t.text).map(t => ({ id: t.id, role: t.role, parts: [{ type: 'text', text: t.text }] }));
    setTurns(ts => [...ts, user, reply]); setInput(''); setBusy(true);
    abort.current = new AbortController();
    try {
      const r = await fetch('/api/analyst', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ reportId, messages: history }), signal: abort.current.signal });
      if (!r.ok || !r.body) { const e = await r.json().catch(() => ({})); throw new Error(e.error || `Analyst unavailable (${r.status}).`); }
      const reader = r.body.pipeThrough(new TextDecoderStream()).getReader(); let buf = '';
      for (;;) {
        const { done, value } = await reader.read(); if (done) break;
        buf += value; const lines = buf.split('\n'); buf = lines.pop() ?? '';
        for (const line of lines) {
          if (!line.startsWith('data: ') || line === 'data: [DONE]') continue;
          let c: Record<string, unknown>; try { c = JSON.parse(line.slice(6)); } catch { continue; }
          if (c.type === 'text-delta') patch(reply.id, t => ({ ...t, text: t.text + String(c.delta ?? '') }));
          else if (c.type === 'tool-input-available') patch(reply.id, t => ({ ...t, steps: [...t.steps, { id: String(c.toolCallId), tool: String(c.toolName), input: brief(c.input), done: false }] }));
          else if (c.type === 'tool-output-available' || c.type === 'tool-output-error') patch(reply.id, t => ({ ...t, steps: t.steps.map(s => (s.id === c.toolCallId ? { ...s, done: true, error: c.type === 'tool-output-error' } : s)) }));
          else if (c.type === 'error') patch(reply.id, t => ({ ...t, error: String(c.errorText ?? 'The analyst failed.') }));
        }
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') patch(reply.id, t => ({ ...t, error: e instanceof Error ? e.message : 'The analyst failed.' }));
    } finally { setBusy(false); abort.current = null; }
  }

  return <section className="analyst-panel" data-testid="analyst-panel-root" aria-label="Ask the analyst">
    <header className="analyst-head"><MessageSquare size={15} /><strong>Ask the analyst</strong><small>answers cite your sessions</small></header>
    <div className="analyst-log">
      {!turns.length && <div className="analyst-quick">{QUICK.map(q => <button key={q} type="button" onClick={() => ask(q)} disabled={busy}>{q}</button>)}</div>}
      {turns.map(t => t.role === 'user'
        ? <p key={t.id} className="analyst-q">{t.text}</p>
        : <div key={t.id} className="analyst-a" data-testid="analyst-answer">
            {t.steps.map(s => <div key={s.id} className={`analyst-step${s.done ? ' done' : ''}${s.error ? ' failed' : ''}`}><Wrench size={11} />{s.tool} <span>{s.input}</span></div>)}
            {t.text ? <p>{renderCitations(t.text, onEvidence)}</p> : !t.error && <p className="analyst-wait">Thinking…</p>}
            {t.error && <p className="analyst-error">{t.error}</p>}
          </div>)}
    </div>
    <form className="analyst-input" onSubmit={e => { e.preventDefault(); ask(input); }}>
      <input data-testid="analyst-input" value={input} onChange={e => setInput(e.target.value)} placeholder="Ask about this report…" maxLength={2000} disabled={busy} />
      <button type="submit" disabled={busy || !input.trim()} aria-label="Send"><Send size={14} /></button>
    </form>
  </section>;
}
