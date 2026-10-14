'use client';
import { useEffect, useRef, useState } from 'react';
import { BadgeCheck, ChevronRight, Clock3, Coins, FileText, Layers, MessageSquare, PencilLine, ShieldCheck } from 'lucide-react';
import type { ReportView } from '@/lib/report-view';
import type { ScorerTile } from '@/lib/types';
import { layoutDayTimeline } from '@/lib/day-timeline';

// Scorer harness blocks (docs/spec/bridge-contract.md). Same component for local and hosted: both read report.scorerMetrics.
const ORDER = ['V', 'C', 'episodes', 'H', 'B_min', 'context', 'cost'];
const TILE: Record<string, { tone: string; icon: typeof Layers }> = {
  V: { tone: 'teal', icon: ShieldCheck }, C: { tone: 'amber', icon: MessageSquare }, episodes: { tone: 'blue', icon: BadgeCheck },
  H: { tone: 'violet', icon: PencilLine }, B_min: { tone: 'amber', icon: Clock3 }, context: { tone: 'cyan', icon: Layers }, cost: { tone: 'slate', icon: Coins },
};
export function sortTiles(tiles: ScorerTile[]) { return [...tiles].sort((a, b) => (ORDER.indexOf(a.key) + 1 || 99) - (ORDER.indexOf(b.key) + 1 || 99)); }
function baseline(t: ScorerTile) {
  if (t.baseline === undefined || t.baseline === null) return null;
  const v = t.unit === 'share' ? `${Math.round(t.baseline * 100)}%` : t.unit === 'usd' ? `$${t.baseline.toFixed(2)}` : `${Math.round(t.baseline * 10) / 10}${t.unit === 'min' ? ' min' : ''}`;
  return `7-day baseline ${v}`;
}

export function ScoresBlock({ report, onEvidence }: { report: ReportView; onEvidence: (id: string) => void }) {
  const s = report.scorerMetrics; if (!s) return null;
  return <section className="scores-panel" data-testid="scorer-kpis">
    <div className="panel-heading"><div><span className="section-kicker">MEASURED FROM SESSIONS{s.demo ? ' · DEMO DATA' : ''}</span><h2>Scores</h2></div><span className="quiet-pill">{s.day} · {s.tz}</span></div>
    {s.verdict ? <p className="scores-verdict">{s.verdict}</p> : null}
    <div className="scores-grid">{sortTiles(s.tiles).map(t => {
      const look = TILE[t.key] ?? { tone: 'slate', icon: Layers }; const Icon = look.icon; const base = baseline(t);
      return <div key={t.key} className={`visual-metric scores-tile ${look.tone}`} data-testid={`scorer-tile-${t.key}`}>
        <span className="metric-top"><Icon size={17} /><span>{t.label}</span></span>
        <strong>{t.display}</strong>
        {t.sub ? <small>{t.sub}</small> : null}
        {base ? <small className="scores-baseline">{base}</small> : null}
        {t.evidence.length ? <div className="evidence-links">{t.evidence.slice(0, 4).map((e, i, all) => <button key={e.id} title={e.excerpt} onClick={() => onEvidence(e.id)}><FileText size={12} />{e.id.startsWith('metric:') ? 'How measured' : `Turn ${all.slice(0, i + 1).filter(x => !x.id.startsWith('metric:')).length}`}<ChevronRight size={12} /></button>)}</div> : null}
      </div>;
    })}</div>
  </section>;
}

export function DayTimeline({ report, onEvidence }: { report: ReportView; onEvidence: (id: string) => void }) {
  const host = useRef<HTMLDivElement>(null); const [width, setWidth] = useState(900);
  useEffect(() => { const el = host.current; if (!el) return; const ro = new ResizeObserver(() => setWidth(el.clientWidth || 900)); ro.observe(el); setWidth(el.clientWidth || 900); return () => ro.disconnect(); }, []);
  const zone = report.scorerMetrics?.tz || report.timezone || 'America/Los_Angeles';
  const hhmm = (ms: number) => new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: zone }).format(new Date(ms));
  const L = layoutDayTimeline(report.activity ?? [], report.scorerMetrics?.lanes, report.scorerMetrics?.interrupts, width);
  const trunc = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
  return <section className="session-timeline day-timeline" data-testid="day-timeline">
    <div className="panel-heading"><div><span className="section-kicker">WORKTREES · {zone}</span><h2>Day overview</h2></div></div>
    <div ref={host} className="day-timeline-host">{L ? <svg viewBox={`0 0 ${L.width} ${L.height}`} width="100%" height={L.height} role="img" aria-label="Agent activity by worktree">
      <defs><clipPath id="day-tl-clip"><rect x={L.labelW} y={0} width={L.width - L.labelW} height={L.height} /></clipPath></defs>
      {L.overlap.map((o, i) => <rect key={i} className="dt-overlap" clipPath="url(#day-tl-clip)" x={o.x} y={0} width={o.w} height={L.lanesH} />)}
      {L.rows.map((r, i) => r.type === 'repo'
        ? <g key={i}><text className="dt-repo" x={0} y={r.y + r.h - 9}>{trunc(r.label, 28)}<title>{r.label}</title></text><line className="dt-rule" x1={L.labelW} x2={L.width} y1={r.y + r.h - .5} y2={r.y + r.h - .5} /></g>
        : <g key={i}>
          <rect x={14} y={r.y + r.h / 2 - 4} width={8} height={8} rx={2} className={`dt-sw ${r.agents[0]}`} />
          <text className="dt-lane" x={30} y={r.y + r.h / 2 + 4}>{trunc(r.label, L.labelW < 200 ? 9 : 18)}<tspan className="dt-agent" dx={8}>{r.agents.map(a => a === 'codex' ? 'Codex' : 'Claude').join(' · ')}</tspan><title>{r.key}{r.sessions.length > 1 ? ` · ${r.sessions.length} sessions` : ''}</title></text>
          {r.bars.map(b => <rect key={b.id} className={`dt-bar ${b.agent}${b.aborted ? ' aborted' : ''}`} clipPath="url(#day-tl-clip)" x={b.x} y={r.y + r.h / 2 - 3} width={b.w} height={6} rx={3}><title>{`${hhmm(b.start)}–${hhmm(b.end)}${b.aborted ? ' · interrupted' : ''}`}</title></rect>)}
          {r.ticks.map((t, j) => <g key={j} className={`dt-tick${t.evidenceId ? ' linked' : ''}`} onClick={t.evidenceId ? () => onEvidence(t.evidenceId!) : undefined} role={t.evidenceId ? 'button' : undefined} tabIndex={t.evidenceId ? 0 : undefined} onKeyDown={t.evidenceId ? e => { if (e.key === 'Enter') onEvidence(t.evidenceId!); } : undefined}>
            <rect x={t.x - 5} y={r.y + 4} width={10} height={r.h - 8} fill="transparent" /><line clipPath="url(#day-tl-clip)" x1={t.x} x2={t.x} y1={r.y + r.h / 2 - 7} y2={r.y + r.h / 2 + 7} /><title>{`Interrupted ${hhmm(t.at)}`}</title></g>)}
        </g>)}
      <line className="dt-rule" x1={L.labelW} x2={L.width} y1={L.lanesH + .5} y2={L.lanesH + .5} />
      {L.axis.map(a => <text key={a.t} className="dt-axis" x={a.x} y={L.lanesH + 19} textAnchor={a.x > L.width - 24 ? 'end' : a.x < L.labelW + 24 ? 'start' : 'middle'}>{hhmm(a.t)}</text>)}
    </svg> : <div className="timeline-unavailable"><Clock3 size={25} /><h3>No recorded turns on this day.</h3></div>}</div>
    <div className="timeline-legend"><span><i className="dt-claude" />Claude Code</span><span><i className="dt-codex" />Codex</span><span><i className="dt-ovl" />Two at once</span><span><i className="dt-int" />Interruption</span><span>{zone} · recorded turns</span></div>
  </section>;
}
