import { homedir } from 'node:os';
import { join } from 'node:path';

/** Everything the sidecar reads from the environment, in one place. */
export const config = {
  port: Number(process.env.PORT) || 4111,
  /** Where `scorer serve` runs; the proxy's X-Scorer-Url header overrides it per request. */
  scorerUrl: (process.env.SCORER_URL || 'http://127.0.0.1:8765').replace(/\/+$/, ''),
  scorerHome: process.env.SCORER_HOME || join(homedir(), '.scorer'),
  analystModel: process.env.SCORER_ANALYST_MODEL || 'claude-opus-5-5',
  fallbackModel: 'claude-sonnet-5-5',
  investigatorModel: process.env.SCORER_INVESTIGATOR_MODEL || 'claude-haiku-5-5',
  sqlModel: process.env.SCORER_SQL_MODEL || 'claude-sonnet-5-5',
  analystMaxSteps: 8,
  subagentMaxSteps: 5,
  maxOutputTokens: 4000,
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
