import { MockLanguageModelV3, simulateReadableStream } from 'ai/test';

export type ScriptPart = { text: string } | { tool: string; input: Record<string, unknown> };
export type ScriptCall = { step: number; prompt: unknown[]; tools: string[]; system: string; providerOptions?: any; maxOutputTokens?: number };

/** A deterministic V3 model: `script` picks the parts for each call from the prompt it sees.
 *  step = number of tool-result messages already in the prompt (0 on the first call). */
export function scriptedModel(modelId: string, script: (c: ScriptCall) => ScriptPart[]) {
  let calls = 0;
  const seen: ScriptCall[] = [];
  const usage = { inputTokens: { total: 100, noCache: 100, cacheRead: 0, cacheWrite: 0 },
                  outputTokens: { total: 10, text: 10, reasoning: 0 } };
  const model = new MockLanguageModelV3({
    provider: 'mock',
    modelId,
    // non-streaming calls (agent.generate, e.g. the suggester): same script, one result
    doGenerate: async (options: any) => {
      const prompt = options.prompt as any[];
      const call: ScriptCall = {
        step: prompt.filter((m) => m.role === 'tool').length,
        prompt,
        tools: (options.tools ?? []).map((t: any) => t.name),
        system: prompt.filter((m) => m.role === 'system').map((m) => String(m.content)).join('\n'),
        providerOptions: options.providerOptions,
        maxOutputTokens: options.maxOutputTokens,
      };
      seen.push(call);
      const n = ++calls;
      const parts = script(call);
      const hasTool = parts.some((p) => 'tool' in p);
      return {
        content: parts.map((p, i) => ('text' in p ? { type: 'text' as const, text: p.text }
          : { type: 'tool-call' as const, toolCallId: `call-${modelId}-${n}-${i}`, toolName: p.tool, input: JSON.stringify(p.input) })),
        finishReason: { unified: hasTool ? 'tool-calls' : 'stop', raw: hasTool ? 'tool_use' : 'end_turn' },
        usage,
        response: { id: `resp-${modelId}-${n}`, modelId, timestamp: new Date(0) },
        warnings: [],
      } as any;
    },
    doStream: async (options: any) => {
      const prompt = options.prompt as any[];
      const call: ScriptCall = {
        step: prompt.filter((m) => m.role === 'tool').length,
        prompt,
        tools: (options.tools ?? []).map((t: any) => t.name),
        system: prompt.filter((m) => m.role === 'system').map((m) => String(m.content)).join('\n'),
        providerOptions: options.providerOptions,
        maxOutputTokens: options.maxOutputTokens,
      };
      seen.push(call);
      const parts = script(call);
      const n = ++calls;
      const chunks: any[] = [
        { type: 'stream-start', warnings: [] },
        { type: 'response-metadata', id: `resp-${modelId}-${n}`, modelId, timestamp: new Date(0) },
      ];
      let hasTool = false;
      parts.forEach((p, i) => {
        if ('text' in p) {
          chunks.push({ type: 'text-start', id: `t${i}` }, { type: 'text-delta', id: `t${i}`, delta: p.text },
            { type: 'text-end', id: `t${i}` });
        } else {
          hasTool = true;
          chunks.push({ type: 'tool-call', toolCallId: `call-${modelId}-${n}-${i}`, toolName: p.tool,
            input: JSON.stringify(p.input) });
        }
      });
      chunks.push({
        type: 'finish',
        finishReason: { unified: hasTool ? 'tool-calls' : 'stop', raw: hasTool ? 'tool_use' : 'end_turn' },
        usage: { inputTokens: { total: 100, noCache: 100, cacheRead: 0, cacheWrite: 0 },
                 outputTokens: { total: 10, text: 10, reasoning: 0 } },
      });
      return { stream: simulateReadableStream({ chunks }) };
    },
  });
  return Object.assign(model, { seen });
}
