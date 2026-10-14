'use client';
import { useState } from 'react';
import { Activity, BadgeCheck, Bot, ChevronRight, Clock3, Coins, Cpu, FileText, GitBranch, Info, Layers, ListTree, MessageSquare, Pause, PencilLine, Plug, Puzzle, ShieldCheck, Sparkles, Terminal, Wrench } from 'lucide-react';
import type { ReportView } from '@/lib/report-view';
import type { ScorerTile } from '@/lib/types';

// Scorer harness blocks (docs/spec/bridge-contract.md). Same components for local and hosted: both read report.scorerMetrics.
const SCORE_ORDER = ['V', 'C', 'episodes', 'H', 'B_min'];
const LEVERAGE_ORDER = ['tooling_skill', 'tooling_command', 'tooling_mcp', 'tooling_plugin', 'top_tools', 'subagents', 'model_mix', 'parallel', 'interrupts', 'gaps', 'context', 'tokens', 'cost'];
export const TOOLING_KEYS = ['tooling_skill', 'tooling_command', 'tooling_mcp', 'tooling_plugin', 'top_tools'];
const TILE: Record<string, { tone: string; icon: typeof Layers }> = {
  V: { tone: 'teal', icon: ShieldCheck }, C: { tone: 'amber', icon: MessageSquare }, episodes: { tone: 'blue', icon: BadgeCheck },
  H: { tone: 'violet', icon: PencilLine }, B_min: { tone: 'amber', icon: Clock3 },
  tooling_skill: { tone: 'blue', icon: Sparkles }, tooling_command: { tone: 'blue', icon: Terminal }, tooling_mcp: { tone: 'cyan', icon: Plug },
  tooling_plugin: { tone: 'cyan', icon: Puzzle }, top_tools: { tone: 'cyan', icon: Wrench }, subagents: { tone: 'violet', icon: Bot },
  model_mix: { tone: 'violet', icon: Cpu }, parallel: { tone: 'violet', icon: GitBranch }, interrupts: { tone: 'amber', icon: Pause },
  gaps: { tone: 'amber', icon: Activity }, context: { tone: 'cyan', icon: Layers }, tokens: { tone: 'slate', icon: ListTree }, cost: { tone: 'slate', icon: Coins },
};
export function sortTiles(tiles: ScorerTile[], order = SCORE_ORDER) { return [...tiles].sort((a, b) => (order.indexOf(a.key) + 1 || 99) - (order.indexOf(b.key) + 1 || 99)); }
function baseline(t: ScorerTile) {
  if (t.baseline === undefined || t.baseline === null) return null;
  const v = t.unit === 'share' ? `${Math.round(t.baseline * 100)}%` : t.unit === 'usd' ? `$${t.baseline.toFixed(2)}` : `${Math.round(t.baseline * 10) / 10}${t.unit === 'min' ? ' min' : ''}`;
  return `7-day baseline ${v}`;
}

// Tile grid with Ari's KPI info pattern: ⓘ in the tile header toggles a metric-definition panel under the grid.
function TileGrid({ tiles, onEvidence }: { tiles: ScorerTile[]; onEvidence: (id: string) => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const current = tiles.find(t => t.key === open && t.howMeasured); const currentTone = current ? (TILE[current.key]?.tone ?? 'slate') : '';
  return <>
    <div className="scores-grid">{tiles.map(t => {
      const look = TILE[t.key] ?? { tone: 'slate', icon: Layers }; const Icon = look.icon; const base = baseline(t);
      return <div key={t.key} className={`visual-metric scores-tile ${look.tone} ${open === t.key ? 'is-open' : ''}`} data-testid={`scorer-tile-${t.key}`}>
        <span className="metric-top"><Icon size={17} /><span>{t.label}</span>{t.howMeasured ? <button className="scores-info" onClick={() => setOpen(open === t.key ? null : t.key)} aria-expanded={open === t.key} aria-label={`How ${t.label} is measured`}><Info size={12} /></button> : null}</span>
        <strong>{t.display}</strong>
        {t.sub ? <small>{t.sub}</small> : null}
        {base ? <small className="scores-baseline">{base}</small> : null}
        {t.evidence.length ? <div className="evidence-links">{t.evidence.slice(0, 4).map((e, i, all) => <button key={e.id} title={e.excerpt} onClick={() => onEvidence(e.id)}><FileText size={12} />{e.id.startsWith('metric:') ? 'Metric source' : `Turn ${all.slice(0, i + 1).filter(x => !x.id.startsWith('metric:')).length}`}<ChevronRight size={12} /></button>)}</div> : null}
      </div>;
    })}</div>
    {current ? <div className={`metric-definition ${currentTone}`}><Info size={16} /><div><strong>{current.label}: how measured</strong><p>{current.howMeasured}</p></div><button onClick={() => setOpen(null)} aria-label="Close metric definition">×</button></div> : null}
  </>;
}

export function ScoresBlock({ report, onEvidence }: { report: ReportView; onEvidence: (id: string) => void }) {
  const s = report.scorerMetrics; if (!s) return null;
  const zone = report.timezone || s.tz;
  const tiles = sortTiles(s.tiles.filter(t => SCORE_ORDER.includes(t.key)));
  return <section className="scores-panel" data-testid="scorer-kpis">
    <div className="panel-heading"><div><span className="section-kicker">MEASURED FROM SESSIONS{s.demo ? ' · DEMO DATA' : ''}</span><h2>Scores</h2></div><span className="quiet-pill">{s.day} · {zone}</span></div>
    {s.verdict ? <p className="scores-verdict">{s.verdict}</p> : null}
    <TileGrid tiles={tiles} onEvidence={onEvidence} />
  </section>;
}

export function LeverageBlock({ report, onEvidence, keys, testId = 'scorer-leverage', title = 'Leverage & diagnostics' }: { report: ReportView; onEvidence: (id: string) => void; keys?: string[]; testId?: string; title?: string }) {
  const all = report.scorerMetrics?.leverage; if (!all?.length) return null;
  const tiles = sortTiles(keys ? all.filter(t => keys.includes(t.key)) : all, LEVERAGE_ORDER); if (!tiles.length) return null;
  return <section className="scores-panel" data-testid={testId}>
    <div className="panel-heading"><div><span className="section-kicker">HOW THE WORK RAN · NOT SCORED</span><h2>{title}</h2></div></div>
    <TileGrid tiles={tiles} onEvidence={onEvidence} />
  </section>;
}
