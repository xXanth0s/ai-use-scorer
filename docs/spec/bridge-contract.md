# Bridge contract — scorer harness → "how do you ai" (Ari's app)

One Python builder (`src/scorer/export_hdyai.py: build(conn, day, *, tz) -> Bundle`) produces the objects below.
Two writers consume the **same** objects:
- **local** (dev/verify loop, phase 1): upsert `sessions` + `reports` rows into `ari-version/.local/sessions.sqlite`.
- **hosted** (product): `scorer sync` → `/api/sync` (summaries) → `/api/analyze` (evidence sample + flat metrics) → poll `/api/analyses/:jobId` → `/api/sync` with `report` (Eve's content + our `scorerMetrics`).

Vocabulary: their **candidate** = our **human**; their **TaskSpan** = our **turn**; their `Session.nativeId` = our `session.id`, their `Session.id` = hash (below).
Timezone: day assignment = `model.local_day` (config tz + 04:00 day cutoff), identical to `facts(day)`. `scorerMetrics.tz` = the tz actually used, printed by `export-hdyai` (here America/Los_Angeles: config tz unset → system tz). The app's `REPORT_TIMEZONE` may differ (Europe/Berlin); our day stays ours.

## Ids

| Ours | Theirs | Rule |
|---|---|---|
| `session.agent` + `session.transcript_path` | `Session.id` = `sha256(f"{agent}:{transcript_path}").hexdigest()[:24]` (their parser rule; NULL path → hash `demo:<session.id>`) | **join on this id, never on nativeId** |
| `session.id` | `Session.nativeId`; `path` = transcript_path | `parentId` = derived id of `session.forked_from` (Codex subagent threads); parent kept, never deduped |
| `(session.id, turn.idx)` (`turn.id` is a per-ingest rowid: never exported) | `Evidence` kind `user` = `<hash>:<idx*1000>` | **turn ref ↔ evidence id**: `line = idx*1000`; reverse: `idx = line // 1000`, turn by `(session, idx)` |
| `tool_call` k-th in turn (k = 1…) | `Evidence` kind `tool_call` = `<hash>:<idx*1000 + 2k-1>`, `tool_result` = `<hash>:<idx*1000 + 2k>` | tool_result only if `has_result`; `error = is_error`; text = `args_summary` (call), `"ok"`/`"error"` + duration (result); `callId = tool_use_id`. Max 499 per turn. |
| day metric (name) | `Evidence` kind **`metric`**, id `metric:<day>:<slug>` (slug = name with `[^A-Za-z0-9_-]` → `_`, e.g. `metric:2026-10-14:C_cause_info_missing`), `line = 9_000_000 + index in the sorted list of the day's metric names`, `sessionId` = hash of the first evidence turn's session | text: `"<metric> = <value> <unit> (n=<n>) · <notes> · turns <evidence ids>"`. Emitted for every metric with `metric_evidence` rows plus those cited by findings/tiles. No turn in this report → busiest session, said in text. `metric_value.id` never exported. |
| `turn` | `TaskSpan {id, start: prompt_at, end: agent_finished_at, aborted, reason: end_source}`; id = Codex `turn.first_uuid`, Claude `<our session.id>:<idx>` | aborted = `interrupt` event on the turn; `queued` turns fold into the open task; `command` turns skipped |
| `api_call` | `TokenUsageRecord {id: request_id, timestamp: at, inputTokens: input_tok + cached_tok, cachedInputTokens: cached_tok, cacheWriteInputTokens: 0, outputTokens: output_tok, totalTokens: input_tok + cached_tok + output_tok}` | our `input_tok` already includes cache_write (Claude) and excludes cached (both CLIs) |
| — | `finalOutputs: []`, `finalOutputCountKnown: false` | we store no stop_reason: never derive tokensPerFinalOutput from our data |

Local `Evidence.kind` gains `'metric'` (`lib/types.ts`). The hosted path never sends kind `'metric'` (see Hosted).
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
  key: string;              // tiles: V | C | episodes | H | B_min; leverage: see `leverage` below
  label: string;            // plain words, e.g. "Verified edit batches"
  value: number | null;     // null = not measured (context as a fraction 0..1)
  unit: 'share' | 'min' | 'count' | 'tokens_k' | 'usd';
  display: string;          // preformatted, e.g. "67%" / "47 min" / "$138.61" / "not measured"
  sub?: string;             // V: "asked by you 10% · 52 unverified"; C: "by cause: instruction violated 4 · …";
                            // episodes: episode titles; H: "repair 2 · own 1"; context: "p90 28%"; cost: per model
  baseline?: number | null; // 7-day baseline when available
  howMeasured?: string;     // one plain sentence (≤ 240 chars), set on every tile and leverage tile
  evidence: ScorerEvidence[]; // self-contained turn links (≤ 8); [] when the number has no turn refs
};
export type ScorerLane = { sessionId: string; agent: 'claude' | 'codex'; repo: string | null; branch: string | null; cwd: string };
export type ScorerInterrupt = { sessionId: string; at: string; evidenceId: string }; // evidenceId = user Evidence of the interrupted turn
export type ScorerMetrics = {
  day: string; tz: string; source: 'scorer'; demo: boolean;
  verdict: string;                        // our deterministic verdict line (report.facts().verdict.text)
  tiles: ScorerTile[];                    // scores, order: V, C, episodes, H, B_min
  leverage: ScorerTile[];                 // diagnostics (not scored), order: tooling_skill, tooling_command, tooling_mcp,
                                          // tooling_plugin (sub 'by you N · by agent N · default N'), top_tools (top 5 named),
                                          // subagents (sub models; depth not stored → omitted), model_mix (share of requests),
                                          // parallel (overlap_s/active_s), interrupts (sub thrown min), gaps (median; buckets),
                                          // context (p50; sub p90), tokens (total; in/out/cached), cost (per model)
  breakdown: {
    cCauses: Record<string, number>;      // corrections by cause (C:cause:*)
    costByModel: { model: string; usd: number; tokens: number }[];
  };
  flat: Record<string, number | null>;    // our numbers; hosted sends them as `scorer_<key>`
  lanes: ScorerLane[];                    // one per snapshot session ("Day overview" timeline)
  interrupts: ScorerInterrupt[];
};
// Report (stored row) and ReportView gain:  scorerMetrics?: ScorerMetrics
// hosted publishedReport: scorerMetrics included but optional (an old service strips it); content is Eve's
```

## Hosted ("A-lite": works against the current, un-redeployed analysis service)

`scorer sync --day D [--db] [--url] [--token] [--dry-run FILE]`, `scorer sync --activate invite_…` (token → `SCORER_HOME/hdyai.json`, 0600).
1. `POST /api/sync {sessions: summaries (with our TaskSpans as tasks + tokenUsage), reportName}`.
2. `POST /api/analyze` (`analysisRequest`, strict): `requestId` = hosted id; evidence kinds **only** user/tool_call/tool_result (never `metric`), text ≤ 1800, sampled so the JSON stays < 115k chars; `metrics` = their `metrics()` keys (ported from `lib/metrics.ts`) plus ours as `scorer_<key>` (≤ 40 chars, truncated, nonnegative or null):
   `scorer_V, scorer_V_asked_by_you, scorer_V_fail_share, scorer_V_final, scorer_B_min, scorer_C, scorer_C_cause_<cause>, scorer_H, scorer_H_repair, scorer_H_own, scorer_ctx_p50, scorer_ctx_p90, scorer_cost_usd, scorer_cost_usd_<model>, scorer_interrupts, scorer_thrown_min, scorer_active_h, scorer_overlap_pct`.
3. Poll `GET /api/analyses/:jobId` (every 5 s, ≤ 300 s; the service fails jobs after 240 s).
4. `POST /api/sync {report}`: `content` = Eve's job content byte-identical; `evidenceIndex` = cited ids only, the identical Evidence objects sent to analyze (+ sessionTitle, agent); `metrics`, `timezone`, `activity`; `scorerMetrics` included (stripped by an old service).
Our own findings/content exist **only** in the local Report row.

## Report content (local path only; hosted content is Eve's)

`summary` = verdict line + one sentence of counts. `findings`: all 7 rubric dimensions, in rubric order:

| Dimension | Our metric | Rating (thresholds) | evidenceIds |
|---|---|---|---|
| Framing | C+ good_spec / scoped_turn episodes | ≥1 gated episode → Effective (High if ≥2) ; else Insufficient/Low | user evidence of episode turns |
| Context selection | — | Insufficient evidence / Low | [] |
| Iteration | C (corrections) | judged & n≥5: user-preventable share ≤0.25 → Effective, ≤0.5 → Needs support… see below; unjudged → Insufficient/Low | metric:<day>:C + correction turns |
| Verification judgment | V | n<5 → Insufficient/Low; V ≥0.75 → Strongly demonstrated; ≥0.5 → Effective; else Needs support | metric:<day>:V + unverified turns |
| Delegation and coordination | B_min | active<0.5 h → Insufficient/Low; B_min/active_min ≤0.05 → Effective; ≤0.15 → Needs support… (>0.15 Needs support) | metric:<day>:B_min |
| Tool judgment | tooling_human_share | <5 tool events → Insufficient/Low; human share ≥0.1 → Effective; else Needs support | metric:<day>:tooling_human_share |
| Outcome evidence | H + V_final | H measured & V_final ≥0.5 → Effective; V_final <0.5 → Needs support; H unmeasured → Insufficient/Low | metric:<day>:H / metric:<day>:V_final |

Iteration precise: preventable share p = user_preventable / max(1, labelled corrections); p ≤ 0.25 → Effective, else Needs support; no corrections but judged → Effective/Moderate.
Confidence: n ≥ 20 → High, n ≥ 5 → Moderate, else Low. Non-"Insufficient" findings must cite ≥1 id (their validator).

`moments` (3–5, from judge episodes + attribution): corrections with cause `agent_error` → `Agent`; user-preventable corrections → `Candidate`; positive episodes (good_spec, verification_catch, early_redirect, scoped_turn, knew_when_not_to_delegate) → `Candidate`; `unclear` → `Unknown`. `opportunities` = `facts().action_items` (≤4) with their turn ids. `limitations` = `["Judge labels are uncalibrated (κ not measured).", "Hand edits need git snapshots from hooks.", "Includes synthetic demo data." if demo]`. `followUps` = 3 fixed questions derived from the verdict category. `coverage` = one line per session.

## Example (abridged)

```json
{
  "id": "scorer-demo-2026-10-14", "status": "complete", "createdAt": "2026-10-14T23:00:00Z",
  "name": "scorer demo · 2026-10-14", "role": "self · 2026-10-14",
  "sessionIds": ["35c237b1347a7615a6b327f3"], "error": null, "coverage": ["…"],
  "snapshot": [{ "id": "35c237b1347a7615a6b327f3", "nativeId": "demo-a-opus-since", "agent": "claude",
    "events": [
      {"id": "35c237b1347a7615a6b327f3:3000", "sessionId": "35c237b1347a7615a6b327f3", "line": 3000, "timestamp": "…", "kind": "user", "text": "add --since to ingest"},
      {"id": "35c237b1347a7615a6b327f3:3001", "sessionId": "35c237b1347a7615a6b327f3", "line": 3001, "timestamp": "…", "kind": "tool_call", "tool": "Bash", "callId": "toolu_…", "text": "uv run pytest -q"},
      {"id": "metric:2026-10-14:V", "sessionId": "35c237b1347a7615a6b327f3", "line": 9000001, "timestamp": "…", "kind": "metric", "text": "V = 0.67 share (n=6) · …"}],
    "tasks": [{"id": "demo-a-opus-since:3", "start": "…", "end": "…", "aborted": false, "reason": "turn_duration"}] }],
  "content": { "title": "…", "summary": "…", "findings": [{"dimension": "Verification judgment", "rating": "Effective", "confidence": "Moderate", "assessment": "…", "evidenceIds": ["metric:2026-10-14:V", "35c237b1347a7615a6b327f3:3000"]}], "moments": [], "opportunities": [], "limitations": [], "followUps": [] },
  "scorerMetrics": { "day": "2026-10-14", "tz": "America/Los_Angeles", "source": "scorer", "demo": true, "verdict": "…",
    "tiles": [{"key": "V", "label": "Verified edit batches", "value": 0.67, "unit": "share", "display": "67%", "sub": "asked by you 15%", "evidence": [{"id": "35c237b1347a7615a6b327f3:3000", "sessionId": "35c237b1347a7615a6b327f3", "excerpt": "add --since…"}]}],
    "breakdown": {"cCauses": {"instruction_violated": 2}, "costByModel": [{"model": "opus-5-5", "usd": 12.3, "tokens": 1200000}]},
    "flat": {"V": 0.67, "V_asked_by_you": 0.15, "B_min": 10.2} }
}
```
Metric evidence `line` = `9_000_000 + index in the sorted day metric names` (sorts after real events, stable across ingests).

## Analyst chat (Vercel-runnable)
`POST /api/analyst` (`app/api/analyst/route.ts`), body `{reportId, messages}` → ai SDK UI message stream (`@ai-sdk/anthropic`, `claude-opus-5-5`, key `ANTHROPIC_API_KEY`). Tools: `list_sessions`, `query_metrics`, `get_evidence(ids)`, all over the **report object** (local: `getReport` from SQLite; hosted: the published report via the analysis service). Citations = Evidence ids; clicking opens the evidence inspector. Python `/api/chat` (`scorer serve :8811`) stays dev-only; no proxy is built.
