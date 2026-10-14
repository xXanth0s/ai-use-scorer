# AI Use Scorer

## scorer (Python harness, `scorer/`)

`scorer` measures how you work with coding agents (Claude Code, Codex). The agent's own performance is out of scope. It reads local transcripts and hook events into SQLite (`~/.scorer/scorer.db`), then computes metrics and serves a dashboard with an analyst chat:
- edits checked by tests;
- edits you made by hand;
- time agents waited on you;
- corrections by cause, labelled by an LLM judge.

```sh
uv sync
uv run scorer install --agent claude      # merge hooks into ~/.claude/settings.json (shows the diff)
uv run scorer ingest                      # parse transcripts + hook events, compute metrics
uv run scorer judge                       # label corrections / strengths (needs ANTHROPIC_API_KEY)
uv run scorer report                      # markdown daily report
uv run scorer serve --port 8811           # dashboard on http://127.0.0.1:8811/
uv run pytest -q                          # tests
```

Use `--db PATH` or `SCORER_DB=PATH` to point `serve`, `report` or `judge` at another database, such as a demo copy. `uv run scorer uninstall --agent claude --yes` removes the hooks.

Details:
- [`HANDOFF.md`](./HANDOFF.md): architecture, file map, known issues and the demo script.
- [`STATUS.md`](./STATUS.md): current progress.
- `docs/spec/`: the specification.
- `agent-mastra/`: the optional Mastra analyst sidecar.

## The app

The Next.js app ("how do you ai"), the Codex/Claude Code capture plugin and the hosted Eve analysis service live at the repo root; see [`../README.md`](../README.md). Run the commands above from this `scorer/` directory. `uv run scorer export-hdyai --day D` writes into the app's `../.local/sessions.sqlite`.
