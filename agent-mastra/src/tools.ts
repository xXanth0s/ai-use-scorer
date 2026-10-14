import { createTool } from '@mastra/core/tools';
import { z } from 'zod';

import { config } from './config';
import { getRun } from './lib/run-state';
import { callTool, type ToolName, type ToolResult } from './lib/scorer-client';
import { emitThinkingStep } from './lib/thinking-step';

/**
 * Thin HTTP wrappers over `scorer serve`'s POST /api/tools/<name>. Descriptions and schemas mirror
 * scorer/agent.py TOOLS. Errors come back as data ({error}), never thrown, so the model can adjust.
 */
const SPECS = {
  query_db: {
    description:
      'Run one read-only SQL SELECT (or WITH … SELECT) against the scorer SQLite DB. Free-text and path columns come ' +
      'back as ≤ 80-char excerpts. Returns {columns, rows, truncated}. Filter metric_value by run_id = (SELECT max(id) FROM ingest_run).',
    inputSchema: z.object({
      sql: z.string().describe('A single SELECT/WITH statement.'),
      limit: z.number().int().optional().describe('Max rows (1-200, default 50).'),
    }),
  },
  get_turn: {
    description:
      'Details of one turn: session (agent, repo), clipped prompt, timings, interrupted, tool calls (intent, clipped ' +
      'args, errors), API calls summary, file edits, metric evidence.',
    inputSchema: z.object({ turn_id: z.number().int().describe('turn.id from a citation, get_metric evidence or query_db.') }),
  },
  get_metric: {
    description:
      'Values of one metric id (e.g. V, H, B_min, active_s, context_p90, or a prefix like gap_bucket / V_tier / ' +
      'model_mix) from the latest run, with up to 20 evidence turns.',
    inputSchema: z.object({
      metric: z.string().describe('Metric id or family prefix, e.g. "V", "V_tier", "cost_usd".'),
      day: z.string().optional().describe('Local YYYY-MM-DD; default = the day on screen.'),
      scope_type: z.enum(['day', 'session', 'all']).optional().describe('Default "day".'),
    }),
  },
  list_sessions: {
    description:
      'Sessions active on a local day: id, agent, repo, turns, first/last prompt (local time), active seconds; ' +
      "each session's first turn carries a citation number n.",
    inputSchema: z.object({ day: z.string().optional().describe('Local YYYY-MM-DD; default = the day on screen.') }),
  },
} as const;

const DUPLICATE = 'identical call already made in this answer — reuse that result';
export const BUDGET_EXHAUSTED = 'call budget exhausted, summarise now';

/** What the model sees: agent.py's tool_result body (everything except ok/summary/citations). */
function modelFacing(r: ToolResult): Record<string, unknown> {
  const { ok: _ok, summary: _s, citations: _c, ...body } = r;
  return body;
}

function makeScorerTool(name: ToolName, agent: string) {
  const spec = SPECS[name];
  return createTool({
    id: name,
    description: spec.description,
    inputSchema: spec.inputSchema,
    execute: async (input, ctx) => {
      const run = getRun(ctx);
      const id = ctx?.agent?.toolCallId ?? `${name}-${Date.now()}`;
      const args = input as Record<string, unknown>;
      await emitThinkingStep(ctx?.writer, { step: name, status: 'in-progress', id, agent, input: args });
      let r: ToolResult;
      if (!run) {
        r = { ok: false, error: 'no scorer run context', summary: 'error: no scorer run context', citations: [] };
      } else {
        r = await run.exclusive(async () => {
          run.toolCalls += 1;
          const key = `${agent}:${name}:${stableJson(args)}`;
          const earlier = run.seen.get(key);
          if (earlier) {
            return { ok: false, error: DUPLICATE, earlier_citations: earlier, summary: 'duplicate call skipped',
                     citations: [] } as ToolResult;
          }
          const budget = config.toolBudgets[agent]?.[name];
          const used = run.executed.get(`${agent}:${name}`) ?? 0;
          if (budget !== undefined && used >= budget) {
            return { ok: false, error: BUDGET_EXHAUSTED, budget, summary: 'call budget exhausted', citations: [] } as ToolResult;
          }
          run.executed.set(`${agent}:${name}`, used + 1);
          const res = await callTool(run.scorerUrl, name, args, { n0: run.nextN, day: run.day });
          run.seen.set(key, res.citations.map((c) => c.n));
          run.nextN += res.citations.length;
          run.citations.push(...res.citations);
          return res;
        });
      }
      await emitThinkingStep(ctx?.writer, {
        step: name, status: 'done', id, agent, ok: r.ok, summary: r.summary, citations: r.citations,
      });
      return modelFacing(r);
    },
  });
}

export function stableJson(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stableJson).join(',')}]`;
  if (v && typeof v === 'object') {
    return `{${Object.keys(v as object).sort().filter((k) => (v as any)[k] !== undefined)
      .map((k) => `${JSON.stringify(k)}:${stableJson((v as any)[k])}`).join(',')}}`;
  }
  return JSON.stringify(v);
}

/** One tool set per agent (the agent label goes into thinking steps and the duplicate key). */
export function scorerTools<K extends ToolName>(agent: string, names: readonly K[]): Record<K, ReturnType<typeof makeScorerTool>> {
  return Object.fromEntries(names.map((n) => [n, makeScorerTool(n, agent)])) as Record<K, ReturnType<typeof makeScorerTool>>;
}
