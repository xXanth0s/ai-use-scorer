import { homedir } from 'node:os';
import { join } from 'node:path';

/** Everything the sidecar reads from the environment, in one place. */
export const config = {
  port: Number(process.env.PORT) || 4111,
  /** Where `scorer serve` runs; the proxy's X-Scorer-Url header overrides it per request. */
  scorerUrl: (process.env.SCORER_URL || 'http://127.0.0.1:8765').replace(/\/+$/, ''),
  scorerHome: process.env.SCORER_HOME || join(homedir(), '.scorer'),
  /** The Mastra analyst runs on Sonnet 5.5 (user decision); Opus 5.5 takes over on errors. The
   *  Python agent keeps config.toml analyst_model (Opus) independently. */
  analystModel: process.env.SCORER_ANALYST_MODEL || 'claude-sonnet-5-5',
  fallbackModel:
    process.env.SCORER_FALLBACK_MODEL ||
    ((process.env.SCORER_ANALYST_MODEL || 'claude-sonnet-5-5') === 'claude-opus-5-5' ? 'claude-sonnet-5-5' : 'claude-opus-5-5'),
  /** Quick-response tiles: one bounded post-answer call (Sherpa turn-chips). */
  suggestModel: process.env.SCORER_SUGGEST_MODEL || 'claude-haiku-5-5',
  suggestTimeoutMs: Number(process.env.SCORER_SUGGEST_TIMEOUT_MS) || 6_000,
  investigatorModel: process.env.SCORER_INVESTIGATOR_MODEL || 'claude-haiku-5-5',
  sqlModel: process.env.SCORER_SQL_MODEL || 'claude-sonnet-5-5',
  analystMaxSteps: 8,
  subagentMaxSteps: 5,
  maxOutputTokens: 4000,
  /** Sub-agent briefs were cut off at 4000 in the first live smoke. */
  subagentMaxOutputTokens: 8000,
  /** agent.py EFFORT: sent as Anthropic output_config.effort by @ai-sdk/anthropic. */
  effort: 'low' as const,
  /** Hard per-turn call budgets for sub-agent tools; calls past it return an error result.
   *  (A step cap bounds steps, not fan-out: the investigator once issued 12 get_turns in one step.) */
  toolBudgets: {
    turnInvestigator: { get_turn: 4 },
    sqlAnalyst: { query_db: 3 },
  } as Record<string, Record<string, number>>,
  historyTokenLimit: Number(process.env.SCORER_HISTORY_TOKEN_LIMIT) || 100_000,
  toolTimeoutMs: 15_000,
};

export const CLIP = 80;

/** model.clip: collapse whitespace, cut to n chars with an ellipsis. */
export function clip(s: unknown, n = CLIP): string {
  if (s === null || s === undefined) return '';
  const t = String(s).replace(/\s+/g, ' ').trim();
  return t.length <= n ? t : `${t.slice(0, n - 1)}…`;
}
