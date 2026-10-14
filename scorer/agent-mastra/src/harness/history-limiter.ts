import { TokenLimiterProcessor } from '@mastra/core/processors';

import { config } from '../config';

/**
 * Token-budget bound on replayed context (system + history), re-checked at every step — Sherpa's
 * harness/history-limiter.ts without the memory-thread anchor logic (the conversation lives in the
 * dashboard and arrives whole with each request). 'contiguous' so the newest turn always survives
 * and only the oldest history is dropped.
 */
export function makeHistoryLimiter(limit = config.historyTokenLimit): TokenLimiterProcessor {
  return new TokenLimiterProcessor({ limit, trimMode: 'contiguous' });
}
