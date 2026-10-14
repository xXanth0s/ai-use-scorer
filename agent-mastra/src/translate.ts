import { clip } from './config';
import type { Citation } from './lib/scorer-client';
import { THINKING_STEP, type ThinkingStep } from './lib/thinking-step';

/** The Python agent's event contract (scorer/agent.py stream_chat), plus the two subagent events. */
export type ChatEvent =
  | { type: 'text-delta'; delta: string }
  | { type: 'tool-start'; id: string; name: string; input: Record<string, unknown> }
  | { type: 'tool-result'; id: string; name: string; ok: boolean; summary: string; citations: Citation[] }
  | { type: 'subagent-start'; id: string; name: string; task: string }
  | { type: 'subagent-result'; id: string; name: string; summary: string }
  | { type: 'error'; message: string }
  | { type: 'done'; model: string | null; backend: 'mastra' };

const DELEGATION = /^agent-(.+)$/;

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
 */
export class StreamTranslator {
  model: string | null = null;
  steps = 0;
  subagentSteps = 0;
  readonly models = new Set<string>();

  constructor(
    private readonly defaultModel: string | null = null,
    private readonly maxSteps = 8,
  ) {}

  translate(chunk: any): ChatEvent[] {
    const p = chunk?.payload ?? {};
    switch (chunk?.type) {
      case 'text-delta':
        return p.text ? [{ type: 'text-delta', delta: String(p.text) }] : [];
      case THINKING_STEP: {
        const d = (chunk.data ?? {}) as ThinkingStep;
        if (d.status === 'in-progress') {
          return [{ type: 'tool-start', id: String(d.id), name: String(d.step), input: d.input ?? {} }];
        }
        return [{ type: 'tool-result', id: String(d.id), name: String(d.step), ok: Boolean(d.ok),
                  summary: clip(d.summary, 80), citations: Array.isArray(d.citations) ? d.citations : [] }];
      }
      case 'tool-call': {
        const m = DELEGATION.exec(String(p.toolName ?? ''));
        if (!m) return [];
        return [{ type: 'subagent-start', id: String(p.toolCallId), name: m[1]!, task: clip(p.args?.prompt, 80) }];
      }
      case 'tool-result': {
        const m = DELEGATION.exec(String(p.toolName ?? ''));
        if (!m) return [];
        const r = p.result;
        const text = typeof r === 'string' ? r : r?.text;
        return [{ type: 'subagent-result', id: String(p.toolCallId), name: m[1]!,
                  summary: p.isError ? `failed: ${clip(text ?? r?.message, 70)}` : clip(text || '(empty brief)', 80) }];
      }
      case 'tool-error': {
        const m = DELEGATION.exec(String(p.toolName ?? ''));
        const msg = p.error?.message ?? String(p.error ?? 'tool failed');
        if (m) return [{ type: 'subagent-result', id: String(p.toolCallId), name: m[1]!, summary: `failed: ${clip(msg, 70)}` }];
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
        return [];
      }
      case 'finish':
        // agent.py: the last step still wanted a tool -> the step cap ended the answer
        return p.stepResult?.reason === 'tool-calls'
          ? [{ type: 'error', message: `step cap (${this.maxSteps}) reached before a final answer` }]
          : [];
      case 'error':
        return [{ type: 'error', message: clip(errorText(p.error ?? p), 80) }];
      case 'tripwire':
        return [{ type: 'error', message: clip(`stopped by guard: ${p.reason ?? 'tripwire'}`, 80) }];
      default:
        return [];
    }
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
