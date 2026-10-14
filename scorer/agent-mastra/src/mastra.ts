import { Mastra } from '@mastra/core/mastra';
import { Observability } from '@mastra/observability';

import { anthropicModels, buildAgents, type ScorerModels } from './agents';
import type { GuardTrip } from './harness/read-loop-guard';
import { JsonlTraceExporter } from './harness/trace-exporter';

export type ScorerMastra = ReturnType<typeof createScorerMastra>;

export function createScorerMastra(opts: { models?: ScorerModels; traceDir?: string; onGuardTrip?: (t: GuardTrip) => void } = {}) {
  const agents = buildAgents(opts.models ?? anthropicModels(), { onGuardTrip: opts.onGuardTrip });
  const exporter = new JsonlTraceExporter(opts.traceDir);
  const mastra = new Mastra({
    agents,
    observability: new Observability({
      configs: { default: { serviceName: 'scorer-agent-mastra', exporters: [exporter] } },
    }),
  });
  return { mastra, exporter };
}
