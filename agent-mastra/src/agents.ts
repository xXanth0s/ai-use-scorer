import { createAnthropic } from '@ai-sdk/anthropic';
import { Agent } from '@mastra/core/agent';
import type { MastraModelConfig } from '@mastra/core/llm';

import { config } from './config';
import { makeHistoryLimiter } from './harness/history-limiter';
import { makeReadLoopGuardProcessor, type GuardTrip } from './harness/read-loop-guard';
import { getRun } from './lib/run-state';
import { scorerTools } from './tools';

const cached = { anthropic: { cacheControl: { type: 'ephemeral' as const } } };

/** Appended to the Python analyst prompt (fetched from /api/agent/system-prompt): the only part
 *  of the analyst's instructions that is Mastra-specific. */
export const DELEGATION_SECTION = `<delegation>
Two specialists can work for you; each returns a short brief whose [n] citation numbers are valid in your answer too (numbering is shared across this whole answer).
- agent-turnInvestigator: give it a set of turn ids (e.g. the worst evidence turns from get_metric) and the question; it reads each turn and reports what went wrong per turn, with citations. Use it when 2+ turns need a close look.
- agent-sqlAnalyst: give it one quantitative question ("share of edit batches with no test per repo this month"); it answers with SQL over the scorer DB.
Delegate when it saves you several tool calls; do simple lookups yourself. Pass the turn ids / day / metric ids the specialist needs in the prompt — it does not see your conversation's tool results.
</delegation>`;

export const TURN_INVESTIGATOR_PROMPT = `<identity>
You are the turn investigator for scorer, a tool that scores how one human drives coding agents (Claude Code, Codex). Principle: score the human's choices, not the agent.
</identity>

<job>
You receive a set of turn ids and a question (e.g. "why were these edit batches unverified?"). Inspect each turn and report, per turn, the concrete fact that answers the question: what the human asked (80-char excerpt), what the agent edited, whether and how it was verified (test/build after the edit, with result), interrupts, errors.
</job>

<tools>
- get_turn(turn_id): one turn in detail; the result carries its citation number n.
- get_metric(metric, day, scope_type): metric values plus evidence turns, each with n — use it only to resolve which turns a metric points at.
</tools>

<grounding>
Every fact must come from a tool result in this run. Cite each turn with its citation number in square brackets, e.g. "edited parse_claude.py, no test afterwards [3]". Use only the n values the tools gave you. Never quote prompts or commands beyond the excerpts the tools return.
</grounding>

<style>
One bullet per turn (≤ 25 words), then one line naming the common pattern, if any. No preamble, no headings.
</style>

<stop_conditions>
At most ${config.subagentMaxSteps} steps and at most ${config.toolBudgets.turnInvestigator?.get_turn} get_turn calls in total (pick the first ones if given more turn ids; further calls are refused), never the same call twice. Then answer.
</stop_conditions>`;

export const SQL_ANALYST_PROMPT = `<identity>
You are the SQL analyst for scorer, a tool that scores how one human drives coding agents. The data is a read-only SQLite DB of sessions, turns, tool calls, file edits and metric values.
</identity>

<job>
Answer one quantitative question with SQL and report the numbers.
</job>

<tools>
- query_db(sql, limit): single SELECT/WITH, ≤ 200 rows. Free-text columns are clipped to 80 chars on purpose. Include a turn_id column when rows are turns: each distinct turn_id gets a citation number (data.citations maps turn_id -> n).
Key tables: session(id, agent, repo), turn(id, session_id, idx, prompt_at, agent_finished_at, end_source), tool_call(id, turn_id, name, kind, bash_intent, initiator, is_error, has_result), file_edit, metric_value(run_id, metric, scope_type, scope_id, value, unit, n), metric_evidence(metric_value_id, turn_id, note), ingest_run(id). Always filter metric_value by run_id = (SELECT max(id) FROM ingest_run). Times are ISO-8601 UTC; for day questions prefer metric_value rows with scope_type='day' (scope_id = local YYYY-MM-DD).
</tools>

<grounding>
Every number must come from a query result in this run. Cite the turns behind a number with their [n]. If a query errors, fix it once; if the data is missing, say so.
</grounding>

<style>
The answer in one or two sentences with the numbers, then at most three bullets of supporting rows. No preamble.
</style>

<stop_conditions>
At most ${config.subagentMaxSteps} steps and at most ${config.toolBudgets.sqlAnalyst?.query_db} queries (further calls are refused); usually one or two. Never repeat an identical query.
</stop_conditions>`;

export type ScorerModels = {
  analyst: MastraModelConfig;
  fallback?: MastraModelConfig;
  turnInvestigator: MastraModelConfig;
  sqlAnalyst: MastraModelConfig;
};

export function anthropicModels(): ScorerModels {
  const anthropic = createAnthropic();
  return {
    analyst: anthropic(config.analystModel),
    fallback: config.analystModel === config.fallbackModel ? undefined : anthropic(config.fallbackModel),
    turnInvestigator: anthropic(config.investigatorModel),
    sqlAnalyst: anthropic(config.sqlModel),
  };
}

function processors(agent: string, onGuardTrip?: (t: GuardTrip) => void) {
  return [makeReadLoopGuardProcessor(agent, onGuardTrip), makeHistoryLimiter()];
}

const effort = { anthropic: { effort: config.effort } };
const subagentOptions = {
  maxSteps: config.subagentMaxSteps,
  modelSettings: { maxOutputTokens: config.subagentMaxOutputTokens },
  providerOptions: effort,
};

export function buildAgents(models: ScorerModels, opts: { onGuardTrip?: (t: GuardTrip) => void } = {}) {
  const turnInvestigator = new Agent({
    id: 'turn-investigator',
    name: 'turnInvestigator',
    description:
      'Investigates a set of turn ids (what was asked, edited, verified, interrupted) and returns per-turn findings with [n] citations.',
    model: models.turnInvestigator,
    instructions: { role: 'system', content: TURN_INVESTIGATOR_PROMPT, providerOptions: cached },
    tools: scorerTools('turnInvestigator', ['get_turn', 'get_metric'] as const),
    inputProcessors: processors('turnInvestigator', opts.onGuardTrip),
    defaultOptions: subagentOptions,
  });
  const sqlAnalyst = new Agent({
    id: 'sql-analyst',
    name: 'sqlAnalyst',
    description: 'Answers one quantitative question about the scorer DB with read-only SQL; cites turns with [n].',
    model: models.sqlAnalyst,
    instructions: { role: 'system', content: SQL_ANALYST_PROMPT, providerOptions: cached },
    tools: scorerTools('sqlAnalyst', ['query_db'] as const),
    inputProcessors: processors('sqlAnalyst', opts.onGuardTrip),
    defaultOptions: subagentOptions,
  });
  const analyst = new Agent({
    id: 'analyst',
    name: 'analyst',
    description: "Explains the human's scorer metrics with evidence turns; delegates turn investigations and SQL.",
    model: models.fallback
      ? [{ model: models.analyst, maxRetries: 1 }, { model: models.fallback, maxRetries: 1 }]
      : models.analyst,
    instructions: ({ requestContext }) => ({
      role: 'system',
      content: `${getRun({ requestContext })?.systemPrompt ?? ''}\n\n${DELEGATION_SECTION}`,
      providerOptions: cached,
    }),
    tools: scorerTools('analyst', ['query_db', 'get_turn', 'get_metric', 'list_sessions'] as const),
    agents: { turnInvestigator, sqlAnalyst },
    inputProcessors: processors('analyst', opts.onGuardTrip),
    defaultOptions: {
      maxSteps: config.analystMaxSteps,
      modelSettings: { maxOutputTokens: config.maxOutputTokens },
      providerOptions: effort,
    },
  });
  return { analyst, turnInvestigator, sqlAnalyst };
}
