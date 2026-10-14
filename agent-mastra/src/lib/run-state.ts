import type { RequestContext } from '@mastra/core/request-context';

import type { Citation } from './scorer-client';

export const RUN_KEY = 'scorerRun';

/**
 * Per-request state, carried in the RequestContext. Mastra's delegation copies the context
 * entries SHALLOWLY into each sub-agent run (@mastra/core 1.70 agent bundle: `new RequestContext(
 * [...requestContext.entries()])`), so this one object is shared by the analyst and every
 * sub-agent it delegates to within one assistant turn — which is what keeps citation numbers
 * consistent across them.
 */
export class RunState {
  /** Next citation number (agent.run_tool's n0). */
  nextN = 1;
  readonly citations: Citation[] = [];
  /** `${agent}:${tool}:${args}` -> citation numbers of the first result (Python's duplicate rule). */
  readonly seen = new Map<string, number[]>();
  toolCalls = 0;
  systemPrompt = '';
  private lock: Promise<void> = Promise.resolve();

  constructor(
    readonly scorerUrl: string,
    readonly day: string | null,
  ) {}

  /** Serialise tool executions so n0 is reserved and advanced atomically, even when the model
   *  issues parallel tool calls or two sub-agents run at once. */
  async exclusive<T>(fn: () => Promise<T>): Promise<T> {
    const prev = this.lock;
    let release!: () => void;
    this.lock = new Promise((r) => (release = r));
    await prev;
    try {
      return await fn();
    } finally {
      release();
    }
  }
}

export function getRun(ctx: { requestContext?: RequestContext } | undefined): RunState | undefined {
  const v = ctx?.requestContext?.get(RUN_KEY);
  return v instanceof RunState ? v : undefined;
}
