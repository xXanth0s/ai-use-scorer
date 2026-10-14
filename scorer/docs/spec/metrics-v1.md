# scorer — metrics v1 (consolidated)

2026-10-14. Result of: 7-day data check on this machine, Codex review, and four parallel reviews (self-optimization, hiring, data-realist, test/demo). Demo in 4 h.

Principle: score the human's choices, not the agent. Every event carries `initiator = human | agent | default`.

## 1. Final metrics

### Scored

| id | metric | definition | source | tier |
|---|---|---|---|---|
| V | **Verification after changes** | share of edit batches followed by a *relevant* test/build/typecheck before the next edit batch; final state verified? Only `initiator=human` counts in candidate mode. Counter-metric: share of verifications that fail or cause a follow-up edit (0% = decorative). | hooks + Bash intent classifier + git snapshot | Fact |
| C | **Correction episodes** | each correction labelled on 3 axes: event (correction / new requirement / clarification / uncertain), cause (instruction violated / info missing / requirement changed / env failure / unknown), detection (human review / automated check / agent self-review / unknown). Includes defects caught (who caught, resolved?). Scored by cause, never by count. | LLM judge, quoted evidence, abstention, calibrated (κ ≥ 0.6 on 50 double-labelled turns) | Judge |
| C+ | **Positive episodes** (what you did well) | same judge call as C (`positive_episode` + `episode_reason` ≤ 120, judge-v3), one label per turn or null; the label only counts when its deterministic gate passes. **good_spec**: the session's first prompt has goal + constraints + acceptance criteria (judge) and no later turn of that session is labelled correction. **verification_catch**: the human asked for a check (judge) and it found a defect: a human-initiated test/build call in that turn failed (`is_error=1`), or it failed and the next turn has a passing check. **early_redirect**: correction or interrupt at session position 1–2 (judge: specific reason stated; gate: event=correction or previous agent turn interrupted). **scoped_turn**: ≤ 3 files and ≤ 80 lines edited in the turn (`file_edit`), every edit batch `verified_pass`; judge adds "explicit done criteria". **knew_when_not_to_delegate**: a git-snapshot hand edit (H repair/own) on a file with ≥ 2 failed verifications or ≥ 2 correction turns touching it earlier in the session. Gate-only labels (scoped_turn, knew_when_not_to_delegate) without a grounded quote are stored with `evidence: null`, source `metrics`. Stored as `C+:<episode>` per day, evidence note `<episode>: <reason ≤ 60>`. Report line 6 picks by priority verification_catch > good_spec > scoped_turn > early_redirect > knew_when_not_to_delegate. Shown, never summed into C. | LLM judge (same evidence rules as C: quote is an exact prompt substring, ≤ 200 stored / ≤ 80 shown, abstention) + metrics gates; uncalibrated | Judge |
| H | **Hand-edits outside the agent** | git delta per turn − agent edit calls (incl. Bash writes). Split: repair of agent-touched files vs own work. Self: silent repair cost. Candidate: knew when not to use the agent. | git snapshot at Stop + tool calls | Correlated |
| B | **Blocked-agent minutes** | per session: Stop → next prompt while another session is active; plus "attended gaps" (gap <2 min + prompt ≤5 words). | hooks | Fact |
| E | **Response to evidence** (candidate) | after a failing test / contradiction: investigate, revise, or ship anyway, over the next 2 turns | judge | Judge |

### Diagnostic (shown, not scored; gameable or agent-dependent)

- Agent-active time & overlap (union wall time, share with ≥2 sessions)
- Between-turn gaps bucketed (<30s, 30s–2m, 2–5m, 5–15m; >15 min = break)
- Interruptions: count + agent time before each, labelled by the prompt that follows
- Context occupancy: % of model window per request, p50/p90
- Orientation cost per session: tokens/time from SessionStart to first file change; files re-read that another session read today
- Subagents (count, depth, model mix), parallel wall time, model mix, queued prompts, kept-change yield, compactions, repo switches, tokens, $ (usage × price table)
- **Tooling use**: skills / slash commands / MCC tools / plugins per day, split by initiator. Claude: `<command-name>/x` in user entry = human; `Skill` tool_use + `attributionSkill` = agent; hook-triggered = default. Codex: `/skill` or `$skill` in user message = human; skill items in rollout = agent (verify on 2026/10 files). Self: what you reach for vs what the agent reaches for. Candidate: discovered and used the sandbox tooling (skills, CLAUDE.md, plan mode).

### Dropped

Turn duration (agent metric), turns per task, prompt specificity, plan-mode checkbox, blind-accept streak, churn, error loops, throwaway minutes, late-context uplift, fresh-session discipline, per-role weightings (theatre at n=1; vary *definitions* per role instead: what counts as verification, where hand-coding is expected).

### Candidate mode

- Challenge rubric + tests = output quality, outside the profile.
- Three traps: false README hint (flag does nothing) → C cause=info-missing + E; ~30% flaky test → V + E + C cause=env-failure; ambiguous requirement with hidden conflict (nullable sort date vs pagination) → C event=clarification + initiator.
- 5-min debrief: explain the change, name one limitation.
- Legal (EU): candidate evaluation = AI Act Annex III high-risk; GDPR Art. 22; DPIA. Minimum: written consent listing what is captured; candidate sees the same profile as the panel; a human reads the transcript before any negative label counts; metrics are debrief prompts, not gates; timing never a disqualifier; retention ≤ ~90 days; same tooling + practice time for all.

## 2. Daily report structure

1. Verdict: the one thing that cost most today, with its number.
2. Tomorrow's one change, with the evidence line.
3. Top two correction episodes: cause, and the prompt that would have prevented each.
4. Unverified or hand-repaired state right now: sessions, files, what to run before pushing.
5. Worst attention window (blocked / babysitting), minutes lost.
6. One pattern to repeat, with evidence: the strongest positive episode (C+) of the day, else the best-verified session / human tooling.
7. Scored metrics vs 7-day baseline.
8. Diagnostics in one line + what the judge was unsure about.

## 3. Definitions (data-realist)

**Prompt** = `user` entry, not `isMeta` / `isCompactSummary` / `isSidechain`, text not starting with `<` (slash commands via `<command-name>` count).
**Turn** start = prompt ts (dequeue ts if queued). End precedence: Stop-hook ts → `turn_duration` (`start+durationMs`) → last assistant/tool_result ts, flagged `ended_by=crash`. Interrupt marker ends the interval at marker ts.
Resumed sessions: split turns at local midnight for day attribution. Forks: dedupe entries by `uuid`. Subagents: fold into parent turn (`end = max(end, last sidechain ts)`), never a second agent. Compaction summary: inside current turn. Queued prompt: `prompt_kind=queued`, gap 0. Permission wait: inside turn, tagged `blocked_on_human`.

```
ActiveTime: I = [(t.start, t.end, t.session) for t in day_turns(both CLIs, all repos)]
            active = len(union(I)); overlap = len(points covered by >=2 distinct sessions)
Gaps:       for t, next in consecutive turns of a session:
              gap = next.start - t.end; prompt in another session during gap -> switched
              gap > 15min -> break (excluded); crash turn -> undefined
Verify:     batch = maximal run of FileEdit not split by a prompt or test|build Bash
            lanes: main thread and each subagent sidechain form their own batches (never interleaved)
            window(batch) = after its last edit, before the lane's next edit batch or the next prompt
            verified(batch) = Bash(intent in test|build, relevant) in window,
                              WITH tool_result (backgrounded without result = no)
            tier (v2): the LAST relevant check in the window decides: failed -> verified_fail,
                  passed -> verified_pass (recovered = an earlier one in the window failed: red -> green,
                  counted in V_recovered) | inspected (git diff / Read of edited file) | none
            TDD red (`cat > test ; pytest` failing, then the lane's next batch ends green) -> pass, recovered
            subagent lane = its sidechain across turns; unchecked there -> parent's checks after it finished
            final = last batch verified AND no later FileEdit
```

**initiator** (precedence; store rule id + matched span): 1 executed by hook → `default`; 2 inside skill/slash command → `default`; 3 this turn's prompt requests the action class (regex `test|run|lint|verify`, LLM fallback for paraphrase/German) → `human`; 4 CLAUDE.md/AGENTS.md requests it (hashed at SessionStart) → `default` tagged `config:user|repo`; 5 else `agent`.

**Bash intent**: split on `&&`, `;`, `|`; strip `cd x &&`, env vars, `npx|pnpm exec|uv run|poetry run|bunx`; classify first token per segment; aggregate test > build > edit > git > run > search > read.

| intent | pattern |
|---|---|
| test | `pytest|vitest|jest|mocha|playwright|cargo test|go test|(npm|pnpm|yarn) (run )?test|make test|python -m (pytest|unittest)` |
| build | `tsc|mypy|pyright|eslint|ruff|biome|cargo (build|check|clippy)|go (build|vet)|(npm|pnpm) run (build|lint|typecheck)|docker build` |
| edit | `>`/`>>` to non-/dev/null, `cat > f <<EOF`, `tee`, `sed -i`, `patch`, `mv|cp|rm|mkdir|touch` → synthesize FileEdit row |
| git | all `git`; subkind read for `diff|log|status|show|blame` |
| search | `grep|rg|ag|fd|find` |
| read | `cat|head|tail|sed -n|awk|wc|ls|tree|jq|stat` |
| run | `python -c|node -e|curl|wget|./x|make <other>|docker run` |

Relevant test: nearest manifest dir (`package.json|pyproject.toml|go.mod|Cargo.toml`) of edited files vs cwd + path args: same/ancestor = relevant; repo-root no-args = relevant-broad; `foo.ts ↔ foo.test.ts` = relevant.

## 4. Storage (SQLite)

```sql
CREATE TABLE ingest_run(id INTEGER PRIMARY KEY, at TEXT, parser_version TEXT);
CREATE TABLE session(id TEXT PRIMARY KEY, agent TEXT, cwd TEXT, repo TEXT, branch TEXT,
  transcript_path TEXT, forked_from TEXT, started_at TEXT, ended_at TEXT, ended_by TEXT);
CREATE TABLE turn(id INTEGER PRIMARY KEY, session_id TEXT REFERENCES session, idx INT,
  prompt_at TEXT, prompt_text TEXT, prompt_kind TEXT, initiator TEXT, agent_started_at TEXT,
  agent_finished_at TEXT, end_source TEXT, first_uuid TEXT, last_uuid TEXT, UNIQUE(session_id, idx));
CREATE TABLE api_call(id INTEGER PRIMARY KEY, turn_id INT REFERENCES turn, request_id TEXT UNIQUE,
  model TEXT, effort TEXT, input_tok INT, cached_tok INT, output_tok INT, context_window INT,
  cost_usd REAL, duration_ms INT, at TEXT);
CREATE TABLE tool_call(id INTEGER PRIMARY KEY, turn_id INT REFERENCES turn, tool_use_id TEXT UNIQUE,
  name TEXT, kind TEXT, bash_intent TEXT, initiator TEXT, initiator_rule TEXT, args_summary TEXT,
  started_at TEXT, duration_ms INT, is_error INT, has_result INT, sidechain_id TEXT);
CREATE TABLE file_edit(id INTEGER PRIMARY KEY, tool_call_id INT REFERENCES tool_call, path TEXT,
  manifest_dir TEXT, lines_added INT, lines_removed INT, user_modified INT);
CREATE TABLE event(id INTEGER PRIMARY KEY, session_id TEXT REFERENCES session, turn_id INT,
  at TEXT, kind TEXT, data TEXT);
CREATE TABLE git_change(id INTEGER PRIMARY KEY, repo TEXT, sha TEXT, at TEXT, turn_id INT,
  files TEXT, lines_added INT, lines_removed INT, author TEXT, UNIQUE(repo, sha));
CREATE TABLE metric_value(id INTEGER PRIMARY KEY, run_id INT REFERENCES ingest_run, metric TEXT,
  definition_version TEXT, scope_type TEXT, scope_id TEXT, value REAL, unit TEXT, n INT);
CREATE TABLE metric_evidence(metric_value_id INT REFERENCES metric_value,
  turn_id INT REFERENCES turn, tool_call_id INT, git_change_id INT, note TEXT);
```

Raw JSONL is the source of truth; `run_id` + `definition_version` make every value re-derivable.

## 5. Collection

- `scorer install`: writes hooks for Claude (`~/.claude/settings.json`) and Codex (`~/.codex/hooks.json`, same schema). Events: SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd.
- `scorer event`: reads all stdin, appends one line to `~/.scorer/events.jsonl` (O_APPEND, no DB in the hook path), prints nothing, exits 0 in `finally` (<1 s; stdout of UserPromptSubmit is injected as context; exit 2 blocks the agent). At Stop: git snapshot (HEAD, index, untracked hash).
- `scorer ingest`: parses transcripts + events into SQLite; dedupes by session id / uuid / requestId.
- `scorer report --day today | --days 7`: deterministic numbers + LLM narrative (§2).
- Agent-called `scorer start/stop` = optional task annotation only, never the measurement boundary.
- Candidate: collector runs outside the candidate's write boundary; events shipped off-box.

## 6. Tests

a. **Fixtures**: `scripts/redact.py in.jsonl out.jsonl --keep 20` keeps structure/timestamps/ids/usage/tool names/`is_error`/`userModified`/queue ops; replaces text/command/path strings with `sha1[:8]`; drops attachment, api-request*, last-prompt, titles, pr-link, file-history-*, cost-state. Keep the literal `[Request interrupted by user` prefix. Six fixtures per agent: interruption, subagent, compaction, resumed session, queued prompt, error tool result.
b. **Goldens** (`tests/golden/*.jsonl`): (1) gaps 10s/90s/200s → buckets 1/1/1, median 90; (2) 3 assistant lines one `requestId` → 1 ApiCall; (3) Codex 2 `token_usage_record` same `response_id` → 1 ApiCall, `token_count.info.total_token_usage` is cumulative, never summed; (4) queued prompt → gap from enqueue ts, ≥0; (5) interrupt → `interrupted=1`, thrown time 45 s; (6) Edit then `pytest` within 3 calls → verified; Edit then `ls` → not; (7) `userModified:true` → hand-edit; (8) two sessions overlapping 120 s, different cwd → overlap 120. Cross-check Σ agent time vs `turn_duration` ±10%.
c. **Hooks**: capture real payloads with `claude -p "say hi" --settings capture.json` where each hook is `jq -c . >> hook_payloads.jsonl`; Codex same with `CODEX_HOME=$tmp`. Contract: reads 1 MB stdin, prints nothing, exit 0, <1 s, 8 parallel invocations don't corrupt. E2E in a throwaway repo: `env -u SUPERSET_HOME_DIR claude -p "…" --settings demo.json`; `codex exec -C /tmp/e2e --skip-git-repo-check --full-auto "…"`; verify via `system/stop_hook_summary`.
d. **Judge**: `tests/judge/cases/*.json`; assert schema, label set incl. `abstain`, evidence is a substring of input, empty turn → abstain; snapshot at temperature 0. Calibration: 50 real turns, 2 humans, κ ≥ 0.6.
e. **Replay** (`scorer ingest --since 30d && scorer check`): no negative gaps; usage deduped; every ToolCall has a result or sits in an interrupted turn; overlaps only across cwd/agent; turns ≈ `turn_duration` lines ±10% (`last-prompt` is ~2× turns, don't use).

## 7. Demo (4 h)

- 0:00–0:30 `git init`, `uv init`; capture hook payloads; `scorer event` appends.
- 0:30–1:30 Claude parser → SQLite (port `explore_claude.py`); goldens 1,2,4,5.
- 1:30–2:15 Codex parser, only `~/.codex/sessions/2026/10` (v0.159+; 43k older files are v0.98 format); goldens 3,6.
- 2:15–3:00 `scorer report --days 7`: active h, gap buckets, interrupts, verify rate, context p50/p90.
- 3:00–3:30 replay on real data, fix invariants. 3:30–4:00 rehearse; write `report_fallback.txt`.
- Cut: old Codex format, git snapshots, subagent→turn attribution, LLM judge (regex stand-in), full Bash classifier (test/build only).

Script (3 min): (1) `scorer report --days 7` on this machine's real data. (2) Split terminal: `tail -f ~/.scorer/events.jsonl` right, `claude -p "add a failing test to hello.py" --settings demo.json` left in `/tmp/e2e`; events scroll live. (3) `scorer ingest && scorer report --day today` shows the new turn. Fallback: `cat report_fallback.txt` + pre-ingested DB.

Risks: hook blocks if stdin unread / stdout injected / exit 2 blocks the agent → read all, print nothing, exit 0 in `finally`. Existing superset hooks fire too → `env -u SUPERSET_HOME_DIR`. Sessions resume across days → split by prompt day. Timezone: Codex filenames local, timestamps UTC → bucket in local tz. `claude -p` needs auth/network on stage → dry-run 10 min before.
