import type { Agent, ScorerInterrupt, ScorerLane, TaskSpan } from './types';
// Worktree timeline layout, ported from the scorer dashboard (src/scorer/static/app.js drawTimeline):
// a repo header row, then one lane per worktree (cwd, else repo+branch); bars per turn, overlap shading
// where two or more sessions run at once, interruption ticks.
export type ActivityInput = { id: string; agent: Agent; title: string; tasks: TaskSpan[] };
export type TimelineBar = { id: string; sessionId: string; agent: Agent; start: number; end: number; aborted: boolean; x: number; w: number };
export type TimelineTick = { sessionId: string; at: number; x: number; evidenceId: string | null };
export type TimelineRow =
  | { type: 'repo'; y: number; h: number; label: string }
  | { type: 'lane'; y: number; h: number; key: string; label: string; branch: string | null; agents: Agent[]; sessions: string[]; bars: TimelineBar[]; ticks: TimelineTick[] };
export type TimelineLayout = { width: number; height: number; lanesH: number; labelW: number; t0: number; t1: number; rows: TimelineRow[]; overlap: { x: number; w: number }[]; axis: { x: number; t: number }[] };

const STEPS = [5, 10, 15, 30, 60, 120, 180, 240].map(m => m * 60e3);
const HOUR = 3600e3;
const tail = (p: string, n: number) => p.replace(/\/+$/, '').split('/').slice(-n).join('/');

export function worktreeOf(lane: ScorerLane | undefined, fallbackTitle: string) {
  if (lane?.cwd) return { key: lane.cwd, label: tail(lane.cwd, 2), repo: lane.repo || tail(lane.cwd, 1), branch: lane.branch };
  if (lane?.repo) return { key: `${lane.repo}\u0000${lane.branch ?? ''}`, label: lane.repo, repo: lane.repo, branch: lane.branch };
  return { key: `?\u0000${fallbackTitle}`, label: fallbackTitle, repo: 'unknown repo', branch: null };
}

/** Intervals where ≥2 distinct sessions have a running task. */
export function overlapIntervals(spans: { sessionId: string; start: number; end: number }[]) {
  const pts = spans.flatMap(s => [{ t: s.start, d: 1, s: s.sessionId }, { t: s.end, d: -1, s: s.sessionId }]).sort((a, b) => a.t - b.t || a.d - b.d);
  const active = new Map<string, number>(); const out: { start: number; end: number }[] = []; let from: number | null = null;
  for (const p of pts) {
    active.set(p.s, (active.get(p.s) || 0) + p.d); if (!active.get(p.s)) active.delete(p.s);
    if (active.size >= 2 && from === null) from = p.t;
    else if (active.size < 2 && from !== null) { if (p.t > from) out.push({ start: from, end: p.t }); from = null; }
  }
  return out;
}

export function layoutDayTimeline(activity: ActivityInput[], lanes: ScorerLane[] = [], interrupts: ScorerInterrupt[] = [], width = 900): TimelineLayout | null {
  const W = Math.max(width, 480), LBL = W < 700 ? 120 : 220, GH = 30, LANE = 32, AX = 26;
  const laneBy = new Map(lanes.map(l => [l.sessionId, l]));
  const sessions = activity.map(s => ({ ...s, spans: s.tasks.filter(t => t.end && Number.isFinite(Date.parse(t.start)) && Number.isFinite(Date.parse(t.end))).map(t => ({ ...t, startMs: Date.parse(t.start), endMs: Math.max(Date.parse(t.end!), Date.parse(t.start)) })) }))
    .filter(s => s.spans.length).map(s => ({ ...s, first: Math.min(...s.spans.map(t => t.startMs)) })).sort((a, b) => a.first - b.first);
  if (!sessions.length) return null;
  let t0 = sessions[0].first; let t1 = Math.max(...sessions.flatMap(s => s.spans.map(t => t.endMs)));
  const maxTicks = Math.max(3, Math.floor((W - 208) / 64));
  const pad = (st: number): [number, number] => { const u = Math.max(st, HOUR); return [Math.floor((t0 - st * 0.25) / u) * u, Math.ceil((t1 + st * 0.25) / u) * u]; };
  const step = STEPS.find(st => { const [a, b] = pad(st); return (b - a) / st <= maxTicks; }) ?? STEPS[STEPS.length - 1];
  [t0, t1] = pad(step);
  const x = (ms: number) => LBL + ((ms - t0) / (t1 - t0)) * (W - LBL);
  // group sessions → worktree lanes → repos (insertion order = first activity)
  const groups = new Map<string, ReturnType<typeof worktreeOf> & { members: typeof sessions }>();
  for (const s of sessions) { const w = worktreeOf(laneBy.get(s.id), s.title); if (!groups.has(w.key)) groups.set(w.key, { ...w, members: [] }); groups.get(w.key)!.members.push(s); }
  const byRepo = new Map<string, (typeof groups extends Map<string, infer G> ? G : never)[]>();
  for (const g of groups.values()) { if (!byRepo.has(g.repo)) byRepo.set(g.repo, []); byRepo.get(g.repo)!.push(g); }
  const rows: TimelineRow[] = []; let y = 0;
  for (const [repo, gs] of byRepo) {
    rows.push({ type: 'repo', y, h: GH, label: repo }); y += GH;
    for (const g of gs) {
      const ids = new Set(g.members.map(m => m.id));
      const bars = g.members.flatMap(m => m.spans.map(t => ({ id: t.id, sessionId: m.id, agent: m.agent, start: t.startMs, end: t.endMs, aborted: t.aborted, x: x(t.startMs), w: Math.max(x(t.endMs) - x(t.startMs), 3) })));
      const ticks = interrupts.filter(i => ids.has(i.sessionId) && Number.isFinite(Date.parse(i.at))).map(i => ({ sessionId: i.sessionId, at: Date.parse(i.at), x: x(Date.parse(i.at)), evidenceId: i.evidenceId }));
      rows.push({ type: 'lane', y, h: LANE, key: g.key, label: g.branch || tail(g.label, 1), branch: g.branch, agents: [...new Set(g.members.map(m => m.agent))], sessions: [...ids], bars, ticks }); y += LANE;
    }
  }
  const overlap = overlapIntervals(sessions.flatMap(s => s.spans.map(t => ({ sessionId: s.id, start: t.startMs, end: t.endMs })))).map(o => ({ x: x(o.start), w: Math.max(x(o.end) - x(o.start), 1) }));
  const axis: { x: number; t: number }[] = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) axis.push({ x: x(t), t });
  return { width: W, height: y + AX, lanesH: y, labelW: LBL, t0, t1, rows, overlap, axis };
}
