import { streamText, convertToModelMessages, stepCountIs, type UIMessage } from 'ai';
import { createAnthropic } from '@ai-sdk/anthropic';
import { getReport } from '@/lib/db';
import { assertLocal, failure } from '@/lib/http';
import { hostedRequest, isHosted } from '@/lib/hosted';
import { DEMO_MAX_OUTPUT_TOKENS, DEMO_MAX_USER_MESSAGES, DEMO_REPORTS, demoRateLimited, isDemo } from '@/lib/demo';
import { ANALYST_SYSTEM, analystTools, fromLocalReport, fromPublishedReport, type AnalystSource } from '@/lib/analyst';

export const maxDuration = 60;
const MODEL = 'claude-opus-5-5';

async function load(request: Request, reportId: string): Promise<AnalystSource | null> {
  if (isHosted()) {
    // Same authenticated device path as the hosted report view; no database credential lives in the web project.
    const published = await hostedRequest(request, `/api/published/${encodeURIComponent(reportId)}`);
    return published ? fromPublishedReport(published) : null;
  }
  assertLocal(request);
  const report = getReport(reportId);
  return report ? fromLocalReport(report) : null;
}

export async function POST(request: Request) {
  try {
    const key = process.env.ANTHROPIC_API_KEY;
    if (!key) return Response.json({ error: 'The analyst is not configured (ANTHROPIC_API_KEY missing).' }, { status: 503 });
    const body = await request.json().catch(() => null) as { reportId?: unknown; messages?: unknown } | null;
    const reportId = typeof body?.reportId === 'string' && /^[A-Za-z0-9:_-]{1,180}$/.test(body.reportId) ? body.reportId : null;
    if (!reportId || !Array.isArray(body?.messages) || !body.messages.length || body.messages.length > 40) return failure(new Error('Send a reportId and 1–40 messages.'));
    if (isDemo()) {
      // Public demo spends a real key: bundled reports only, short conversations, per-IP hourly cap.
      if (!DEMO_REPORTS.has(reportId)) return failure(new Error('Report not found.'), 404);
      const userTurns = (body.messages as { role?: unknown }[]).filter(m => m?.role === 'user').length;
      if (userTurns > DEMO_MAX_USER_MESSAGES) return failure(new Error(`The demo allows ${DEMO_MAX_USER_MESSAGES} questions per conversation. Reload to start over.`), 429);
      const ip = request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() || request.headers.get('x-real-ip') || 'unknown';
      if (demoRateLimited(ip)) return failure(new Error('Demo question limit reached for this hour. Try again later.'), 429);
    }
    let src: AnalystSource | null;
    try { src = await load(request, reportId); } catch (e) {
      const status = (e as { status?: number }).status;
      return failure(e, typeof status === 'number' ? status : 400);
    }
    if (!src) return failure(new Error('Report not found.'), 404);
    const tools = analystTools(src);
    const result = streamText({
      model: createAnthropic({ apiKey: key })(MODEL),
      system: ANALYST_SYSTEM,
      messages: await convertToModelMessages(body.messages as UIMessage[], { ignoreIncompleteToolCalls: true }),
      tools,
      stopWhen: stepCountIs(6),
      maxOutputTokens: isDemo() ? DEMO_MAX_OUTPUT_TOKENS : 1500,
    });
    return result.toUIMessageStreamResponse({ onError: e => (e instanceof Error ? e.message : 'The analyst failed.') });
  } catch (e) { return failure(e); }
}
