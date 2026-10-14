import type { ToolStream } from '@mastra/core/tools';

import type { Citation } from './scorer-client';

/** One scorer tool call as a `data-thinking-step` custom chunk (Sherpa's lib/thinking-step.ts).
 *  A sub-agent's custom chunks are forwarded into the parent's fullStream as-is, so the stream
 *  translator sees tool calls from every level through this one channel. */
export type ThinkingStep = {
  step: string; // tool name
  status: 'in-progress' | 'done';
  id: string;
  agent: string;
  input?: Record<string, unknown>;
  ok?: boolean;
  summary?: string;
  citations?: Citation[];
};

export const THINKING_STEP = 'data-thinking-step';

export async function emitThinkingStep(writer: ToolStream | undefined, data: ThinkingStep): Promise<void> {
  try {
    await writer?.custom({ type: THINKING_STEP, data });
  } catch {
    // a progress event is never worth failing a tool call over
  }
}
