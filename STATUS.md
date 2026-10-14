# STATUS

## M0 — skeleton (done)
Works: uv project, CLI surface, shared contract (`model.py`, `db.py` DDL, `ingest.write_bundle`), stubs for classify/prices.
Stubbed: everything behind the CLI commands.
Demo: `uv run scorer --help`.

## M1 — hooks + event CLI (done)
Works: `scorer event` (silent, exit 0, O_APPEND, ~56 ms / ~91 ms at Stop with git snapshot), `scorer install|uninstall` (merge, diff-first, `.bak`, idempotent, hardened `2>/dev/null || true`); real Claude E2E passes (`scripts/demo_e2e.sh`, all 5 events, Stop git.head == repo HEAD, no injected output, stop_hook_summary clean).
Stubbed/unverified: Codex hook payloads (Codex 0.160 trust gate — user must approve hooks interactively); real `~/.claude/settings.json` install awaits user OK (demo uses `--settings scripts/demo.json`).
Demo: pane 1 `export SCORER_HOME=/tmp/scorer-demo; tail -f $SCORER_HOME/events.jsonl | jq -c '{at,event,tool:.payload.tool_name}'`; pane 2 `SCORER_HOME=/tmp/scorer-demo PROMPT="say hi, then run 'ls' with the Bash tool" scripts/demo_e2e.sh`.
