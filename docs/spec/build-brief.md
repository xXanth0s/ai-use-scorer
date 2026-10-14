# Build brief — scorer v1 (hackathon, demo in ~4 h)

You are the lead implementer. Read `docs/spec/metrics-v1.md` fully first; it is the source of truth (metrics, definitions, Bash intent classifier, SQLite DDL, collection design, test plan, demo plan, risks). `docs/research/ai-usage-scorer-options.md` is background. A working exploratory parser for Claude Code transcripts exists at `/private/tmp/claude-501/-Users-maximilianlang-Projects-Private-ai-use-scorer/5d1ca265-bb31-4df6-85b9-9a9ea6e5baca/scratchpad/explore_claude.py` — copy it into the repo as the seed.

## What to build

A Python CLI `scorer` (uv project, stdlib + sqlite3; `click` or `typer` allowed; `pytest` for tests) plus a local dashboard.

1. `scorer install` — writes hooks into `~/.claude/settings.json` and `~/.codex/hooks.json` (same schema) for SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd, each calling `scorer event --agent <claude|codex> --event <name>`. Must merge, never overwrite existing hooks. Provide `scorer uninstall`.
2. `scorer event` — reads ALL stdin, appends one JSON line to `~/.scorer/events.jsonl` (O_APPEND), prints nothing, exits 0 in `finally`, <1 s. At Stop: git snapshot of cwd (HEAD sha, `git diff --stat` hash, untracked file list hash) appended as part of the event. Never touch the DB in the hook path.
3. `scorer ingest [--since 30d]` — parses Claude transcripts (`~/.claude/projects/*/*.jsonl` + `subagents/`) and Codex rollouts (only `~/.codex/sessions/2026/10/**`, v0.159+ format; skip unknown `payload.type`), merges `events.jsonl`, writes SQLite at `~/.scorer/scorer.db` using the DDL in the spec. Idempotent; dedupe by session id, `uuid`, `requestId` / `response_id`.
4. Metrics (deterministic first): verification after changes (Bash intent classifier, test/build at minimum), hand-edits (git snapshot delta minus agent edits; may be stubbed if snapshots absent), blocked-agent minutes, and all diagnostics (active time & overlap, gap buckets, interruptions, turn duration, context occupancy with per-model window table, subagents, parallel wall time, model mix, tokens, cost from a price table). `initiator` field per the precedence rules. Store in `metric_value` + `metric_evidence`.
5. Judge (last; stub acceptable for demo): correction episodes via Anthropic API (`claude-haiku-5-5`), structured output with event/cause/detection, evidence quote, abstention; cached by turn hash. If no API key, regex stand-in flagged as such.
6. `scorer report --day today | --days 7` — Markdown report in the 8-line structure from the spec §2; narrative via LLM if key present, else deterministic text.
7. Dashboard — a local web page (`scorer serve`, stdlib http.server or FastAPI) reading the SQLite DB: day timeline (agent-active per session, overlaps, gaps, interruptions), the scored metrics with evidence links to turns, diagnostics, 7-day trend. Keep it simple and readable in light and dark; no build step.
7b. Analyst agent in the dashboard — a chat panel next to the report. Backed by the Anthropic API (`claude-opus-5-5` default, `claude-sonnet-5-5` fallback), key from `ANTHROPIC_API_KEY`; graceful "no key" state. Tools the agent can call: `query_db(sql)` (read-only SQLite, row cap), `get_turn(turn_id)` (prompt excerpt ≤80 chars, tool calls, timings, evidence), `get_metric(metric, scope)`, `list_sessions(day)`. System prompt carries the metric definitions from `metrics-v1.md` so it answers "why is my verification rate low" with evidence links to turns. Same agent generates the daily narrative. Streaming responses; conversation kept client-side for the session. Dashboard page shows the chat beside the day timeline so the user can ask about what they see.
8. Tests per spec §6: redaction script for fixtures from real transcripts, the 8 golden cases, hook contract tests (stdin 1 MB, silent, exit 0, parallel), replay invariants on real data. `uv run pytest -q` must pass.

## Working rules

- Work in this repo; commit early and often with clear messages.
- Use many subagents in parallel: Claude parser, Codex parser, classifier + metrics, hooks + event CLI, dashboard, tests. Keep interfaces (event model / DDL) fixed from the spec so they can work independently.
- Consult the advisor before committing to the architecture and before declaring any milestone done.
- Order: hour 0 hooks + event CLI (that's the live demo moment) → Claude ingest + goldens → metrics + report on real data → dashboard → Codex ingest → judge. Cut from the end if time runs out.
- Never modify `~/.claude/settings.json` or `~/.codex/hooks.json` without showing the user the diff first; for tests use `--settings` / `CODEX_HOME` temp dirs.
- Real data on this machine: 57 Claude sessions (25 multi-day), 25 current-format Codex rollouts in `2026/10`. Prompts contain source code: never print prompt text in logs or reports beyond 80 chars.
- Hook pitfalls: UserPromptSubmit stdout is injected into the agent context; exit 2 blocks the agent; superset hooks already exist and fire too (`env -u SUPERSET_HOME_DIR` for demos).
- When done with each milestone, write a 3-line status to `STATUS.md` (what works, what's stubbed, how to demo).
