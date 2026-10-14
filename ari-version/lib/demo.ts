// Read-only public demo (HDYAI_DEMO=1): bundled synthetic reports, no collection, no writes.
export const isDemo = () => process.env.HDYAI_DEMO === '1';
export const DEMO_HOME = '/reports/scorer-demo-2026-10-14';
export const DEMO_REPORTS = new Set(['scorer-demo-2026-10-14', 'scorer-attempt-cand-a-2026-10-13', 'scorer-attempt-cand-b-2026-10-13']);
// API paths the demo serves; everything else under /api answers 403.
export const DEMO_API = [/^\/api\/reports(\/[^/]+)?$/, /^\/api\/sessions$/, /^\/api\/evidence$/, /^\/api\/analyst$/, /^\/api\/(status|settings|analysis-connection)$/];
export const demoReadOnly = () => Response.json({ error: 'This demo is read-only.' }, { status: 403 });

// Analyst spend caps for a public URL: per-IP requests per hour + per-conversation user turns.
const hits = new Map<string, number[]>();
export const DEMO_MAX_USER_MESSAGES = 6;
export const DEMO_MAX_OUTPUT_TOKENS = 1200;
export function demoRateLimited(ip: string, limit = 20, windowMs = 3_600_000): boolean {
  const now = Date.now();
  const recent = (hits.get(ip) ?? []).filter(t => now - t < windowMs);
  if (recent.length >= limit) { hits.set(ip, recent); return true; }
  recent.push(now); hits.set(ip, recent);
  return false;
}
