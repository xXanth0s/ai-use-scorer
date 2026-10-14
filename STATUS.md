# STATUS

## M0 — skeleton (done)
Works: uv project, CLI surface, shared contract (`model.py`, `db.py` DDL, `ingest.write_bundle`), stubs for classify/prices.
Stubbed: everything behind the CLI commands.
Demo: `uv run scorer --help`.

## M1 — hooks + event CLI (done)
Works: `scorer event` (silent, exit 0, O_APPEND, ~56 ms / ~91 ms at Stop with git snapshot), `scorer install|uninstall` (merge, diff-first, `.bak`, idempotent, hardened `2>/dev/null || true`); real Claude E2E passes (`scripts/demo_e2e.sh`, all 5 events, Stop git.head == repo HEAD, no injected output, stop_hook_summary clean).
Stubbed/unverified: Codex hook payloads (Codex 0.160 trust gate — user must approve hooks interactively); real `~/.claude/settings.json` install awaits user OK (demo uses `--settings scripts/demo.json`).
Demo: pane 1 `export SCORER_HOME=/tmp/scorer-demo; tail -f $SCORER_HOME/events.jsonl | jq -c '{at,event,tool:.payload.tool_name}'`; pane 2 `SCORER_HOME=/tmp/scorer-demo PROMPT="say hi, then run 'ls' with the Bash tool" scripts/demo_e2e.sh`.

## M2 — Claude ingest + goldens (code done; hook→ingest seam being verified)
Works: Claude + Codex parsers → SQLite; 30 d real data ingests in ~5 s (82 sessions, 541 turns, 6.5k api calls, 7.2k tool calls); `scorer check` ok (soft WARN: 11% of Claude turns end by last entry — interrupted/older turns without turn_duration); goldens 1–8 green; 266 tests.
Stubbed: H = edits(partial) until hooks provide git snapshots (transcripts never set userModified); permission waits not visible; initiator rules 1 + 4.
Definition v1 notes (deviations from spec, deliberate): turn end = the turn_duration entry's own ts (durationMs counts from the last trigger, often a background notification); a resumed agent run >15 min after the previous one keeps its rows but doesn't extend the turn; dequeue ts used as prompt ts only within 5 s of the prompt; command turns (/clear, /model) kept with agent_started_at NULL; task-notification / other-session relay messages are not prompts; Codex subagent threads are own sessions (forked_from) excluded from human time/overlap/gaps/B but kept in tokens/cost; `--since Nd` filters by transcript file mtime (resumed sessions bring their whole history).
Demo: `uv run scorer ingest --since 30d && uv run scorer check`.

## M3 — metrics + report (metrics done; report in progress)
Works: V/B + diagnostics + tooling on real data (30 d: active 33.5 h, overlap 3.2 h, V 22% of 254 batches, final verified 2%, B 184 min, 33 interrupts / 2.7 h thrown, $790 all priced). Note: V "fail share" 91% is ~all "verified then edited again" (only 5/235 test/build runs failed).
Pending: report.py, report_fallback.txt, judge (bounded to 40 uncached turns at report time; `--no-judge` fallback).

## M2 — done (hook→ingest seam verified)
`scripts/demo_e2e.sh` with "add a failing test to hello.py": turn ends via Stop hook (matched by prompt_id), git_snapshot attached with changed_paths, H measured (unit `edits`), check ok.

## M3 — metrics + report (done)
`scorer report --day today | --days 7`: 8 lines from metric rows, 7-day baseline, $ per model next to tokens, analyst narrative when a key is present (`SCORER_NO_LLM=1` to skip). Fallback: `scripts/report_fallback.sh` → `report_fallback.txt` (gitignored; contains clipped real text).
Known: verdict weights are guesses (unverified batch = 5 min, hand repair = 3 min).

## M6 — judge (done)
`scorer judge --day today|YYYY-MM-DD|all`; `scorer report` judges ≤40 uncached turns first (≤45 s), `--no-judge` to skip. claude-haiku-5-5, strict tool schema, evidence must be an exact prompt substring else abstain, cached in `judge_label`; fallbacks `claude -p` (runs in SCORER_HOME/judge, excluded from ingest) → regex (flagged). Haiku 5.5 rejects temperature≠default → determinism via schema + cache. Calibration (κ on 50 double-labelled turns) not done.

## In progress
Dashboard (Sherpa theme, tabs Today/Sessions/Trends/Leverage/Candidate, Ask-AI sidebar) · Mastra sidecar (agent-mastra/, subagents turnInvestigator/sqlAnalyst; optional, Python agent stays default) · `install --project` + `ingest --cwd-prefix` for the herdr test lab.

## Live collection (installed 2026-10-14, user-approved diff)
Claude hooks in ~/.claude/settings.json (one scorer group per event, backup settings.json.bak), daily launchd job com.scorer.daily at 18:00 (ingest --since 2d + report → ~/.scorer/reports/<date>.md), default ~/.scorer/config.toml. Codex hooks NOT installed (needs user trust approval). Revert: `uv run scorer uninstall --agent claude --yes`.
Mastra sidecar analyst model: claude-sonnet-5-5 (Opus fallback); Python analyst stays on config analyst_model (Opus).

## Dashboard (done; review round 1 applied)
`uv run scorer serve --port 8811` → tabs Today · Sessions · Trends · Leverage. Candidate tracking is out of the UI for now (user decision); it can be added later. Sherpa colors (warm background, white cards), worktree timeline with analyst hover summaries, analyst narrative (cached), Ask-AI sidebar (steps, subagents, citations, peek, quick-reply tiles), chat becomes a drawer < 1440 px. Verdict = `report.facts()` (single source with `scorer report`). Judge labels carry an "uncalibrated" chip. Lab/scratch/temp sessions excluded at ingest by default (`--include-lab` to opt in).
Demo: `uv run scorer ingest && uv run scorer judge --day today && uv run scorer serve --port 8811` → open http://127.0.0.1:8811/.

## Since then (landed 2026-10-14)
Judge v3 positive episodes + `C+:<episode>` metrics + report line 6 (`edbf5d0`) · concise analyst answers, no-speculation rule, maxOutputTokens 2500 (`53f4a94`) · dashboard mockup A layout port, plain metric names, episode chips, demo-data badge, chat ported from the Mastra dummy page, `SCORER_DB` / `--db` (`93c8891`) · attribution rules, definition v2, `V_recovered` (`f160daf`) · lab `LAB_SET=v1|v2`, `compare`, stable-idle wait; run4 in `~/.scorer-lab/scorer.db` (`3fcf24a`, `4053428`) · merged origin/main with Ari's `ari-version/` (never modify it) (`9514601`) · `/api/attribution` tooling bucket fix (`03d9926`). Mastra QA findings: all 11 fixed (`cc6de95`/`42e0783`).

## Pending (open now)
- Live chat streaming in the ported chat panel: being verified.
- Turn chip: moving to the batch tier + recovered.
- Lab DB (`~/.scorer-lab/scorer.db`) needs a re-ingest after `f160daf`.
- Codex hooks still need manual trust approval in Codex.
- Zoom on Trends/Sessions, wheel zoom and range filtering: not done.
- Demo DB: after any judge prompt change, realign with `python demo/seed/make_demo_db.py --from ~/.scorer/scorer.db --out demo/scorer-demo.db` (the judge cache is keyed by model + prompt digest).
