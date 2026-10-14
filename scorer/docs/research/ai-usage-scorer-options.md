# AI Usage Scorer — research & options

*Hackathon research doc, 2026-10-14. Goal: measure how well a person uses coding agents (Claude Code + Codex first, any agent later), produce an end-of-day report, and later reuse the same pipeline to assess candidates on a coding challenge.*

**One framing rule that shapes everything below:** the tool scores **the human's choices**, not the agent's quality. Every metric should attribute to something the person did — how they prompted, whether they verified, when they intervened, what they delegated. "The model was slow" is not a finding; "you waited 11 min for a run you then discarded" is.

---

## 1. What data already exists on disk (verified on this machine)

Both agents write full session transcripts locally. **No proxy, no API keys, no instrumentation of the agent itself is needed** — a collector that reads these files gets ~90% of the signal.

### Claude Code — `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`

One JSON object per line, forming a tree (`uuid` / `parentUuid`). Every entry has `timestamp` (ISO UTC), `sessionId`, `cwd`, `gitBranch`, `version`.

| Entry type | What it gives us |
|---|---|
| `user` | Prompt text, `promptId`, `permissionMode`, `isCompactSummary`, `interruptedMessageId`, and `tool_result` blocks (with `is_error`) paired to tool calls via `tool_use_id`; structured `toolUseResult` (Bash: stdout/stderr/`interrupted`; Edit: `filePath`, `structuredPatch`, `userModified`) |
| `assistant` | `message.model`, `stop_reason`, `usage` (input/output/cache tokens, `thinking_tokens`), `requestId`, `effort`, `tool_use` blocks (name + input). ⚠️ one API response = several entries that repeat `usage` → **dedupe per `requestId`** |
| `system/turn_duration` | `durationMs`, `messageCount` per turn |
| `system/compact_boundary` | compaction events: `preTokens`, `postTokens`, trigger |
| `system/stop_hook_summary` | hook timings |
| `cost-state` | running per-session: `totalCostUSD`, `totalAPIDuration`, `totalToolDuration`, `totalLinesAdded/Removed`, per-model usage → take the last one |
| `file-history-snapshot` / `-delta` | which files the agent touched and when (backups) |
| `pr-link` | PR number/url created from the session |
| `queue-operation` | prompts queued while the agent was still running |
| `mode`, `permission-mode` | plan/normal, auto/default etc. |
| `attachment` | hook outputs, skill listings, plan-mode exits |

Also: `"[Request interrupted by user]"` text marks interruptions; subagents live in `<sessionId>/subagents/agent-<id>.jsonl` + `.meta.json` (agentType, model); `~/.claude/history.jsonl` is a flat cross-project prompt log (`display`, `timestamp`, `project`, `sessionId`); `~/.claude/stats-cache.json` holds daily message/session/tool counts (stale).

**Hooks** (`~/.claude/settings.json`): `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `Stop`, `SubagentStop`, `PreCompact`, `Notification`. Every hook gets `session_id`, `transcript_path`, `cwd` *(payload fields per docs/recall, not observed on disk — verify against the official hooks reference before building on them)*. Claude Code can also export **OpenTelemetry** events (`claude_code.user_prompt`, `claude_code.tool_result`, cost/token metrics), with prompt content redacted by default ([Last9 guide](https://last9.io/docs/integrations/claude-code/), [Elastic integration](https://www.elastic.co/docs/reference/integrations/claude_code)).

### Codex CLI — `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl`

Also one JSON per line; each line has `timestamp`, `ordinal`, `type`, `payload`.

| `type` / `payload.type` | What it gives us |
|---|---|
| `session_meta` | `session_id`, `cwd`, `git` (branch/commit), `cli_version`, `model_provider`, `originator` |
| `turn_context` | per turn: `model`, `effort`, `approval_policy`, `sandbox_policy`, `collaboration_mode` |
| `event_msg/task_started` + `task_complete` | turn boundaries, `turn_id`, `model_context_window`, `last_agent_message` |
| `event_msg/token_count` | cumulative `input/cached/output/reasoning_tokens`, rate limits |
| `event_msg/item_completed` | user messages, agent messages, with `completed_at_ms` |
| `response_item/message` | role (`user`/`developer`/`assistant`) + content |
| `response_item/function_call` + `function_call_output` | tool calls (`exec_command` observed; others e.g. `apply_patch` per docs) paired by `call_id`; outputs include wall time + exit code |
| `response_item/reasoning` | encrypted; only `summary` is readable |

Extras: `~/.codex/session_index.jsonl` (id, thread name, updated_at), `~/.codex/history.jsonl` (prompt log), **`hooks.json` with the same event model as Claude Code** (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, `PreCompact`, `PostCompact`, `Stop`, `SubagentStart/Stop`) — [official docs](https://developers.openai.com/codex/hooks). Codex also already *imports* Claude Code transcripts (`external_agent_session_imports.json`), which proves cross-agent normalisation is tractable.

**Implication:** the two formats differ in shape but map onto the same ~8 concepts (session, turn, prompt, model response, tool call, tool result, token usage, interruption). That's the normalised event model (§4).

### Missing from transcripts — needs git

Transcripts tell you what the agent *wrote*, not what *survived*. For rework/waste you need the working repo's git data: commits, diffs, reverts, test runs. Cheap to get (`git log --since`, `git diff`) and correlatable to agent edits by file path + timestamp. Without it there is no ground truth for "good".

---

## 2. Metrics catalog

Grouped by the question the daily report answers. ★ = computable from transcripts alone today; ◆ = needs git; ○ = needs OS-level activity tracking (out of scope v1).

### A. Where did the time go? (waste)
- ★ **Wall time per session / per day**, split into: agent running (API + tool time), human typing, **idle wait** (agent done → next prompt gap), **abandoned waits** (long agent run followed by interruption or no follow-up)
- ★ **Interruptions** (`[Request interrupted by user]`, Bash `interrupted`) — count, and how much agent time was thrown away each time
- ★ **Failing tool loops**: ≥3 consecutive `is_error` results on the same tool / same command
- ★ **Compactions per session** and context size at compaction — proxy for "session ran too long"
- ◆ **Sessions with zero kept changes** (no commit / diff touching agent-edited files)
- ○ Time in editor/browser between prompts

### B. How much rework did you cause?
- ★ **Re-prompts on the same topic**: consecutive prompts with high text similarity, or a prompt that starts with "no", "wrong", "actually", "instead"
- ★ **Correction ratio**: share of prompts that are corrections vs. new tasks (LLM-classified or heuristic)
- ◆ **Agent edits later reverted**: lines written by the agent in session that don't appear in the next commit, or are changed again within N hours
- ◆ **Churn**: same file edited by agent in ≥3 separate turns of one session
- ★ **Edit `userModified`** flag — human hand-fixed the agent's edit before accepting

### C. Did you verify before trusting?
- ★ **Verification rate**: share of agent edit batches followed (within the session) by a test/lint/typecheck Bash call or a `Read` of the diff
- ★ **Blind-accept streaks**: longest run of edits accepted with no verifying action in between
- ★ **Permission-mode profile**: time spent in `auto`/bypass vs. default — not "bad", but context for the above
- ◆ Test failures in CI/commit right after agent edits

### D. How well did you steer?
- ★ **Prompt quality proxies**: prompt length, presence of acceptance criteria / file references / constraints; first-prompt-of-task length vs. number of follow-ups needed
- ★ **Clarification rounds** before the agent starts producing edits
- ★ **Turns per completed task** (task boundary ≈ commit, PR link, or long idle gap)
- ★ **Use of plan mode** before big edits; **spec-first vs. dive-in** ratio

### E. Did you use leverage?
- ★ Subagents spawned, parallel sessions, skills/slash commands used, MCP tools used
- ★ **Model/effort choice** vs. task size (opus+high for a one-line fix is waste)
- ★ Cache hit ratio (cache_read / total input) — proxy for keeping sessions coherent

### F. Cost & efficiency (normalisers, not scores)
- ★ Tokens and $ per session / day / model (Claude: `cost-state`; Codex: `token_count` × price table)
- ◆ **$ and minutes per kept change** (per commit, per merged PR) — the single best efficiency number
- ★ Output tokens per edit — chatty vs. terse sessions

### Candidate-assessment subset
For comparability across candidates, use metrics that are **task-invariant** and reflect skill: B (rework), C (verification), D (steering), plus "time to first kept change" and the final outcome (tests pass / task complete). Avoid raw cost/time as a score — they depend on the task. Score = weighted profile, shown next to a cohort distribution, not a single number.

**Known trap** ([DX AI Measurement Framework](https://getdx.com/ai-measurement-framework/), [Warp](https://www.warp.dev/articles/metrics-that-prove-ai-coding-agents-are-working)): *acceptance rate* and *usage volume* are misleading in isolation; pair utilization with impact (kept/merged work) and cost. The [METR RCT](https://getdx.com/blog/metr-study-on-how-ai-affects-developer-productivity/) found experienced devs *felt* 20% faster while being 19% slower — the daily report's value is exactly in exposing that gap, so it should show time spent, not just activity.

---

## 3. How prior art compares

| Tool | What it does | Gap vs. our idea |
|---|---|---|
| ccusage, [claude-usage](https://docsearch.algolia.com/mcp/docs/repo/phuryn/claude-usage), [Claude Code Personal Analytics](https://pypi.org/project/dashboard-for-claude-code/), [cc-lens](https://www.sourcepulse.org/projects/27691938), [token-dashboard](https://www.sourcepulse.org/projects/28644780) | Local JSONL → tokens, cost, sessions, tool counts dashboards | **Cost accounting only.** No rework/verification/steering, no git correlation, no scoring |
| [toolytics](https://awesome.ecosyste.ms/projects/github.com%2Fseolsnow%2Ftoolytics) | Claude Code + Codex tool/skill usage, daily collector | Closest to multi-agent; still usage counts, no behaviour scoring |
| Claude Code OTel + Grafana/Elastic | Team-wide telemetry pipeline | Heavy, content redacted by default, team not individual coaching |
| DX / Larridin ([Agent Effectiveness Score](https://larridin.com/measurement-guide/agent-effectiveness/agent-effectiveness-score/)) | Enterprise frameworks, PR/survey based | Not local, not per-session, paid |

**Our differentiation:** behavioural scoring of the *human*, git-correlated outcomes, daily coaching report, and the candidate-assessment mode. Nothing found does the last two.

---

## 4. Storage options

Whatever is chosen, normalise to **one agent-agnostic event model** first; metrics are computed from that, never from raw formats.

```
Session   (agent, session_id, cwd, repo, git_branch, started_at, ended_at, model_default)
Turn      (session, turn_idx, prompt_at, prompt_text, prompt_kind: new|followup|correction|interrupt,
           agent_started_at, agent_finished_at, stop_reason, idle_before_ms)
ApiCall   (turn, model, effort, input_tok, cached_tok, output_tok, thinking_tok, cost_usd, duration_ms)
ToolCall  (turn, name, kind: read|edit|exec|search|delegate|other, args_summary, started_at,
           duration_ms, is_error, interrupted, files_touched[])
FileEdit  (tool_call, path, lines_added, lines_removed, user_modified)
Event     (session, at, kind: compaction|interrupt|mode_change|permission|subagent_spawn|pr_link, data)
GitChange (repo, commit_sha, at, files[], lines_added, lines_removed, author)     ← outcome side
DailyScore(date, person, metric, value, evidence_refs[])                            ← report side
```

| Option | Pros | Cons | Fit |
|---|---|---|---|
| **A. SQLite (local)** — collector parses JSONL + git → tables above; report queries it | Zero infra, privacy by default (prompts contain source), fast queries, trivial to ship as a CLI; same as most prior art | Single machine; candidate mode needs an export step | **v1 / hackathon** |
| **B. DuckDB over raw JSONL + Parquet** | No ETL — query the transcripts in place; great for exploration, columnar for aggregates | Raw formats drift (two already differ); every metric re-parses; harder to add hooks-live data | Good for *prototyping metrics* before committing a schema |
| **C. Local collector + cloud (Postgres/ClickHouse) sync** | Team view, candidate cohort comparison, history survives machine loss | Must redact prompt/tool content or get consent; ops at a hackathon | Candidate mode later; sync only the *scored* events, not content |
| **D. OTel → Grafana/ClickHouse** | Standard pipeline, Claude Code supports it natively | Codex doesn't emit OTel; content redacted → rework/steering metrics impossible; no git | Not for this |

**Recommendation:** A, with the raw JSONL kept as source of truth (re-parse on schema change). Store `prompt_text` and tool args locally only; the exportable "candidate bundle" contains metrics + evidence excerpts, not full transcripts.

### Capture: files vs. hooks
- **File watching** (`fswatch` / polling `~/.claude/projects` and `~/.codex/sessions`) works for both agents, no config on the user's side, catches everything retroactively. Sufficient for daily reports.
- **Hooks** add precise wall-clock for prompt-submit / stop / tool events and work the same on both agents. Worth adding as an *optional* precision layer; never required.
- **Git**: read `git log/diff` for the repo in each session's `cwd` at report time; optionally snapshot `git status` on `Stop` hooks for finer attribution.

---

## 5. Architecture options

1. **CLI + SQLite + Markdown report** (`scorer collect`, `scorer report --day today`), scheduled via launchd/cron. Smallest; what prior art does; mates can hack metrics as SQL/Python. Add a local web page later.
2. **Daemon + local web dashboard** (watcher → SQLite → small web UI with trends). Nicer demo, more surface to build in a hackathon.
3. **Hosted** (collector uploads scored events; web app with team/candidate views). Needed for candidate mode at scale, premature now.

Report generation: deterministic metrics in code, **plus an LLM pass** over the day's structured events to write the narrative ("you spent 38 min across 4 re-prompts on the auth bug; the first prompt lacked the error message") and classify prompt kinds. Keep the LLM out of the numbers.

### Candidate mode specifics
- Run in a **sandbox you provide** (devcontainer / cloud VM with the collector + both agents preinstalled, challenge repo cloned). Solves tamper-resistance and gives you the git history for free. Installing on the candidate's own machine is realistic but untrustworthy.
- Needs: Challenge (repo + task + rubric), Attempt (sessions + git activity within a window), consent screen, cohort baseline for the score profile.
- Legal: transcripts contain the candidate's prompts → say what's collected and for how long.

---

## 6. Suggested hackathon scope (if you go for it)

1. Parser for both formats → normalised SQLite (~half a day; formats documented in §1).
2. Git correlation for the session's repo.
3. 8–10 metrics from §2 A–D that are ★ or ◆, with evidence links back to the turn.
4. `report` command → Markdown with the three sections: *where time went*, *rework & verification*, *3 things to try tomorrow* (LLM narrative over the structured data).
5. Demo on your own last 7 days of transcripts (you have hundreds of sessions locally — instant test data).

Candidate mode = same pipeline + a "label these sessions as Attempt X" command + a side-by-side report. Keep it as the stretch goal.

---

## 7. Decisions to make with the team

1. **Persona first:** self daily report (recommended; candidate mode becomes a labelled subset) vs. candidate assessment.
2. **Git in scope as outcome signal?** (recommended yes — without it there's no "good", only "busy").
3. **Which metric groups ship first?** Recommended: A waste, B rework, C verification; D/E/F as secondary.
4. **Time scope:** agent-interaction time only (recommended) vs. OS-level activity tracking.
5. **Data locality:** local SQLite only (recommended) vs. cloud sync for team/candidate views.
6. **Delivery:** CLI + Markdown (recommended) vs. local web dashboard vs. hosted.
7. **Capture:** file watching only vs. + hooks on both agents.
8. **Candidate sandbox:** VM/devcontainer you provide (recommended) vs. install on candidate's machine.
9. **Scoring:** single composite score vs. metric profile against a baseline (recommended profile; a single number invites gaming and hides the METR-style perception gap).

### Proposed vocabulary (to pin in CONTEXT.md once decided)
- **Agent Session** — one transcript file of one agent (Claude Code session / Codex rollout).
- **Turn** — one human prompt and everything the agent did until it stopped.
- **Work Day** — all Agent Sessions of one person on one calendar day, across agents and repos.
- **Kept Change** — agent-written lines that survive into a commit.
- **Challenge / Attempt / Candidate** — candidate mode only; an Attempt is a labelled set of Agent Sessions + git activity against a Challenge.

---

### Sources
- Codex hooks: [developers.openai.com/codex/hooks](https://developers.openai.com/codex/hooks), [Codex hooks guide](https://codex.danielvaughan.com/2026/04/15/codex-cli-hooks-complete-guide-events-policy-patterns/)
- Claude Code OTel: [Last9](https://last9.io/docs/integrations/claude-code/), [Elastic](https://www.elastic.co/docs/reference/integrations/claude_code), [OpenObserve](https://openobserve.ai/docs/integration/ai/claude-code-tracing/), [claude-otel](https://github.com/li0nel/claude-otel)
- Prior art: [claude-usage](https://docsearch.algolia.com/mcp/docs/repo/phuryn/claude-usage), [dashboard-for-claude-code](https://pypi.org/project/dashboard-for-claude-code/), [cc-lens](https://www.sourcepulse.org/projects/27691938), [token-dashboard](https://www.sourcepulse.org/projects/28644780), [toolytics](https://awesome.ecosyste.ms/projects/github.com%2Fseolsnow%2Ftoolytics), [ccclub usage guide](https://ccclub.dev/claude-code-usage)
- Measurement frameworks: [DX AI Measurement Framework](https://getdx.com/ai-measurement-framework/), [DX: measuring AI code assistants and agents](https://getdx.com/research/measuring-ai-code-assistants-and-agents/), [Warp: metrics that prove agents are working](https://www.warp.dev/articles/metrics-that-prove-ai-coding-agents-are-working), [Larridin Agent Effectiveness Score](https://larridin.com/measurement-guide/agent-effectiveness/agent-effectiveness-score/), [METR study summary](https://getdx.com/blog/metr-study-on-how-ai-affects-developer-productivity/), [Faros: lab vs reality](https://www.faros.ai/blog/lab-vs-reality-ai-productivity-study-findings), [arXiv 2504.12211](https://arxiv.org/html/2504.12211v1)
