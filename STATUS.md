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
