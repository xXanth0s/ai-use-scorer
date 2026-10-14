import { RequestContext } from '@mastra/core/request-context';

import { config } from './config';
import { RUN_KEY, RunState } from './lib/run-state';
import { fetchSystemPrompt } from './lib/scorer-client';
import type { ScorerMastra } from './mastra';
import { suggestFollowups, type ToolTrace } from './suggest';
import { errorText, StreamTranslator, type ChatEvent } from './translate';

export type ChatBody = { messages?: Array<{ role?: string; content?: unknown }>; day?: string | null };

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

/** agent.py _to_api_messages: user/assistant text only, starts and ends on a user turn, the last
 *  one carrying the dashboard context. */
export function toMessages(body: ChatBody, day: string | null, today: string) {
  const out: Array<{ role: 'user'; content: string } | { role: 'assistant'; content: string }> = [];
  for (const m of body.messages ?? []) {
    if ((m?.role === 'user' || m?.role === 'assistant') && typeof m.content === 'string' && m.content.trim()) {
      out.push(m.role === 'user' ? { role: 'user', content: m.content } : { role: 'assistant', content: m.content });
    }
  }
  while (out.length && out[0]!.role !== 'user') out.shift();
  while (out.length && out[out.length - 1]!.role !== 'user') out.pop();
  if (out.length) {
    const last = out[out.length - 1]!;
    last.content += `\n\n<dashboard_context>day on screen: ${day ?? 'none (all days)'}; today: ${today}</dashboard_context>`;
  }
  return out;
}

export type ChatStats = { steps: number; subagentSteps: number; models: string[]; toolCalls: number; citations: number; ms: number };

/** One assistant answer as dashboard events. Always ends with exactly one `done`. */
export async function* runChat(
  scorer: ScorerMastra,
  body: ChatBody,
  opts: { scorerUrl: string; abortSignal?: AbortSignal; stats?: (s: ChatStats) => void },
): AsyncGenerator<ChatEvent> {
  const t0 = Date.now();
  const day = typeof body.day === 'string' && DAY_RE.test(body.day) ? body.day : null;
  const tr = new StreamTranslator(config.analystModel, config.analystMaxSteps);
  const run = new RunState(opts.scorerUrl, day);
  const text: string[] = [];
  const tools = new Map<string, ToolTrace>(); // analyst-level tool calls, for the follow-up tiles
  let failed = false;
  const seen = (ev: ChatEvent): ChatEvent => {
    if (ev.type === 'text-delta') text.push(ev.delta);
    else if (ev.type === 'error') failed = true;
    else if (ev.type === 'tool-start' && !ev.subagent_id) tools.set(ev.id, { name: ev.name, input: ev.input });
    else if (ev.type === 'tool-result' && tools.has(ev.id)) Object.assign(tools.get(ev.id)!, { summary: ev.summary, citations: ev.citations });
    else if (ev.type === 'subagent-result') tools.set(ev.id, { name: `agent-${ev.name}`, input: {}, summary: ev.summary });
    return ev;
  };
  try {
    try {
      for await (const ev of answer(scorer, body, run, tr, opts.abortSignal)) yield seen(ev);
    } catch (e) {
      for (const ev of tr.flush()) yield seen(ev);
      yield seen({ type: 'error', message: errorText(e).slice(0, 80) });
    }
    for (const ev of tr.flush()) yield seen(ev);
    const answerText = text.join('').trim();
    if (answerText && !failed && !opts.abortSignal?.aborted) {
      const question = toMessages(body, run.day, '').at(-1)?.content ?? '';
      const items = await suggestFollowups(scorer.mastra.getAgent('suggester'), question, answerText, [...tools.values()],
                                           { abortSignal: opts.abortSignal });
      if (items.length) yield { type: 'suggestions', items };
    }
    yield tr.done();
  } finally {
    // also on abort: the consumer stops iterating (generator return) or the stream throws
    try {
      opts.stats?.({ steps: tr.steps, subagentSteps: tr.subagentSteps, models: [...tr.models], toolCalls: run.toolCalls,
                     citations: run.citations.length, ms: Date.now() - t0 });
    } catch {
      /* stats are best-effort */
    }
  }
}

async function* answer(
  scorer: ScorerMastra,
  body: ChatBody,
  run: RunState,
  tr: StreamTranslator,
  abortSignal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  let today: string;
  try {
    const info = await fetchSystemPrompt(run.scorerUrl);
    run.systemPrompt = info.system_prompt;
    today = info.today;
  } catch (e) {
    yield { type: 'error', message: `cannot reach scorer: ${errorText(e)}`.slice(0, 80) };
    return;
  }
  const messages = toMessages(body, run.day, today);
  if (!messages.length) {
    yield { type: 'error', message: 'empty question' };
    return;
  }
  const requestContext = new RequestContext();
  requestContext.set(RUN_KEY, run);
  const stream = await scorer.mastra.getAgent('analyst').stream(messages, {
    requestContext,
    maxSteps: config.analystMaxSteps,
    ...(abortSignal ? { abortSignal } : {}),
  });
  for await (const chunk of stream.fullStream as AsyncIterable<any>) {
    for (const ev of tr.translate(chunk)) yield ev;
  }
}
