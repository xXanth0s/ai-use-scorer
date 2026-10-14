import { clip } from './config';
import type { Citation } from './lib/scorer-client';
import { THINKING_STEP, type ThinkingStep } from './lib/thinking-step';

/** The Python agent's event contract (scorer/agent.py stream_chat), plus the two subagent events. */
export type ChatEvent =
  | { type: 'text-delta'; delta: string }
  | { type: 'tool-start'; id: string; name: string; input: Record<string, unknown>; agent?: string; subagent_id?: string }
  | { type: 'tool-result'; id: string; name: string; ok: boolean; summary: string; citations: Citation[];
      agent?: string; subagent_id?: string }
  | { type: 'subagent-start'; id: string; name: string; task: string }
  | { type: 'subagent-result'; id: string; name: string; summary: string }
  | { type: 'suggestions'; items: Array<{ label: string; prompt: string }> }
  | { type: 'error'; message: string }
  | { type: 'done'; model: string | null; backend: 'mastra' };

const DELEGATION = /^agent-(.+)$/;

/** Chars of a step's text held back: a step that then calls a tool drops them (thinking out loud
 *  before a tool call is not part of the answer); longer text streams live. Mirrors agent.py. */
export const PREAMBLE_HOLD = 160;

/**
 * Mastra fullStream chunk -> zero or more dashboard events. Stateful only for the model id.
 *
 * - Scorer tool calls (any level) arrive as `data-thinking-step` custom chunks emitted by the tool
 *   wrappers; a sub-agent's custom chunks are forwarded into the parent stream as-is. Mastra's own
 *   tool-call / tool-result chunks for those tools are therefore skipped (no double events).
 * - `agent-<key>` tool-call / tool-result chunks (Mastra's built-in delegation tools) become
 *   subagent-start / subagent-result.
 * - A sub-agent's other chunks arrive wrapped in `tool-output`; its prose never becomes top-level
 *   text (the analyst re-states what it needs).
 * - Top-level text is held per step until it is longer than PREAMBLE_HOLD or the step ends without
 *   a tool call; text of later steps is separated from earlier text by a paragraph break.
 * - Tool events carry the agent that ran the tool; a sub-agent's also carry `subagent_id` = the id
 *   of its subagent-start event.
 */
export class StreamTranslator {
  model: string | null = null;
  steps = 0;
  subagentSteps = 0;
  readonly models = new Set<string>();
  private held: string[] = [];
  private heldLen = 0;
  private live = false; // this step's text is streaming
  private toolStep = false; // this step called a tool: drop held text
  private textOut = false; // any text emitted in this answer
  private sep = '';
  private readonly openDelegations = new Map<string, string[]>(); // agent name -> open delegation ids

  /** Emit held text (with the step separator). */
  private release(): ChatEvent[] {
    if (!this.held.length) return [];
    const delta = this.sep + this.held.join('');
    this.held = [];
    this.heldLen = 0;
    this.textOut = true;
    this.sep = '';
    return [{ type: 'text-delta', delta }];
  }

  private endStep(): ChatEvent[] {
    const out = this.toolStep ? [] : this.release();
    this.held = [];
    this.heldLen = 0;
    this.live = false;
    this.toolStep = false;
    this.sep = this.textOut ? '\n\n' : '';
    return out;
  }

  /** Anything still held (stream ended without a step-finish, e.g. abort/error). */
  flush(): ChatEvent[] {
    return this.toolStep ? [] : this.release();
  }

  private stepAgent(d: ThinkingStep): { agent?: string; subagent_id?: string } {
    if (!d.agent) return {};
    const open = this.openDelegations.get(d.agent);
    const sub = open?.length ? open[open.length - 1] : undefined;
    return sub ? { agent: d.agent, subagent_id: sub } : { agent: d.agent };
  }

  constructor(
    private readonly defaultModel: string | null = null,
    private readonly maxSteps = 8,
  ) {}

  translate(chunk: any): ChatEvent[] {
    const p = chunk?.payload ?? {};
    switch (chunk?.type) {
      case 'text-delta': {
        if (!p.text) return [];
        const t = String(p.text);
        if (this.live) return [{ type: 'text-delta', delta: t }];
        this.held.push(t);
        this.heldLen += t.length;
        if (this.heldLen < PREAMBLE_HOLD) return [];
        this.live = true;
        return this.release();
      }
      case THINKING_STEP: {
        const d = (chunk.data ?? {}) as ThinkingStep;
        const who = this.stepAgent(d);
        if (d.status === 'in-progress') {
          return [{ type: 'tool-start', id: String(d.id), name: String(d.step), input: d.input ?? {}, ...who }];
        }
        return [{ type: 'tool-result', id: String(d.id), name: String(d.step), ok: Boolean(d.ok),
                  summary: clip(d.summary, 80), citations: Array.isArray(d.citations) ? d.citations : [], ...who }];
      }
      case 'tool-call-input-streaming-start':
        this.toolStep = true;
        return [];
      case 'tool-call': {
        this.toolStep = true;
        const m = DELEGATION.exec(String(p.toolName ?? ''));
        if (!m) return [];
        const id = String(p.toolCallId);
        this.openDelegations.set(m[1]!, [...(this.openDelegations.get(m[1]!) ?? []), id]);
        return [{ type: 'subagent-start', id, name: m[1]!, task: clip(p.args?.prompt, 80) }];
      }
      case 'tool-result': {
        const m = DELEGATION.exec(String(p.toolName ?? ''));
        if (!m) return [];
        this.closeDelegation(m[1]!, String(p.toolCallId));
        const r = p.result;
        const text = typeof r === 'string' ? r : r?.text;
        return [{ type: 'subagent-result', id: String(p.toolCallId), name: m[1]!,
                  summary: p.isError ? `failed: ${clip(text ?? r?.message, 70)}` : clip(text || '(empty brief)', 80) }];
      }
      case 'tool-error': {
        const m = DELEGATION.exec(String(p.toolName ?? ''));
        const msg = p.error?.message ?? String(p.error ?? 'tool failed');
        if (m) {
          this.closeDelegation(m[1]!, String(p.toolCallId));
          return [{ type: 'subagent-result', id: String(p.toolCallId), name: m[1]!, summary: `failed: ${clip(msg, 70)}` }];
        }
        return [];
      }
      case 'tool-output': {
        const o = p.output;
        if (o?.type === 'step-finish') {
          this.subagentSteps += 1;
          const id = o.payload?.metadata?.modelId;
          if (id) this.models.add(String(id));
        }
        return [];
      }
      case 'step-finish': {
        this.steps += 1;
        const id = p.metadata?.modelId ?? p.response?.modelId;
        if (id) {
          this.model = String(id);
          this.models.add(String(id));
        }
        if (p.stepResult?.reason === 'tool-calls') this.toolStep = true;
        return this.endStep();
      }
      case 'finish': {
        const out = this.flush();
        // agent.py: the last step still wanted a tool -> the step cap ended the answer
        return p.stepResult?.reason === 'tool-calls'
          ? [...out, { type: 'error', message: `step cap (${this.maxSteps}) reached before a final answer` }]
          : out;
      }
      case 'error':
        return [...this.flush(), { type: 'error', message: clip(errorText(p.error ?? p), 80) }];
      case 'tripwire':
        return [...this.flush(), { type: 'error', message: clip(`stopped by guard: ${p.reason ?? 'tripwire'}`, 80) }];
      default:
        return [];
    }
  }

  private closeDelegation(name: string, id: string) {
    const open = (this.openDelegations.get(name) ?? []).filter((x) => x !== id);
    if (open.length) this.openDelegations.set(name, open);
    else this.openDelegations.delete(name);
  }

  done(): Extract<ChatEvent, { type: 'done' }> {
    return { type: 'done', model: this.model ?? this.defaultModel, backend: 'mastra' };
  }
}

export function errorText(e: unknown): string {
  const anyE = e as { statusCode?: number; message?: string; name?: string };
  if (anyE?.statusCode === 401) return 'the API key was rejected';
  if (anyE?.statusCode === 429) return 'rate limited by the API — try again in a minute';
  if (anyE?.statusCode) return `API error ${anyE.statusCode}: ${anyE.message ?? ''}`;
  if (e instanceof Error) return `${e.name}: ${e.message}`;
  if (typeof e === 'string') return e;
  return anyE?.message ?? 'agent failed';
}
