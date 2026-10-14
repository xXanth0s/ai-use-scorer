import { appendFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

import { TracingEventType, type TracingEvent } from '@mastra/core/observability';
import { BaseExporter } from '@mastra/observability';

import { clip, config } from '../config';

/** Span types worth a row: the run, each model call, each tool call / delegation. */
const KEEP = new Set(['agent_run', 'model_generation', 'model_step', 'tool_call', 'generic']);

export type TraceRow = {
  ts: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  type: string;
  name: string;
  entity: string | null;
  latency_ms: number | null;
  model?: string;
  provider?: string;
  usage?: { input?: number; output?: number; cached?: number; reasoning?: number };
  finish_reason?: string;
  delegation?: string;
  error?: string;
};

function localDate(d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** Pure: one ended span -> one JSONL row. Never copies span input/output (they hold prompt text). */
export function spanRow(span: any): TraceRow | null {
  if (!span || !KEEP.has(span.type)) return null;
  const start = span.startTime ? new Date(span.startTime).getTime() : NaN;
  const end = span.endTime ? new Date(span.endTime).getTime() : NaN;
  const a = span.attributes ?? {};
  const row: TraceRow = {
    ts: new Date(Number.isFinite(end) ? end : Date.now()).toISOString(),
    trace_id: String(span.traceId ?? ''),
    span_id: String(span.id ?? ''),
    parent_span_id: span.parentSpanId ? String(span.parentSpanId) : null,
    type: String(span.type),
    name: clip(span.name, 80),
    entity: span.entityName ? clip(span.entityName, 40) : span.entityId ? clip(span.entityId, 40) : null,
    latency_ms: Number.isFinite(start) && Number.isFinite(end) ? end - start : null,
  };
  if (a.model) row.model = clip(a.model, 40);
  if (a.provider) row.provider = clip(a.provider, 40);
  if (a.usage) {
    row.usage = {
      input: a.usage.inputTokens,
      output: a.usage.outputTokens,
      cached: a.usage.inputDetails?.cacheRead,
      reasoning: a.usage.outputDetails?.reasoning,
    };
  }
  if (a.finishReason) row.finish_reason = clip(a.finishReason, 20);
  if (span.type === 'tool_call' && /^agent-/.test(String(a.toolName ?? span.entityName ?? span.name ?? ''))) {
    row.delegation = clip(String(a.toolName ?? span.entityName ?? span.name).replace(/^.*agent-/, ''), 40);
  }
  if (span.errorInfo?.message) row.error = clip(span.errorInfo.message, 80);
  return row;
}

/** Local JSONL trace export: $SCORER_HOME/traces/<local date>.jsonl, one row per ended span. */
export class JsonlTraceExporter extends BaseExporter {
  name = 'scorer-jsonl';
  private queue: Promise<void> = Promise.resolve();

  constructor(private readonly dir = join(config.scorerHome, 'traces')) {
    super({ logLevel: 'error' });
  }

  protected async _exportTracingEvent(event: TracingEvent): Promise<void> {
    if (event.type !== TracingEventType.SPAN_ENDED) return;
    const row = spanRow(event.exportedSpan);
    if (!row) return;
    const file = join(this.dir, `${localDate()}.jsonl`);
    this.queue = this.queue
      .then(() => mkdir(this.dir, { recursive: true }))
      .then(() => appendFile(file, `${JSON.stringify(row)}\n`))
      .catch(() => undefined); // tracing never breaks a chat
    await this.queue;
  }

  async flush(): Promise<void> {
    await this.queue;
  }
}
