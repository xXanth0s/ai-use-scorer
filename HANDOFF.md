# HANDOFF: scorer

Last updated: 2026-10-14 (evening) · git `cc5900f` (main), remote `origin` = GitHub `xXanth0s/ai-use-scorer` (the lead pushes; agents commit only their own files). Untracked: `demo/` (video agent, includes `demo/seed/make_demo_db.py` and `demo/scorer-demo.db`) and `docs/spec/{bridge-brief,metrics-merged,video-brief,video-storyboard,video-usecases}.md`. Run `git status` first.

**`ari-version/` is a teammate's (Ari's) separate Next.js app, merged from origin/main in `9514601`. Never modify anything under `ari-version/`.** The README has our scorer section on top and Ari's section below.

Read this first, then `STATUS.md` (milestone log), `docs/spec/metrics-v1.md` (source of truth for metrics, DDL and definitions) and `DESIGN.md` (dashboard theme).

---

## 1. What scorer is

scorer measures how well a human drives coding agents (Claude Code and Codex). It does this from local transcripts and lightweight hooks, entirely on the local machine. It ingests everything into SQLite, computes deterministic metrics plus LLM-judged correction episodes, and shows them as a daily Markdown report and a local dashboard with an "Ask AI" analyst chat.

**Principle: score the human's choices, not the agent.** Every event carries `initiator = human | agent | default`. Agent-driven work (subagent threads, agent-initiated turns) is kept for tokens and cost but excluded from human time and scoring.

**Current focus: self-review** (your own daily use). Candidate/hiring mode (spec §1 "Candidate mode", metric E) is **out of the UI for now** (user decision): no Candidate tab, nothing candidate-related on the dashboard. It can be added later. The judge is uncalibrated.

---

## 2. What was built (per milestone)

| Area | What exists | Main files |
|---|---|---|
| Hooks + event CLI (M1) | `scorer event` is a fast path in `__main__.py` that bypasses click and sqlite. It reads all of stdin, appends one JSON line to `$SCORER_HOME/events.jsonl` with O_APPEND, prints nothing and always exits 0 (~56 ms, ~91 ms at Stop). It takes a git snapshot at SessionStart (baseline), Stop and SessionEnd. Real Claude E2E passes. | `hookio.py`, `__main__.py`, `scripts/demo_e2e.sh`, `scripts/capture_hooks.sh` |
| Install | `scorer install/uninstall` shows a diff first, merges one scorer group per event, writes atomically with a `.bak`, and is idempotent. Modes: **global** (`~/.claude/settings.json`, `~/.codex/hooks.json`) plus a daily **launchd** job (cron on Linux) and a default `config.toml`; **`--project DIR`** (lab: `.claude/settings.local.json` and `.codex/hooks.json`, pinned to the current `SCORER_HOME`, no schedule); `--no-schedule`; `--dry-run`. | `install.py` |
| Parsers | Claude: turns, prompts, queued prompts, interrupts, subagent folding, requestId dedupe, compaction, tooling events. Codex 0.155–0.160 rollouts: `token_usage_record` dedupe by `response_id`, CommandExecution/FileChange from code-mode `exec`, apply_patch edits, steer becomes a queued turn, subagent threads become their own sessions. Both are pure: `parse(path, hook_events) -> Bundle`. | `parse_claude.py`, `parse_codex.py`, `model.py`, `docs/notes/*-format.md` |
| Ingest + exclusion + purge | Discovery by mtime (`--since Nd`), idempotent per-session delete and reinsert, Stop-hook turn ends and git snapshots joined from `events.jsonl`. Lab, scratch and temp cwds are **excluded and purged by default** (`DEFAULT_EXCLUDE_CWD`, config `exclude_cwd`, `--include-lab`). `--cwd-prefix` / `SCORER_CWD_PREFIX` keeps only one tree, and an explicit prefix wins over exclusion. The judge workdir is always excluded. | `ingest.py`, `db.py` |
| Metrics | Deterministic V / H / B plus all diagnostics. **Attribution rules (`f160daf`, definition v2):** subagent calls inherit the spawning turn's initiator; `asked_by_you` counts humans only; hand edits exclude parallel-session and subagent edits; the verification tier is the last relevant check in the window, with `recovered` (red → green) and `V_recovered`; `/exit` is no longer a turn: active time, overlap, gaps, interrupts, context occupancy, tokens, cost per model, subagents, orientation, and tooling by initiator. The Bash intent classifier handles wrappers, heredocs, pipes and masked exit codes. Day = config `day_cutoff`, cutoff to cutoff in local tz. | `metrics.py`, `classify.py`, `prices.py`, `config.py` |
| Check | Replay invariants (spec §6e): no negative gaps, usage deduped, tool results, overlap only across sessions, turn counts. | `check.py` |
| Report | `scorer report`: the 8-line structure from spec §2 with a 7-day baseline and $ per model, then "What you did well" and "Action items". It adds an analyst narrative when a key is present. `report.facts()` is the single source for both report and dashboard verdicts. | `report.py`, `scripts/report_fallback.sh` |
| Judge (v3) | Correction episodes on 3 axes (event / cause / detection) plus defect_caught/resolved. Per turn it adds `user_action`, `preventable_by_user`, `action_category`, `user_strength` and `strength_category` (strengths = positive episodes). **v3 (`edbf5d0`)** adds data-gated positive episodes (`good_spec`, `verification_catch`, `early_redirect`, `scoped_turn`, `knew_when_not_to_delegate`) as `C+:<episode>` metrics; report line 6 ("repeat") comes from them, and the first prompt of each session is labelled too. Model is `claude-haiku-5-5` with a strict tool schema; evidence must be an exact prompt substring, otherwise the judge abstains. Labels are cached in `judge_label` by turn hash (`PROMPT_VERSION="judge-v3"`; the cache key is model + prompt digest, so any prompt change re-labels). Backend chain: API → `claude -p` → regex (flagged, confidence 0.3). Daily **action items** and **strengths** are clustered by one Haiku call and cached in `judge_action`. **Attribution** sorts corrections into user_preventable / agent_error / environment / unclear, and splits verification and tooling into you / agent. | `judge.py`, `tests/judge/` |
| Analyst agent (Python, default) | Anthropic tool-use loop with `query_db` (read-only connection, authorizer, clipping views), `get_turn`, `get_metric` and `list_sessions`. It returns citations as `[turn:id]` pills, streams over SSE, generates the narrative and adds Haiku quick-reply tiles. Default model is `claude-opus-5-5`, with `claude-sonnet-5-5` as fallback. Since `53f4a94`: concise answers (lead with the answer, ~120-word cap, no advice boilerplate), no speculation presented as advice, 3–5 sentence narrative, `maxOutputTokens` 2500. | `agent.py` |
| Mastra sidecar (optional) | Node/TS. An `analyst` delegates to the subagents `turnInvestigator` (Haiku, ≤4 `get_turn` per turn) and `sqlAnalyst` (Sonnet, ≤3 `query_db`). The harness has a read-loop guard, a history limiter (100k tokens) and JSONL traces in `$SCORER_HOME/traces`. Its tools are HTTP wrappers over Python `POST /api/tools/<name>`, so Python owns the data and the clipping. Its analyst runs on `claude-sonnet-5-5` (Opus fallback). `scorer serve` proxies `/api/chat` to it when `SCORER_MASTRA_URL/health` answers within 300 ms, and otherwise falls back to Python. | `agent-mastra/src/*`, `scripts/serve_with_mastra.sh` |
| Dashboard | Stdlib `ThreadingHTTPServer` with no build step, in the Sherpa theme. **Mockup A layout port (`93c8891`):** header card, worktree lanes, two-column cards, diagnostics, action items, chat header; plain metric names (no V/H/B/C letters in the UI); judge v3 episode chips; a demo-data badge when serving a demo DB; the chat panel is ported from the Mastra dummy page. `--db` / `SCORER_DB` picks the DB for serve/report/judge. `/api/attribution` tooling accepts `{count, turn_ids}` buckets (`03d9926`). Tabs: Today / Sessions / Trends / Leverage. Today has the worktree timeline with hover summaries, the narrative, a 4-number band, you-vs-agent split bars, "What you did well", corrections by cause with owner chips, and action items. The Ask-AI sidebar shows steps, subagents, citations, peek and tiles, and becomes a drawer under 1440 px. Judge output carries an "uncalibrated" chip. Lab and temp sessions are hidden. | `serve.py`, `cache.py`, `static/{index.html,app.js,app.css}`, `DESIGN.md` |
| herdr lab | A scripted multi-agent lab: 3× Claude + 1× Codex in herdr panes, scripted correction / clarification / interrupt / queued / hand-edit / test-asked scenarios, isolated in `~/.scorer-lab`, with ground truth in `lab-actions.log`. Its findings fed the parser fixes in `643a744`. Project sets `LAB_SET=v1|v2` (v2: 4 Claude-only projects with positive episodes), a `compare` subcommand (ground truth vs DB), and a stable-idle wait before each next prompt (`3fcf24a`, `4053428`). `measure` ingests all of `~/scorer-lab`, so earlier runs stay in the lab DB. Run4 is in `~/.scorer-lab/scorer.db`. | `scripts/lab/orchestrate.sh`, `scripts/lab/sets/` |
| QA | Mastra harness QA: dummy chat page, tool checks (130/136 at the time), chat quality, and abort. The 11 bugs it found are fixed in `cc6de95`/`42e0783` (marked in FINDINGS.md). | `agent-mastra/qa/*`, `agent-mastra/public/index.html` |

---

## 3. Architecture and file map

```
 Claude Code / Codex                                   ~/.claude/projects/*/*.jsonl (+subagents/)
   │ hooks: SessionStart UserPromptSubmit                ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
   │        PostToolUse Stop SessionEnd                              │
   ▼                                                                 ▼
 scorer event (hookio.py, stdlib only, exit 0)        parse_claude.py / parse_codex.py  (pure)
   │ O_APPEND + git snapshot at Start/Stop/End               │  hook events joined by session_id
   ▼                                                         ▼
 $SCORER_HOME/events.jsonl ───────────────────────►  model.Bundle ──► ingest.py (exclude/purge, idempotent)
                                                                         │
                                                                         ▼
                                                 $SCORER_HOME/scorer.db  (SQLite, db.py DDL + migrations)
                                                       │            │                 │
                                     metrics.compute_all            judge.py (Haiku)   check.py (invariants)
                                     (metric_value/evidence)        judge_label / judge_action / C:* metrics
                                                       │            │
                                                       ▼            ▼
                                   report.py (facts() + render) ◄───┤
                                     │  `scorer report` → stdout / ~/.scorer/reports/<date>.md (launchd 18:00)
                                     ▼
                                   serve.py  ── /api/day /api/turn /api/trend /api/report /api/narrative
                                     │          /api/summary /api/actions /api/strengths /api/attribution
                                     │          ui_cache (cache.py) for LLM prose
                                     ├── static/ (index.html, app.js, app.css) ── browser: tabs + Ask-AI sidebar
                                     │
                                     └── /api/chat (SSE) ─┬─► agent.py (Python analyst, default)
                                                          │      tools: query_db get_turn get_metric list_sessions
                                                          └─► proxy to Mastra sidecar if SCORER_MASTRA_URL/health ok
                                                                 agent-mastra/src/server.ts  POST /chat, GET /health
                                                                 analyst → turnInvestigator / sqlAnalyst
                                                                 tools.ts ──HTTP──► serve.py POST /api/tools/<name>
                                                                 GET /api/agent/system-prompt (shared prompt)
```

| File | Responsibility | Key functions / entry points |
|---|---|---|
| `src/scorer/__main__.py` | Entry point; `event` fast path | `main()` |
| `src/scorer/cli.py` | click CLI: install, uninstall, ingest, check, report, serve, judge | command functions |
| `src/scorer/hookio.py` | Hook sink, git snapshot | `main_event(argv)`, `build_line()` |
| `src/scorer/install.py` | Hook merge/unmerge, launchd/cron, default config, project mode | `run_install()`, `run_project_install()`, `merge()`, `unmerge()`, `run_schedule()`, `plist_text()`, `ensure_default_config()` |
| `src/scorer/model.py` | Bundle dataclasses, paths, clipping, tz | `scorer_home()`, `events_path()`, `db_path()`, `clip()`, `parse_ts()`, `local_day()` |
| `src/scorer/db.py` | DDL (spec §4) and additive migration | `connect()`, `_migrate()` (`EXTRA_COLUMNS`), `latest_run_id()` |
| `src/scorer/parse_claude.py` | Claude transcript → Bundle | `parse(path, hook_events)` |
| `src/scorer/parse_codex.py` | Codex rollout → Bundle | `parse(path, hook_events)`, `parse_patch()` |
| `src/scorer/ingest.py` | Discovery, exclusion, purge, DB write | `run_ingest()`, `write_bundle()`, `default_exclude_patterns()`, `purge_excluded()`, `cwd_prefixes_from_env()` |
| `src/scorer/classify.py` | Bash intent, tool kinds, relevance, human check request | `bash_intent()`, `bash_edit_paths()`, `tool_kind()`, `is_relevant()`, `human_check_request()`, `summarize_command()`, `repo_root()` |
| `src/scorer/prices.py` | Price and context-window tables | `cost_usd()`, `context_window()`, `lookup()` |
| `src/scorer/config.py` | `config.toml` loader | `load() -> Config` |
| `src/scorer/metrics.py` | Deterministic metrics and read-only helpers | `compute_all()`, `day_bounds()`, `verification_batches()`, `hand_edits()`, `blocked()`, `gaps()`, `tooling()` |
| `src/scorer/check.py` | Replay invariants | `run_checks()` |
| `src/scorer/judge.py` | Correction / action / strength judge, attribution | `run_judge()`, `label_candidates()`, `validate()`, `backend_chain()`, `action_items()`, `strengths()`, `attribution()` |
| `src/scorer/report.py` | Daily report | `facts()`, `render()` |
| `src/scorer/agent.py` | Python analyst | `stream_chat()`, `narrative()`, `run_tool()`, `suggest_followups()`, `has_key()` |
| `src/scorer/cache.py` | `ui_cache` table | `make_key()`, `get()`, `put()`, `lock()` |
| `src/scorer/serve.py` | HTTP server, JSON API, SSE chat, Mastra proxy | `serve()`, `make_server()`, `day_payload()`, `turn_payload()`, `trend_payload()`, `chat_status()`, `sanitize_event()` (full endpoint list in its docstring) |
| `src/scorer/static/` | Dashboard UI (vanilla JS/CSS) | `index.html`, `app.js`, `app.css`, `fonts/` |
| `agent-mastra/src/server.ts` | Sidecar HTTP: `GET /health`, `POST /chat` (CORS loopback), static `public/` | `makeServer` |
| `agent-mastra/src/agents.ts` | analyst, turnInvestigator, sqlAnalyst, suggester | `buildAgents()`, `anthropicModels()` |
| `agent-mastra/src/mastra.ts` | Mastra instance and JSONL trace exporter | `createScorerMastra()` |
| `agent-mastra/src/chat.ts` | One chat request → event stream | `runChat()` |
| `agent-mastra/src/translate.ts` | Mastra fullStream → Python event contract | `ChatEvent`, translator |
| `agent-mastra/src/tools.ts`, `lib/scorer-client.ts` | HTTP wrappers over `/api/tools/*` | `TOOL_NAMES` |
| `agent-mastra/src/suggest.ts` | Quick-reply tiles | `suggestFollowups()` |
| `agent-mastra/src/config.ts` | All sidecar env vars | `config`, `clip()` |
| `agent-mastra/src/harness/` | `read-loop-guard.ts`, `history-limiter.ts`, `trace-exporter.ts` | |
| `ari-version/` | Ari's Next.js app (teammate). **Do not modify.** | |
| `demo/` (untracked) | Video/demo assets; `demo/seed/make_demo_db.py` builds `demo/scorer-demo.db` | |
| `scripts/` | `demo_e2e.sh`, `demo.json`, `capture_hooks.sh`, `redact.py`, `report_fallback.sh`, `serve_with_mastra.sh`, `explore_claude.py` (seed), `lab/orchestrate.sh` | |

**Tests (structure):**
- `tests/test_{agent,check,classify,hooks,ingest_filter,install,metrics,parse_claude,parse_codex,prices,report,serve,static}.py`
- `tests/golden/`: g1 gaps, g2 request dedupe, codex golden 3, g4 queued, g5 interrupt, g7 userModified, g8 overlap, g9 tooling, plus `make_claude_goldens.py`
- `tests/judge/`: `cases/`, `test_judge.py`, `test_actions.py`
- `tests/fixtures/{claude,codex,hook_payloads}/` hold redacted fixtures
- `agent-mastra/test/`: `integration`, `static` and `translate` tests, plus a mock model

---

## 4. Data model

Spec DDL (`db.py`, verbatim from `metrics-v1.md` §4): `ingest_run`, `session`, `turn`, `api_call`, `tool_call` (spec already has `sidechain_id`), `file_edit`, `event`, `git_change`, `metric_value(run_id, metric, definition_version, scope_type day|session|all, scope_id, value, unit, n)`, `metric_evidence(metric_value_id, turn_id, tool_call_id, git_change_id, note)`.

Extensions beyond the spec:

| Table / column | Where | Purpose |
|---|---|---|
| `api_call.sidechain_id TEXT` | `db.EXTRA_COLUMNS`, ALTER on old DBs | subagent tokens and cost |
| `judge_label(turn_hash PK, model, label JSON, at)` | `judge.DDL` | per-turn judge cache |
| `judge_action(day, run_hash, items JSON, at, PK(day, run_hash))` | `judge.DDL` | clustered daily action items and strengths |
| `ui_cache(key PK, value JSON, at)` | `cache.DDL` | narrative and summary cache |

Raw JSONL is the source of truth. Ingest deletes and reinserts per session, so turn ids change, and metrics are recomputed in the same run. The judge's C rows live on the newest run that has them; `serve.py` reads them from there.

**Metric id catalogue** (day scope unless noted; headline set also at scope `all`):
- Time: `active_s`, `overlap_s`, `parallel_s`, `turns`, `sessions`, `gap_bucket:<lt30s|30s-2m|2-5m|5-15m|break>`, `gap_median_s`, `switch_share`, `interrupts`, `thrown_s`, `queued_prompts`
- Scored V: `V`, `V_fail_share`, `V_final`, `V_human`, `V_recovered`, `V_tier:<verified_pass|verified_fail|inspected|none>`
- Scored H: `H` (unit `edits` or `edits(partial)`), `H_repair`, `H_own`
- Scored B: `B_min`
- Context / cost: `context_p50`, `context_p90`, `tokens_in`, `tokens_cached`, `tokens_out`, `cost_usd`, `unpriced_calls`, `model_mix:<model>`, `tokens:<model>`, `cost_usd:<model>`
- Other diagnostics: `subagents`, `compactions`, `tool_calls`, `tool_error_share`, `orientation_s` (session scope plus day median)
- Tooling: `tooling:<category>:<initiator>`, `tooling:<category>`, `tooling_human_share`
- Judge (C): `C:event:<event>`, `C:cause:<cause>`, `C:detection:<detection>`, `C_abstain`, `C_source:<api|claude-p|regex|rule>`, `C:defect_caught`, `C:resolved`, `C_labelled`, `C+:<episode>` (positive episodes, judge v3)

The UI shows plain names for these (no V/H/B/C letters); the ids stay in the DB, report and API.

Judge enums: event = correction / new_requirement / clarification / uncertain / abstain. Detection = human_review / automated_check / agent_self_review / unknown. Action categories = prompt_specificity / verification / context / task_split / tooling / review / other.

---

## 5. What works vs stubbed / known limitations

Works: everything in §2 on real data, `scorer check` ok, pytest green (see §6 for the latest count).

Limitations:
- **H needs hooks.** Transcripts never set `userModified` reliably. Real hand-edit deltas come only from Stop and SessionStart git snapshots, so sessions without hooks report `edits(partial)`.
- **Codex hooks need trust.** Codex 0.160 asks the user to approve hooks interactively. They are not installed on this machine, and Codex hook payloads are unverified. Codex is ingested from rollouts only.
- **The judge is uncalibrated.** The κ ≥ 0.6 check on 50 double-labelled turns has not been done. The dashboard shows an "uncalibrated" chip. Haiku 5.5 rejects non-default temperature, so determinism comes from the schema plus the cache. Report verdict weights are guesses (unverified batch = 5 min, hand repair = 3 min).
- **Candidate tracking is out of the UI for now** (user decision; can be added later). Metric E, the traps and the debrief are not built. STATUS.md and `docs/design/decisions.md` now say the same.
- **Initiator rules 1 and 4 are not implemented.** Rule 1 (executed by hook → default) and rule 4 (CLAUDE.md/AGENTS.md requests it → default, tagged config) are missing. Only rules 2 (`2:command`), 3 (`3:prompt:<span>`) and 5 (`5`, agent) appear in data.
- **Permission waits are not timed.** Only accept/reject flags exist, with no `permission_wait` events and no `blocked_on_human` tag.
- `scorer check` gives a soft WARN: about 11% of Claude turns end by the last entry (interrupted or older turns without `turn_duration`).
- `--since Nd` filters by file mtime, so resumed sessions bring their whole history.
- V "fail share" mostly means "verified, then edited again", not failing tests.
- The judge backend chain only checks the `ANTHROPIC_API_KEY` env var. The Infisical fallback exists only in the Python analyst (`agent.py`).
- **QA bugs (`agent-mastra/qa/FINDINGS.md`):** all 11 fixed in `cc6de95`/`42e0783` and marked so; a full re-run of `tool_checks.py` / `chat_quality.py` has not been done since.
- **Open now** (also in STATUS.md): live chat streaming in the ported chat panel is being verified; the turn chip is moving to the batch tier + recovered; the lab DB needs a re-ingest after `f160daf`; zoom on Trends/Sessions, wheel zoom and range filtering are not done.
- **Demo DB drift.** The judge cache is keyed by model + prompt digest, so after any judge prompt change realign the demo DB: `python demo/seed/make_demo_db.py --from ~/.scorer/scorer.db --out demo/scorer-demo.db`.

---

## 6. Exact commands

All commands run from the repo root: `/Users/maximilianlang/Projects/Private/ai-use-scorer`.

**Install / uninstall** (each shows a diff and asks unless `--yes`):
```bash
uv run scorer install --agent claude --dry-run            # print diff only (global settings + schedule)
uv run scorer install --agent claude                      # global: ~/.claude/settings.json + launchd + config.toml
uv run scorer install --agent codex                       # ~/.codex/hooks.json (then approve trust in Codex)
uv run scorer install --agent claude --no-schedule        # hooks without the daily job
uv run scorer install --project ~/scorer-lab/foo --yes    # lab: project-local hooks pinned to $SCORER_HOME
uv run scorer install --agent claude --settings scripts/demo.json --no-schedule --yes   # regenerate demo settings (without --no-schedule it also touches launchd + config.toml)
uv run scorer uninstall --agent claude --yes              # removes hooks AND the launchd job
uv run scorer uninstall --agent claude --no-schedule --yes   # hooks only
```

**Ingest / check / judge / report / serve:**
```bash
uv run scorer ingest                          # --since 30d default; lab/temp excluded + purged
uv run scorer ingest --since 2d
uv run scorer ingest --cwd-prefix ~/scorer-lab/run2   # only this tree (repeatable; or SCORER_CWD_PREFIX)
uv run scorer ingest --include-lab            # keep lab/scratch/temp sessions
uv run scorer ingest --no-metrics --db /path/x.db
uv run scorer check                           # exit 1 on invariant failure
uv run scorer judge --day today               # today | YYYY-MM-DD | all ; --limit N
uv run scorer report --day today              # judges ≤40 uncached turns first (≤45 s)
uv run scorer report --days 7
uv run scorer report --day today --no-judge
SCORER_NO_LLM=1 uv run scorer report --day today   # no narrative / no LLM clustering
uv run scorer serve --port 8811               # default port 8765 → http://127.0.0.1:8811/
uv run scorer serve --port 8811 --db demo/scorer-demo.db   # demo DB (demo-data badge); same as SCORER_DB=...
python demo/seed/make_demo_db.py --from ~/.scorer/scorer.db --out demo/scorer-demo.db   # realign demo DB after judge prompt changes
```

**Mastra sidecar:**
```bash
scripts/serve_with_mastra.sh                       # sidecar :4111 + scorer serve :8765 (SCORER_MASTRA_URL set)
SCORER_PORT=8811 MASTRA_PORT=4111 scripts/serve_with_mastra.sh
cd agent-mastra && pnpm install && pnpm dev        # sidecar alone (PORT default 4111, SCORER_URL default :8765)
cd agent-mastra && pnpm test                       # vitest run (exits); pnpm typecheck
agent-mastra/qa/stack.sh start                     # QA: DB copy in /tmp/scorer-qa; sidecar :4190, scorer :8790 (mastra) / :8791 (python)
agent-mastra/qa/stack.sh stop
```
QA scripts and their arguments are listed under "Re-run" in `agent-mastra/qa/FINDINGS.md`.

**Tests:**
```bash
uv run pytest -q          # 2026-10-14 at cc5900f: 541 passed, 1 skipped in 17.2s
cd agent-mastra && pnpm test   # 2026-10-14 (working tree): 3 files, 23 tests passed
```

**Lab** (must run inside a herdr pane, `HERDR_ENV=1`):
```bash
scripts/lab/orchestrate.sh setup|launch|drive|measure|teardown|all|reset
LAB_ROOT=$HOME/scorer-lab/run3 scripts/lab/orchestrate.sh all   # fresh run; report → ~/.scorer-lab/reports/lab.md
LAB_ROOT=$HOME/scorer-lab/run4 LAB_SET=v2 scripts/lab/orchestrate.sh all   # v2 project set (run4 is in ~/.scorer-lab/scorer.db)
scripts/lab/orchestrate.sh compare            # ground truth (lab-actions.log) vs measured DB
```

**Demo E2E** (real Claude, never touches global settings):
```bash
SCORER_HOME=/tmp/scorer-demo PROMPT="add a failing test to hello.py" scripts/demo_e2e.sh
```

**Report fallback:**
```bash
scripts/report_fallback.sh                 # → report_fallback.txt (7 days); DAYS=1 for today; extra args → scorer report
cat report_fallback.txt                    # gitignored; contains clipped real text, do not commit/share
```

`scorer event --agent claude|codex --event <Name>` exists but is not listed in `--help` (fast path). Hooks call it as `<venv>/bin/python3 -m scorer event … 2>/dev/null || true`.

---

## 7. Config

`$SCORER_HOME/config.toml` (default `~/.scorer/config.toml`, written commented-out by `install`). All keys are optional:

```toml
timezone = "Europe/Berlin"        # default: system tz
day_cutoff = "04:00"              # turns before it belong to the previous day
report_time = "18:00"             # launchd/cron time
judge_model = "claude-haiku-5-5"
analyst_model = "claude-opus-5-5" # Python analyst only
exclude_cwd = ["/tmp/**", "lab-*"]   # REPLACES ingest.DEFAULT_EXCLUDE_CWD
[prices."claude-opus-5-5"]        # USD per 1M tokens, overrides prices.py
input = 5.0
cached = 0.5
cache_write = 6.25
output = 25.0
```

Environment variables:

| Var | Used by | Effect |
|---|---|---|
| `SCORER_HOME` | all | store dir (default `~/.scorer`): `events.jsonl`, `scorer.db`, `config.toml`, `reports/`, `logs/`, `judge/`, `traces/` |
| `SCORER_CWD_PREFIX` | ingest | default `--cwd-prefix` (`:`-separated) |
| `SCORER_DB` | report, judge, serve (`model.db_path()`) | overrides the DB file; `--db PATH` exports it so the analyst's connections use the same file |
| `SCORER_MASTRA_URL` | serve | proxy `/api/chat` to the sidecar when `/health` answers within 300 ms |
| `SCORER_NO_LLM=1` | report, serve, judge clustering | skip narrative, summaries and LLM clustering |
| `ANTHROPIC_API_KEY` | agent, judge, sidecar | API key. Without it the judge uses `claude -p`, then regex |
| Infisical fallback | `agent.py` only | if the env key is missing, one `infisical` CLI lookup (5 s, never logged) |
| `SCORER_ANALYST_MODEL`, `SCORER_FALLBACK_MODEL`, `SCORER_SUGGEST_MODEL`, `SCORER_INVESTIGATOR_MODEL`, `SCORER_SQL_MODEL`, `SCORER_SUGGEST_TIMEOUT_MS`, `SCORER_HISTORY_TOKEN_LIMIT`, `SCORER_URL`, `PORT` | Mastra sidecar only | defaults: sonnet / opus / haiku / haiku / sonnet / 6000 / 100000 / `http://127.0.0.1:8765` / 4111 |
| `SCORER_PORT`, `MASTRA_PORT` | `serve_with_mastra.sh`, `qa/stack.sh` | ports; `QA_DIR` and `SRC_DB` are for QA |
| `LAB_ROOT`, `LAB_MODEL`, `SCORER_HOME_LAB`, `LAB_SET` | lab orchestrator | defaults `~/scorer-lab`, `claude-sonnet-5-5`, `~/.scorer-lab`, `v1` |
| `SCORER_NO_SCHEDULE`, `SCORER_SCHEDULER`, `SCORER_LAUNCHCTL`, `SCORER_CRONTAB`, `SCORER_LAUNCHAGENTS_DIR` | install (test seams) | skip or redirect the scheduler |

---

## 8. Live state on this machine (as of 2026-10-14)

- **Claude hooks are installed globally (since 2026-10-14).** `~/.claude/settings.json` has one scorer group for each of the 5 events. The pre-install backup is `~/.claude/settings.json.bak`.
- **Codex hooks are not installed.** `~/.codex/hooks.json` exists with 0 scorer entries.
- **launchd** `com.scorer.daily` (`~/Library/LaunchAgents/com.scorer.daily.plist`, backup `.plist.bak`) is loaded and runs at **18:00 local**: `ingest --since 2d && report --day today > ~/.scorer/reports/<date>.md`. Logs go to `~/.scorer/logs/daily.{out,err}.log`. It had not run yet at handoff, so `~/.scorer/reports/` does not exist.
- **`~/.scorer`** holds `events.jsonl`, `scorer.db`, `config.toml` (all defaults commented out), `judge/` and `logs/`.
- **`~/.scorer-lab`** holds the lab store (`scorer.db`, `events.jsonl`, `lab-actions.log`, `lab-state.env`, `reports/lab.md`, `run1-aborted/`). Lab projects are in `~/scorer-lab/` (`run2/`, `run4/` plus `lab-*` dirs). The lab DB holds run4 but was ingested before `f160daf`, so re-ingest it.
- **Servers** (check with `lsof -i :8811` etc.; the list below is from the first handoff and may be stale):
  - `:8811` is `scorer serve --port 8811` on `~/.scorer/scorer.db`, with the **Python** chat backend (no Mastra).
  - `:4190` (sidecar), `:8790` (mastra-backed) and `:8791` (python-backed) are the **QA stack** (`agent-mastra/qa/stack.sh`) on `/tmp/scorer-qa/scorer.db`, not the live DB.
  - Stop them with `agent-mastra/qa/stack.sh stop` and by killing the `scorer serve --port 8811` process.
- **To revert everything:**
  ```bash
  uv run scorer uninstall --agent claude --yes      # hooks + launchd job (diff shown)
  rm -rf ~/.scorer ~/.scorer-lab ~/scorer-lab       # data (irreversible; optional)
  ```

---

## 9. Demo script (3–5 min)

Before the demo:
- Run `uv run scorer ingest && uv run scorer judge --day today`.
- Run `scripts/report_fallback.sh`.
- Start `uv run scorer serve --port 8811`.
- Dry-run `claude -p` about 10 minutes beforehand (it needs auth and network).

1. **(30 s) Pitch.** "Score the human's choices, not the agent." Everything is local, built from transcripts plus hooks.
2. **(60 s) Live hooks.** Split the terminal.
   - Right pane: `export SCORER_HOME=/tmp/scorer-demo; tail -f $SCORER_HOME/events.jsonl | jq -c '{at,event,tool:.payload.tool_name}'`
   - Left pane: `SCORER_HOME=/tmp/scorer-demo PROMPT="add a failing test to hello.py" scripts/demo_e2e.sh`
   - Events scroll live and the script prints PASS lines.
   - Note: the demo repo lives in a temp dir, which ingest excludes by default. To see that turn, ingest it into a separate store: `SCORER_HOME=/tmp/scorer-demo uv run scorer ingest --include-lab`.
3. **(60 s) Report.** Run `uv run scorer report --day today`. Walk through the verdict, tomorrow's change, corrections by cause, unverified-now, the worst attention window, the pattern to repeat, the baseline table, "What you did well" and the action items.
4. **(90 s) Dashboard** at http://127.0.0.1:8811/:
   - Today: header card, narrative, worktree lanes with hover, you-vs-agent bars, episode chips, action items, the uncalibrated chip. For a stable demo use `--db demo/scorer-demo.db` (demo-data badge).
   - Sessions: open a row, then a turn.
   - Trends.
   - Leverage.
5. **(45 s) Ask AI.** Ask "why is my verification rate low?" and show the steps, citations and peek. Optionally run `scripts/serve_with_mastra.sh` beforehand to show nested subagent steps.

**Fallback:** if live hooks, the API or the network fail, run `cat report_fallback.txt` and use the already-ingested `~/.scorer/scorer.db` with `scorer serve`. For an LLM-free live report, run `SCORER_NO_LLM=1 uv run scorer report --day today --no-judge`.

---

## 10. Next steps (prioritized)

1. **Calibrate the judge.** Double-label 50 real turns, compute κ against the 0.6 target, then tune the prompt. After that, drop the "uncalibrated" chip and revisit the report verdict weights.
2. **Implement initiator rules 1 and 4.** Rule 1 marks hook-executed actions as `default`. Rule 4 hashes CLAUDE.md/AGENTS.md at SessionStart and marks matching requests `default` with the tag `config:user|repo`. This affects V_human and the tooling split.
3. **Codex hooks.** Approve trust, then capture real payloads (`scripts/capture_hooks.sh`) and verify Stop snapshots and H for Codex.
4. **Finish the open items in STATUS.md** (chat streaming verification, turn chip on batch tier + recovered, lab re-ingest, Trends/Sessions zoom), then re-run Mastra QA (`tool_checks.py`, `chat_quality.py`).
5. **Watch the first launchd run** (18:00) in `~/.scorer/logs/daily.err.log` and check that `~/.scorer/reports/<date>.md` gets written.
6. **Permission waits.** Time accept/reject waits inside turns (`blocked_on_human`) and fold them into B.
7. **Candidate tracking** (out of the UI for now, can be added later): metric E, the three traps, off-box collection and the legal minimums (spec §1). Build it only after calibration.
8. **Cleanup.** `pyproject.toml` lists `anthropic` both as a core dependency and as the `judge` extra. Old fonts are kept only for `test_serve.py`.
