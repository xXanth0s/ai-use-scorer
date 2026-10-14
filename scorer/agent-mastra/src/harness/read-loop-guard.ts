import type { MastraDBMessage } from '@mastra/core/agent';
import type { InputProcessor, ProcessInputStepArgs, ProcessInputStepResult } from '@mastra/core/processors';

import { stableJson } from '../tools';

/** Mirrors Sherpa's harness/read-loop-guard.ts. All four scorer tools are reads. */
export const IDENTICAL_CALLS_BEFORE_WITHDRAWAL = 2;

type ToolInvocation = { toolName: string; args?: unknown; state?: string };

/** The current turn = the last user message and everything the loop appended after it. */
export function toolInvocationsThisTurn(messages: readonly MastraDBMessage[]): ToolInvocation[] {
  const lastUser = messages.findLastIndex((m) => m.role === 'user' || (m.role as string) === 'signal');
  if (lastUser === -1) return [];
  return messages
    .slice(lastUser + 1)
    .flatMap((m) => (Array.isArray(m.content?.parts) ? m.content.parts : []))
    .flatMap((part: any) => (part?.type === 'tool-invocation' && part.toolInvocation ? [part.toolInvocation] : []));
}

/** Tools called with identical args at least twice this turn. Keyed on [toolName, args] only — a
 *  repeat cannot tell the model anything the first call did not (the scorer DB is a snapshot). */
export function repeatedTools(messages: readonly MastraDBMessage[]): string[] {
  const seen = new Map<string, number>();
  const repeated = new Set<string>();
  for (const call of toolInvocationsThisTurn(messages)) {
    const key = stableJson([call.toolName, call.args ?? {}]);
    const n = (seen.get(key) ?? 0) + 1;
    seen.set(key, n);
    if (n >= IDENTICAL_CALLS_BEFORE_WITHDRAWAL) repeated.add(call.toolName);
  }
  return [...repeated];
}

export type GuardTrip = { agent: string; withdrawn: string[] };

export function readLoopGuardStep(
  { messages, activeTools, tools }: Pick<ProcessInputStepArgs, 'messages' | 'activeTools' | 'tools'>,
  onTrip?: (withdrawn: string[]) => void,
): ProcessInputStepResult | undefined {
  const withdrawn = repeatedTools(messages);
  const offered = activeTools ?? Object.keys(tools ?? {});
  if (!withdrawn.some((name) => offered.includes(name))) return undefined;
  onTrip?.(withdrawn);
  return { activeTools: offered.filter((name) => !withdrawn.includes(name)) };
}

/** Identical tool+args twice in one turn -> that tool is not offered again for the rest of the turn. */
export function makeReadLoopGuardProcessor(agent: string, onTrip?: (t: GuardTrip) => void): InputProcessor {
  return {
    id: `read-loop-guard-${agent}`,
    processInputStep: (args) => readLoopGuardStep(args, (withdrawn) => onTrip?.({ agent, withdrawn })),
  };
}
