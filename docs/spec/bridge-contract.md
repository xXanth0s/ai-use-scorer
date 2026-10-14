# Bridge contract — scorer harness → "how do you ai" (Ari's app)

One Python builder (`src/scorer/export_hdyai.py: build(conn, day, *, tz) -> Bundle`) produces the objects below.
Two writers consume the **same** objects:
- **local** (dev/verify loop, phase 1): upsert `sessions` + `reports` rows into `ari-version/.local/sessions.sqlite`.
- **hosted** (product): `scorer sync` → `/api/sync` (summaries) → `/api/analyze` (evidence sample + flat metrics) → poll `/api/analyses/:jobId` → `/api/sync` with `report` (Eve's content + our `scorerMetrics`).

Vocabulary: their **candidate** = our **human**; their **TaskSpan** = our **turn**; their `Session.id`/`nativeId` = our `session.id`.
Timezone: day boundaries use our config tz (unset → system tz = America/Los_Angeles = their `REPORT_TIMEZONE` default). The bundle carries `tz`.

## Ids

| Ours | Theirs | Rule |
|---|---|---|
| `session.id` (uuid / `demo-…`, never contains `:`) | `Session.id`, `Session.nativeId` | identical |
| `turn.id`, `turn.idx` | `Evidence.id` of kind `user` = `<session.id>:<idx*1000>` | **turn ref ↔ evidence id**: `line = turn.idx*1000`; reverse: `idx = line // 1000`, then `turn` by `(session_id, idx)` |
| `tool_call` k-th in turn (k = 1…) | `Evidence` kind `tool_call` = `<sid>:<idx*1000 + 2k-1>`, `tool_result` = `<sid>:<idx*1000 + 2k>` | tool_result only if `has_result`; `error = is_error`; text = `args_summary` (call), `"ok"`/`"error"` + duration (result). Max 499 tool calls per turn evidenced. |
| `metric_evidence` row → `metric_value.id` | `Evidence` kind **`metric`**, id `metric:<metric_value_id>` (one per metric_value; repeated turn refs go in text), `sessionId` = session of the first evidence turn | text: `"<metric> = <value> <unit> (n=<n>) · <note> · turns <turn refs as evidence ids>"`; lives in that session's `events[]`. metric_value rows whose evidence has no turn (git_change only) → attach to the day's session with most turns; documented in text. |
| `turn` | `TaskSpan {id: "<sid>:t<idx>", start: prompt_at, end: agent_finished_at, aborted: interrupted, reason: end_source}` | command turns (agent_started_at NULL) skipped |
| `api_call` | `TokenUsageRecord {id: request_id, timestamp: at, inputTokens: input_tok + cached_tok (+ cache_write if stored; theirs: cache is a SUBSET of input), cachedInputTokens: cached_tok, cacheWriteInputTokens, outputTokens: output_tok, totalTokens: input+output}` | check Codex semantics in `parse_codex.py` (Codex input may already include cached) |
| — | `finalOutputs` | **one per completed turn** `{id: "<sid>:t<idx>", timestamp: agent_finished_at}`; `finalOutputCountKnown: true`. (We store no `stop_reason`; documented approximation.) |

Local `Evidence.kind` gains `'metric'` (`lib/types.ts`), and hosted zod enums gain `'metric'` (`contract.ts` + `library.ts`).
Local `/api/reports/[id]` must resolve evidence by **scanning all snapshot events**, not `id.split(':')[0]`.

Text caps: one Evidence object is built **once** with `text ≤ 1800` chars (hosted zod cap; also the digest input) and reused byte-identical in `analyze.input` and `report.evidenceIndex`. Local SQLite snapshot may carry up to 12k (`truncated: true` when clipped); the hosted path always uses the 1800 version.

## Report ids

| Path | id | name | role |
|---|---|---|---|
| local, real DB | `scorer-<day>` | `scorer · <day>` | `self · <day>` |
| local, demo DB (`session.id LIKE 'demo-%'` or `demo_marker` event) | `scorer-demo-<day>` | `scorer demo · <day>` | `self · <day>` |
| local, candidate attempt (`event.kind='attempt'`) | `scorer-attempt-<candidate>-<day>` (one per attempt session) | `<candidate>` | `candidate · <challenge> · <day>` (role must contain the date; dashboard parses it) |
| hosted | `scorer-<day>-<8 hex of sha1(bundle json)>` = `requestId` | same | same |

Local re-run replaces the same ids. Hosted: complete reports are immutable, so identical data → same id (free re-sync, no new analysis); changed data → new id (costs 1 of 6/day).

## TypeScript (added to `ari-version/lib/types.ts`)

```ts
export type EvidenceKind = 'user' | 'assistant' | 'tool_call' | 'tool_result' | 'metric';
export type ScorerEvidence = { id: string; sessionId: string; excerpt: string };   // excerpt ≤ 320 chars
export type ScorerTile = {
  key: string;              // 'V' | 'B_min' | 'C' | 'H' | 'context' | 'cost'
  label: string;            // plain words, e.g. "Verified edit batches"
  value: number | null;     // null = not measured
  unit: 'share' | 'min' | 'count' | 'tokens_k' | 'usd';
  display: string;          // preformatted, e.g. "67%" / "235 min" / "$138.61"
  sub?: string;             // e.g. "asked by you 15%" / "p90 27k" / "4 user-preventable · 1 agent error"
  baseline?: number | null; // 7-day baseline when available
  evidence: ScorerEvidence[]; // self-contained turn links (≤ 8), work without evidenceIndex
};
export type ScorerMetrics = {
  day: string; tz: string; source: 'scorer'; demo: boolean;
  verdict: string;                        // our deterministic verdict line (report.facts().verdict.text)
  tiles: ScorerTile[];                    // order: V, B_min, C, H, context, cost
  breakdown: {
    cCauses: Record<string, number>;      // C:cause:* → count
    costByModel: { model: string; usd: number; tokens: number }[];
  };
  flat: Record<string, number | null>;    // exactly what /api/analyze gets as `metrics` (see below)
};
// Report (stored row) and ReportView gain:  scorerMetrics?: ScorerMetrics
// hosted publishedReport gains:            scorerMetrics: optional, moments stay inside content (Eve's)
```

Flat `metrics` record (hosted `/api/analyze`, keys ≤ 40 chars, values nonnegative or null), merged on top of their `analysisMetrics()` keys:
`V, V_asked_by_you, V_fail_share, V_final, B_min, C, C_cause_<cause>, H, H_repair, H_own, ctx_p50, ctx_p90, cost_usd, cost_usd_<model>, interrupts, thrown_min, active_h, overlap_pct` (model slug `[a-z0-9_]` truncated to fit 40).

## Report content (local path only; hosted content is Eve's)

`summary` = verdict line + one sentence of counts. `findings`: all 7 rubric dimensions, in rubric order:

| Dimension | Our metric | Rating (thresholds) | evidenceIds |
|---|---|---|---|
| Framing | C+ good_spec / scoped_turn episodes | ≥1 gated episode → Effective (High if ≥2) ; else Insufficient/Low | user evidence of episode turns |
| Context selection | — | Insufficient evidence / Low | [] |
| Iteration | C (corrections) | judged & n≥5: user-preventable share ≤0.25 → Effective, ≤0.5 → Needs support… see below; unjudged → Insufficient/Low | metric:<C id> + correction turns |
| Verification judgment | V | n<5 → Insufficient/Low; V ≥0.75 → Strongly demonstrated; ≥0.5 → Effective; else Needs support | metric:<V id> + unverified turns |
| Delegation and coordination | B_min | active<0.5 h → Insufficient/Low; B_min/active_min ≤0.05 → Effective; ≤0.15 → Needs support… (>0.15 Needs support) | metric:<B_min id> |
| Tool judgment | tooling_human_share | <5 tool events → Insufficient/Low; human share ≥0.1 → Effective; else Needs support | metric:<tooling id> |
| Outcome evidence | H + V_final | H measured & V_final ≥0.5 → Effective; V_final <0.5 → Needs support; H unmeasured → Insufficient/Low | metric:<H id> / metric:<V_final id> |

Iteration precise: preventable share p = user_preventable / max(1, labelled corrections); p ≤ 0.25 → Effective, else Needs support; no corrections but judged → Effective/Moderate.
Confidence: n ≥ 20 → High, n ≥ 5 → Moderate, else Low. Non-"Insufficient" findings must cite ≥1 id (their validator).

`moments` (3–5, from judge episodes + attribution): corrections with cause `agent_error` → `Agent`; user-preventable corrections → `Candidate`; positive episodes (good_spec, verification_catch, early_redirect, scoped_turn, knew_when_not_to_delegate) → `Candidate`; `unclear` → `Unknown`. `opportunities` = `facts().action_items` (≤4) with their turn ids. `limitations` = `["Judge labels are uncalibrated (κ not measured).", "Hand edits need git snapshots from hooks.", "Includes synthetic demo data." if demo]`. `followUps` = 3 fixed questions derived from the verdict category. `coverage` = one line per session.

## Example (abridged)

```json
{
  "id": "scorer-demo-2026-10-14", "status": "complete", "createdAt": "2026-10-14T23:00:00Z",
  "name": "scorer demo · 2026-10-14", "role": "self · 2026-10-14",
  "sessionIds": ["demo-a-opus-since"], "error": null, "coverage": ["…"],
  "snapshot": [{ "id": "demo-a-opus-since", "nativeId": "demo-a-opus-since", "agent": "claude",
    "events": [
      {"id": "demo-a-opus-since:3000", "sessionId": "demo-a-opus-since", "line": 3000, "timestamp": "…", "kind": "user", "text": "add --since to ingest"},
      {"id": "demo-a-opus-since:3001", "sessionId": "demo-a-opus-since", "line": 3001, "timestamp": "…", "kind": "tool_call", "tool": "Bash", "callId": "toolu_…", "text": "uv run pytest -q"},
      {"id": "metric:4711", "sessionId": "demo-a-opus-since", "line": 9000001, "timestamp": "…", "kind": "metric", "text": "V = 0.67 share (n=6) · …"}],
    "tasks": [{"id": "demo-a-opus-since:t3", "start": "…", "end": "…", "aborted": false, "reason": "turn_duration"}] }],
  "content": { "title": "…", "summary": "…", "findings": [{"dimension": "Verification judgment", "rating": "Effective", "confidence": "Moderate", "assessment": "…", "evidenceIds": ["metric:4711", "demo-a-opus-since:3000"]}], "moments": [], "opportunities": [], "limitations": [], "followUps": [] },
  "scorerMetrics": { "day": "2026-10-14", "tz": "America/Los_Angeles", "source": "scorer", "demo": true, "verdict": "…",
    "tiles": [{"key": "V", "label": "Verified edit batches", "value": 0.67, "unit": "share", "display": "67%", "sub": "asked by you 15%", "evidence": [{"id": "demo-a-opus-since:3000", "sessionId": "demo-a-opus-since", "excerpt": "add --since…"}]}],
    "breakdown": {"cCauses": {"instruction_violated": 2}, "costByModel": [{"model": "opus-5-5", "usd": 12.3, "tokens": 1200000}]},
    "flat": {"V": 0.67, "V_asked_by_you": 0.15, "B_min": 10.2} }
}
```
Metric evidence `line` = `9_000_000 + metric_value_id` (sorts after real events, unique).

## Analyst chat (Vercel-runnable)
`POST /api/analyst` (`app/api/analyst/route.ts`), body `{reportId, messages}` → ai SDK UI message stream (`@ai-sdk/anthropic`, `claude-opus-5-5`, key `ANTHROPIC_API_KEY`). Tools: `list_sessions`, `query_metrics`, `get_evidence(ids)`, all over the **report object** (local: `getReport` from SQLite; hosted: the published report via the analysis service). Citations = Evidence ids; clicking opens the evidence inspector. Python `/api/chat` (`scorer serve :8811`) stays dev-only; no proxy is built.
