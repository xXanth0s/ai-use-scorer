import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Server } from 'node:http';

import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import type { GuardTrip } from '../src/harness/read-loop-guard';
import { createScorerMastra, type ScorerMastra } from '../src/mastra';
import { makeServer } from '../src/server';
import { DAY, freePort, lastUserText, sse, startScorer, toolResultsText, type ScorerProc } from './helpers';
import { scriptedModel, type ScriptCall, type ScriptPart } from './mock-model';

/**
 * Real chain, mock LLMs: `scorer serve` (fixture DB, Python tools) <-> this sidecar (Mastra agents
 * on scripted V3 models). Scenarios are selected by a keyword in the user's question.
 */
let scorer: ScorerProc;
let sidecar: Server;
let sidecarUrl: string;
let proxied: ScorerProc; // a second scorer serve with SCORER_MASTRA_URL -> the sidecar
let app: ScorerMastra;
const trips: GuardTrip[] = [];
let ids: Record<string, number>;

const analystModel = scriptedModel('mock-analyst', (c: ScriptCall): ScriptPart[] => {
  const q = lastUserText(c.prompt);
  if (q.includes('TOOLS')) {
    if (c.step === 0) return [{ tool: 'get_metric', input: { metric: 'V', day: DAY } }];
    if (c.step === 1) return [{ tool: 'query_db', input: { sql: 'SELECT id AS turn_id FROM turn ORDER BY id LIMIT 2' } }];
    return [{ text: 'V is 0.5 [1].' }];
  }
  if (q.includes('DELEGATE')) {
    if (c.step === 0) return [{ tool: 'get_metric', input: { metric: 'V', day: DAY } }];
    if (c.step === 1) {
      return [{ tool: 'agent-turnInvestigator', input: { prompt: `Investigate turns ${ids['0']} and ${ids['3']}: verified?` } }];
    }
    return [{ text: 'Half the batches were unverified [1]; see [2][3].' }];
  }
  if (q.includes('LOOP')) {
    if (c.step < 2) return [{ tool: 'get_metric', input: { metric: 'B_min', day: DAY } }];
    if (c.step === 2) return c.tools.includes('get_metric') ? [{ tool: 'get_metric', input: { metric: 'B_min', day: DAY } }]
      : [{ text: 'guarded' }];
    return [{ text: 'still looping' }];
  }
  return [{ text: 'hello' }];
});

const investigatorModel = scriptedModel('mock-haiku', (c) => {
  if (c.step === 0) {
    const m = lastUserText(c.prompt).match(/turns (\d+) and (\d+)/);
    return [{ tool: 'get_turn', input: { turn_id: Number(m![1]) } }, { tool: 'get_turn', input: { turn_id: Number(m![2]) } }];
  }
  const ns = [...toolResultsText(c.prompt).matchAll(/\\?"n\\?":(\d+)/g)].map((m) => m[1]);
  return [{ text: `turn A tested [${ns[0]}]; turn B interrupted [${ns[1]}].` }];
});

const sqlModel = scriptedModel('mock-sql', () => [{ text: 'n/a' }]);

beforeAll(async () => {
  scorer = await startScorer();
  ids = scorer.turnIds;
  app = createScorerMastra({
    models: { analyst: analystModel, turnInvestigator: investigatorModel, sqlAnalyst: sqlModel },
    traceDir: join(scorer.dir, 'traces'),
    onGuardTrip: (t) => trips.push(t),
  });
  sidecar = makeServer(app);
  const port = await freePort();
  await new Promise<void>((ok) => sidecar.listen(port, '127.0.0.1', ok));
  sidecarUrl = `http://127.0.0.1:${port}`;
  proxied = await startScorer({ SCORER_MASTRA_URL: sidecarUrl });
}, 60_000);

afterAll(async () => {
  await Promise.all([scorer?.stop(), proxied?.stop()]);
  await new Promise<void>((ok) => (sidecar ? sidecar.close(() => ok()) : ok()));
});

async function chatDirect(q: string) {
  const res = await fetch(`${sidecarUrl}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Scorer-Url': scorer.url },
    body: JSON.stringify({ messages: [{ role: 'user', content: q }], day: DAY }),
  });
  expect(res.headers.get('content-type')).toMatch(/text\/event-stream/);
  return sse(await res.text());
}

describe('(a) tool call -> HTTP wrapper -> real scorer serve', () => {
  it('streams tool-start/tool-result in the Python event shape with numbered citations', async () => {
    const evs = await chatDirect('TOOLS: why is V low?');
    expect(evs.map((e) => e.type)).toEqual(['tool-start', 'tool-result', 'tool-start', 'tool-result', 'text-delta', 'done']);
    const [s1, r1, , r2] = evs;
    expect(s1).toMatchObject({ name: 'get_metric', input: { metric: 'V', day: DAY } });
    expect(r1).toMatchObject({ id: s1.id, name: 'get_metric', ok: true });
    expect(r1.summary).toMatch(/^V 2026-10-13: 0\.5/);
    expect(r1.citations).toEqual([expect.objectContaining({ n: 1, turn_id: ids['1'] })]);
    // query_db numbering continues after get_metric's single citation
    expect(r2.citations.map((c: any) => c.n)).toEqual([2, 3]);
    expect(evs.at(-1)).toEqual({ type: 'done', model: 'mock-analyst', backend: 'mastra' });
    // the model saw Python's model-facing body: data with n slots, no summary/citations keys
    const seen = toolResultsText(analystModel.seen.at(-1)!.prompt);
    expect(seen).toContain('evidence');
    expect(seen).not.toMatch(/\\?"summary\\?"/);
    for (const e of evs) for (const c of e.citations ?? []) expect(c.label.length).toBeLessThanOrEqual(80);
  });

  it('system prompt comes from Python and carries the metric definitions + delegation section', () => {
    const sys = analystModel.seen.at(-1)!.system;
    expect(sys).toContain('<grounding>');
    expect(sys).toContain('Verification after changes');
    expect(sys).toContain('<delegation>');
    expect(lastUserText(analystModel.seen.at(-1)!.prompt)).toContain(`<dashboard_context>day on screen: ${DAY}`);
  });
});

describe('(b) delegation to a subagent', () => {
  it('runs turnInvestigator; its tool calls and citations come back with consistent numbering', async () => {
    const before = investigatorModel.seen.length;
    const evs = await chatDirect('DELEGATE: investigate the worst turns');
    const types = evs.map((e) => e.type);
    expect(types.slice(0, 3)).toEqual(['tool-start', 'tool-result', 'subagent-start']); // analyst get_metric -> [1]
    // investigator's two get_turn calls run in parallel (start/result may interleave) -> [2], [3]
    expect(types.slice(3, 7).sort()).toEqual(['tool-result', 'tool-result', 'tool-start', 'tool-start']);
    expect(types.slice(7)).toEqual(['subagent-result', 'text-delta', 'done']);
    expect(investigatorModel.seen.length - before).toBe(2);
    const sub = evs[2];
    expect(sub).toMatchObject({ name: 'turnInvestigator', task: expect.stringContaining(`turns ${ids['0']}`) });
    const subTools = evs.filter((e) => e.type === 'tool-result').slice(1);
    expect(subTools.map((e) => e.name)).toEqual(['get_turn', 'get_turn']);
    const ns = evs.flatMap((e) => (e.citations ?? []).map((c: any) => c.n));
    expect(ns).toEqual([1, 2, 3]); // one numbering across analyst + subagent
    expect(subTools.flatMap((e) => e.citations.map((c: any) => c.turn_id)).sort()).toEqual([ids['0'], ids['3']].sort());
    const result = evs.find((e) => e.type === 'subagent-result');
    expect(result).toMatchObject({ id: sub.id, name: 'turnInvestigator' });
    expect(result.summary).toMatch(/\[2\].*\[3\]/);
    // the investigator only had its own tools
    expect(investigatorModel.seen.at(-1)!.tools.sort()).toEqual(['get_metric', 'get_turn']);
  });
});

describe('(c) read-loop guard', () => {
  it('withdraws a tool after an identical call repeats within the turn', async () => {
    const evs = await chatDirect('LOOP: blocked minutes?');
    const calls = analystModel.seen.filter((c) => lastUserText(c.prompt).includes('LOOP'));
    expect(calls).toHaveLength(3);
    expect(calls[1]!.tools).toContain('get_metric');
    expect(calls[2]!.tools).not.toContain('get_metric');
    expect(calls[2]!.tools).toContain('query_db');
    expect(trips).toContainEqual({ agent: 'analyst', withdrawn: ['get_metric'] });
    const results = evs.filter((e) => e.type === 'tool-result');
    expect(results[1]).toMatchObject({ ok: false, summary: 'duplicate call skipped' });
    expect(evs.at(-2)).toEqual({ type: 'text-delta', delta: 'guarded' });
  });
});

describe('full chain through the Python proxy', () => {
  it('scorer /api/chat proxies to the sidecar, sanitizes, and reports backend mastra', async () => {
    const st = await (await fetch(`${proxied.url}/api/chat/status`)).json();
    expect(st.backend).toBe('mastra');
    const res = await fetch(`${proxied.url}/api/chat`, {
      method: 'POST',
      body: JSON.stringify({ messages: [{ role: 'user', content: 'DELEGATE: why?' }], day: DAY }),
    });
    const evs = sse(await res.text());
    expect(evs.map((e) => e.type)).toContain('subagent-start');
    expect(evs.at(-1)).toEqual({ type: 'done', model: 'mock-analyst', backend: 'mastra' });
    // tools were called back on the proxying scorer (X-Scorer-Url), so its own turn ids resolve
    expect(evs.filter((e) => e.type === 'tool-result').every((e) => e.ok)).toBe(true);
  });
});

describe('traces', () => {
  it('writes span rows (run, model, tools, delegation) to $SCORER_HOME/traces/<date>.jsonl, no prompt text', () => {
    const dir = join(scorer.dir, 'traces');
    const files = readdirSync(dir).filter((f) => f.endsWith('.jsonl'));
    expect(files).toHaveLength(1);
    const raw = readFileSync(join(dir, files[0]!), 'utf8');
    const rows = raw.trim().split('\n').map((l) => JSON.parse(l));
    const types = new Set(rows.map((r) => r.type));
    expect(types).toContain('agent_run');
    expect(types).toContain('tool_call');
    expect(types).toContain('model_generation');
    expect(rows.some((r) => r.delegation === 'turnInvestigator')).toBe(true);
    expect(rows.some((r) => r.model === 'mock-haiku' && r.usage?.input > 0)).toBe(true);
    expect(rows.every((r) => r.latency_ms === null || r.latency_ms >= 0)).toBe(true);
    expect(raw).not.toContain('please refactor');
    expect(raw).not.toContain('Investigate turns');
  });
});
