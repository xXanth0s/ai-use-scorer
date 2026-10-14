import { describe, expect, it } from 'vitest';

import { readLoopGuardStep, repeatedTools } from '../src/harness/read-loop-guard';
import { spanRow } from '../src/harness/trace-exporter';
import { toMessages } from '../src/chat';
import { StreamTranslator } from '../src/translate';

const cite = { n: 4, turn_id: 7, session_id: 's', label: 'turn 7' };

describe('(d) Mastra chunk -> Python event shape', () => {
  it('maps text, thinking steps, delegations and finish to the dashboard contract', () => {
    const tr = new StreamTranslator('fallback-model');
    const chunks = [
      { type: 'start', payload: {} },
      { type: 'text-delta', payload: { id: 't', text: 'Hi ' } },
      { type: 'tool-call', payload: { toolCallId: 'c1', toolName: 'get_turn', args: { turn_id: 7 } } }, // native: skipped
      { type: 'data-thinking-step', data: { step: 'get_turn', status: 'in-progress', id: 'c1', agent: 'analyst', input: { turn_id: 7 } } },
      { type: 'data-thinking-step', data: { step: 'get_turn', status: 'done', id: 'c1', agent: 'analyst', ok: true,
                                            summary: 'x'.repeat(200), citations: [cite] } },
      { type: 'tool-result', payload: { toolCallId: 'c1', toolName: 'get_turn', result: {} } }, // native: skipped
      { type: 'tool-call', payload: { toolCallId: 'd1', toolName: 'agent-sqlAnalyst', args: { prompt: 'q'.repeat(300) } } },
      { type: 'tool-output', payload: { toolCallId: 'd1', output: { type: 'text-delta', payload: { text: 'SUB PROSE' } } } },
      { type: 'tool-output', payload: { toolCallId: 'd1', output: { type: 'step-finish', payload: { metadata: { modelId: 'sub-m' } } } } },
      { type: 'tool-result', payload: { toolCallId: 'd1', toolName: 'agent-sqlAnalyst', result: { text: '42 rows [4]' } } },
      { type: 'step-finish', payload: { metadata: { modelId: 'claude-opus-5-5' } } },
      { type: 'error', payload: { error: new Error('boom') } },
    ];
    const evs = chunks.flatMap((c) => tr.translate(c));
    expect(evs).toEqual([
      // 'Hi ' was written before a tool call in the same step: held back and dropped
      { type: 'tool-start', id: 'c1', name: 'get_turn', input: { turn_id: 7 }, agent: 'analyst' },
      { type: 'tool-result', id: 'c1', name: 'get_turn', ok: true, summary: `${'x'.repeat(79)}…`, citations: [cite],
        agent: 'analyst' },
      { type: 'subagent-start', id: 'd1', name: 'sqlAnalyst', task: `${'q'.repeat(79)}…` },
      { type: 'subagent-result', id: 'd1', name: 'sqlAnalyst', summary: '42 rows [4]' },
      { type: 'error', message: 'Error: boom' },
    ]);
    expect(JSON.stringify(evs)).not.toContain('SUB PROSE'); // sub-agent prose never becomes top-level text
    expect(tr.done()).toEqual({ type: 'done', model: 'claude-opus-5-5', backend: 'mastra' });
    expect([...tr.models]).toEqual(['sub-m', 'claude-opus-5-5']);
    expect([tr.steps, tr.subagentSteps]).toEqual([1, 1]);
    expect(new StreamTranslator('fallback-model').done().model).toBe('fallback-model');
  });

  it('holds pre-tool text, releases answer text, separates steps with a paragraph break', () => {
    const tr = new StreamTranslator();
    const td = (text: string) => ({ type: 'text-delta', payload: { text } });
    const out = [
      td('Let me check.'), { type: 'tool-call', payload: { toolCallId: 'a', toolName: 'get_metric', args: {} } },
      { type: 'step-finish', payload: { stepResult: { reason: 'tool-calls' } } },
      td('Long reasoning before another tool. '.repeat(6)), // > PREAMBLE_HOLD: streams live
      td('more'), { type: 'tool-call', payload: { toolCallId: 'b', toolName: 'get_turn', args: {} } },
      { type: 'step-finish', payload: { stepResult: { reason: 'tool-calls' } } },
      td('V is 50% '), td('[1].'), { type: 'step-finish', payload: { stepResult: { reason: 'stop' } } },
      { type: 'finish', payload: { stepResult: { reason: 'stop' } } },
    ].flatMap((c) => tr.translate(c));
    const text = out.filter((e) => e.type === 'text-delta').map((e: any) => e.delta);
    expect(text).toEqual(['Long reasoning before another tool. '.repeat(6), 'more', '\n\nV is 50% [1].']);
    expect(text.join('')).not.toContain('Let me check.');
    // stream ended without step-finish (abort): flush keeps the partial answer
    const tr2 = new StreamTranslator();
    expect(tr2.translate(td('partial'))).toEqual([]);
    expect(tr2.flush()).toEqual([{ type: 'text-delta', delta: 'partial' }]);
  });

  it('tags a sub-agent tool call with the delegation it belongs to', () => {
    const tr = new StreamTranslator();
    tr.translate({ type: 'tool-call', payload: { toolCallId: 'd1', toolName: 'agent-turnInvestigator', args: { prompt: 'p' } } });
    const step = (status: string) => ({ type: 'data-thinking-step',
      data: { step: 'get_turn', status, id: 'x', agent: 'turnInvestigator', input: {}, ok: true, summary: 's', citations: [] } });
    expect(tr.translate(step('in-progress'))[0]).toMatchObject({ type: 'tool-start', agent: 'turnInvestigator', subagent_id: 'd1' });
    expect(tr.translate(step('done'))[0]).toMatchObject({ type: 'tool-result', agent: 'turnInvestigator', subagent_id: 'd1' });
    tr.translate({ type: 'tool-result', payload: { toolCallId: 'd1', toolName: 'agent-turnInvestigator', result: { text: 'ok' } } });
    expect(tr.translate(step('done'))[0]).not.toHaveProperty('subagent_id');
  });

  it('reports the step cap like agent.py when the run ends on a tool-calls step', () => {
    const tr = new StreamTranslator(null, 8);
    expect(tr.translate({ type: 'finish', payload: { stepResult: { reason: 'tool-calls' } } }))
      .toEqual([{ type: 'error', message: 'step cap (8) reached before a final answer' }]);
    expect(tr.translate({ type: 'finish', payload: { stepResult: { reason: 'stop' } } })).toEqual([]);
  });

  it('a failed delegation becomes a subagent-result with a failure summary', () => {
    const tr = new StreamTranslator();
    expect(tr.translate({ type: 'tool-error', payload: { toolCallId: 'd', toolName: 'agent-turnInvestigator',
                                                         error: new Error('timeout') } }))
      .toEqual([{ type: 'subagent-result', id: 'd', name: 'turnInvestigator', summary: 'failed: timeout' }]);
  });
});

describe('message shaping (agent.py _to_api_messages)', () => {
  it('keeps user/assistant text, trims to user ends, appends dashboard context', () => {
    const m = toMessages({ messages: [{ role: 'assistant', content: 'a0' }, { role: 'system', content: 's' },
      { role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1' }, { role: 'user', content: 'q2' },
      { role: 'assistant', content: 'dangling' }] }, '2026-10-13', '2026-10-14');
    expect(m.map((x) => x.role)).toEqual(['user', 'assistant', 'user']);
    expect(m[2]!.content).toBe('q2\n\n<dashboard_context>day on screen: 2026-10-13; today: 2026-10-14</dashboard_context>');
    expect(toMessages({ messages: [] }, null, 'x')).toEqual([]);
  });
});

describe('(c) read-loop guard (unit)', () => {
  const inv = (toolName: string, args: unknown) => ({
    role: 'assistant', content: { format: 2, parts: [{ type: 'tool-invocation', toolInvocation: { state: 'result', toolName, args, result: {} } }] },
  });
  const user = { role: 'user', content: { format: 2, parts: [{ type: 'text', text: 'q' }] } };
  it('counts identical calls in the current turn only, key order insensitive', () => {
    const msgs: any[] = [user, inv('get_metric', { metric: 'V', day: 'd' }), user, inv('get_metric', { metric: 'V', day: 'd' })];
    expect(repeatedTools(msgs)).toEqual([]);
    msgs.push(inv('get_metric', { day: 'd', metric: 'V' }), inv('get_turn', { turn_id: 1 }));
    expect(repeatedTools(msgs)).toEqual(['get_metric']);
    expect(readLoopGuardStep({ messages: msgs, activeTools: undefined, tools: { get_metric: {}, get_turn: {}, 'agent-x': {} } } as any))
      .toEqual({ activeTools: ['get_turn', 'agent-x'] });
    expect(readLoopGuardStep({ messages: [user, inv('get_turn', { turn_id: 1 }), inv('get_turn', { turn_id: 2 })] } as any))
      .toBeUndefined();
  });
});

describe('trace rows', () => {
  it('keep ids, latency, model, usage; drop span input/output', () => {
    const row = spanRow({
      id: 's1', traceId: 't1', parentSpanId: 'p', type: 'model_generation', name: 'llm: claude', entityName: 'analyst',
      startTime: new Date(1000), endTime: new Date(1500), input: { messages: 'SECRET PROMPT' }, output: 'SECRET OUT',
      attributes: { model: 'claude-opus-5-5', provider: 'anthropic', usage: { inputTokens: 10, outputTokens: 2, inputDetails: { cacheRead: 5 } } },
    });
    expect(row).toMatchObject({ trace_id: 't1', span_id: 's1', parent_span_id: 'p', latency_ms: 500, model: 'claude-opus-5-5',
                                usage: { input: 10, output: 2, cached: 5 } });
    expect(JSON.stringify(row)).not.toContain('SECRET');
    expect(spanRow({ type: 'model_chunk' })).toBeNull();
  });
});
