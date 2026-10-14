import { config } from '../config';

/** The four Python tools. Python owns the data: read-only SQLite, 80-char clipping, citations. */
export const TOOL_NAMES = ['query_db', 'get_turn', 'get_metric', 'list_sessions'] as const;
export type ToolName = (typeof TOOL_NAMES)[number];

export type Citation = { n: number; turn_id: number | null; session_id: string | null; label: string };

/** agent.run_tool's return shape. */
export type ToolResult = {
  ok: boolean;
  data?: unknown;
  error?: string;
  available?: unknown;
  days_with_values?: unknown;
  summary: string;
  citations: Citation[];
};

async function fetchJson(url: string, init: RequestInit, timeoutMs: number): Promise<unknown> {
  const res = await fetch(url, { ...init, signal: AbortSignal.timeout(timeoutMs) });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = (body as { error?: string } | null)?.error ?? `HTTP ${res.status}`;
    throw new Error(err);
  }
  return body;
}

/** POST <scorer>/api/tools/<name>. Never throws: transport failures come back as {ok:false, error}. */
export async function callTool(
  scorerUrl: string,
  name: ToolName,
  input: Record<string, unknown>,
  opts: { n0: number; day?: string | null },
): Promise<ToolResult> {
  try {
    const body = await fetchJson(
      `${scorerUrl}/api/tools/${name}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input, n0: opts.n0, day: opts.day ?? null }),
      },
      config.toolTimeoutMs,
    );
    const r = body as ToolResult;
    return { ...r, citations: Array.isArray(r.citations) ? r.citations : [] };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return { ok: false, error: `scorer tool endpoint failed: ${msg}`, summary: `error: ${msg}`.slice(0, 100), citations: [] };
  }
}

export type SystemPromptInfo = { system_prompt: string; today: string; max_steps: number };

/** GET <scorer>/api/agent/system-prompt — the Python analyst's prompt, so both backends define
 *  metrics identically. */
export async function fetchSystemPrompt(scorerUrl: string): Promise<SystemPromptInfo> {
  return (await fetchJson(`${scorerUrl}/api/agent/system-prompt`, {}, 5_000)) as SystemPromptInfo;
}
