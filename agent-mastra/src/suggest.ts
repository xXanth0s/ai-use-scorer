import { Agent } from '@mastra/core/agent';
import type { MastraModelConfig } from '@mastra/core/llm';
import { RequestContext } from '@mastra/core/request-context';
import { createTool } from '@mastra/core/tools';
import { z } from 'zod';

import { clip, config } from './config';
import type { Citation } from './lib/scorer-client';

/**
 * Quick-response tiles under the last answer (Sherpa harness/turn-chips.ts, tier 2): derived AFTER
 * the answer by one bounded call to a small model, never asked of the analyst's tool loop. Strict
 * tool schema, hard timeout, sanitised (trim, dedupe, cap); any failure means no tiles. Mirrors
 * scorer/agent.py suggest_followups (same system prompt and limits).
 */
export type Suggestion = { label: string; prompt: string };
export type ToolTrace = { name: string; input: unknown; summary?: string; citations?: Citation[] };

export const SUGGEST_TOOL = 'suggest_followups';
const CAPTURE_KEY = 'suggestions';
export const MAX_SUGGESTIONS = 4;

export const SUGGEST_SYSTEM = `<role>
You write quick-response tiles for a chat where one human asks an analyst about their own coding-agent usage (scorer metrics: verification V, corrections C, hand-edits H, blocked minutes B, cost, tokens, sessions, turns).
</role>

<task>
Given the user's latest question, the analyst's answer and the tools it used, call suggest_followups with 2 to 4 follow-up questions the user is most likely to ask next.
</task>

<rules>
1. Build on THIS answer: name the concrete things it surfaced — a turn id, a repo, a metric, a day, a count ("Why did turn 1423 fail?", "Show the 4 unverified sessions", "Compare with last week"). Never generic ("Tell me more", "Any other insights?").
2. Each must be answerable by the analyst with its tools (metrics, turns, sessions, SQL over the scorer DB). No actions outside the data (no "fix my code", no "open the file").
3. label: at most 40 characters, imperative or question, no trailing period. prompt: at most 200 characters, a complete, self-contained question in the user's voice that names the day / turn ids / repo it refers to.
4. All distinct from each other and from the question already asked.
5. Never copy prompt or command excerpts from the answer into a tile; refer to turns by id.
6. Write in the language of the user's question.
</rules>`;

const itemsSchema = z.object({
  items: z
    .array(
      z.object({
        label: z.string().describe('Tile text, at most 40 characters.'),
        prompt: z.string().describe('The full question sent when the tile is clicked, at most 200 characters.'),
      }),
    )
    .describe('2 to 4 follow-ups.'),
});

export function buildSuggester(model: MastraModelConfig): Agent {
  return new Agent({
    id: 'suggester',
    name: 'suggester',
    description: 'Post-answer quick-response tiles (bounded, best-effort).',
    model,
    instructions: { role: 'system', content: SUGGEST_SYSTEM, providerOptions: { anthropic: { cacheControl: { type: 'ephemeral' } } } },
    tools: {
      [SUGGEST_TOOL]: createTool({
        id: SUGGEST_TOOL,
        description: 'Record the follow-up tiles.',
        inputSchema: itemsSchema,
        execute: async (input, ctx) => {
          ctx?.requestContext?.set(CAPTURE_KEY, input);
          return { ok: true };
        },
      }),
    },
  });
}

/** Trim, single-line, label ≤ 40 / prompt ≤ 200, dedupe by label, cap 4; fewer than 2 -> []. */
export function sanitizeSuggestions(raw: unknown): Suggestion[] {
  const out: Suggestion[] = [];
  const seen = new Set<string>();
  for (const it of Array.isArray(raw) ? raw : []) {
    if (!it || typeof it !== 'object') continue;
    const label = clip(String((it as any).label ?? '').replace(/[\s.]+$/, ''), 40);
    const prompt = clip(String((it as any).prompt ?? ''), 200);
    if (!label || !prompt || seen.has(label.toLowerCase())) continue;
    seen.add(label.toLowerCase());
    out.push({ label, prompt });
    if (out.length === MAX_SUGGESTIONS) break;
  }
  return out.length >= 2 ? out : [];
}

const CTX_RE = /<dashboard_context>([\s\S]*?)<\/dashboard_context>/;

export function suggestInput(question: string, answer: string, tools: ToolTrace[]): string {
  const q = question.replace(CTX_RE, '').trim();
  const ctx = CTX_RE.exec(question);
  const lines = tools.slice(0, 12).map((t) => {
    const cites = (t.citations ?? []).slice(0, 6).map((c) => `[${c.n}] turn ${c.turn_id}`).join(', ');
    return clip(`${t.name}(${JSON.stringify(t.input ?? {})}) -> ${t.summary ?? ''}${cites ? ` · ${cites}` : ''}`, 240);
  });
  return (
    `<question>${clip(q, 300)}</question>\n` +
    (ctx ? `<dashboard>${clip(ctx[1], 120)}</dashboard>\n` : '') +
    `<tools_used>\n${lines.join('\n') || '(none)'}\n</tools_used>\n` +
    `<answer>\n${answer.slice(0, 2000)}\n</answer>`
  );
}

/** One bounded call; never throws, [] on timeout / error / too few items. */
export async function suggestFollowups(
  agent: Agent,
  question: string,
  answer: string,
  tools: ToolTrace[],
  opts: { abortSignal?: AbortSignal; timeoutMs?: number } = {},
): Promise<Suggestion[]> {
  const timeoutMs = opts.timeoutMs ?? config.suggestTimeoutMs;
  const signals = [AbortSignal.timeout(timeoutMs), ...(opts.abortSignal ? [opts.abortSignal] : [])];
  const abortSignal = AbortSignal.any(signals);
  const requestContext = new RequestContext();
  let timer: NodeJS.Timeout | undefined;
  try {
    const run = agent.generate([{ role: 'user', content: suggestInput(question, answer, tools) }], {
      requestContext,
      abortSignal,
      maxSteps: 1,
      toolChoice: { type: 'tool', toolName: SUGGEST_TOOL },
      modelSettings: { maxOutputTokens: 800 },
    });
    run.catch(() => undefined); // a late rejection after the deadline won the race
    // backstop for a run that swallows its abort and never settles
    const deadline = new Promise<never>((_, fail) => {
      timer = setTimeout(() => fail(new Error('suggestions timed out')), timeoutMs + 500);
    });
    await Promise.race([run, deadline]);
    const got = requestContext.get(CAPTURE_KEY) as { items?: unknown } | undefined;
    return sanitizeSuggestions(got?.items);
  } catch {
    return [];
  } finally {
    clearTimeout(timer);
  }
}
