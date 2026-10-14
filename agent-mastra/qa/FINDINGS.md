# Mastra harness QA — findings (2026-10-14)

Data: `.backup` copy of ~/.scorer/scorer.db (run 2: 83 sessions, 545 turns). No prompt text below.

## Summary
- Dummy chat page `public/index.html`: SSE chunking, XSS escaping, markdown, citation pills (valid / invalid), nested subagent steps, Enter/Shift+Enter, Stop, history — pass in headless Chromium. Live: 8 steps, 1 subagent (4 nested), 13 pills, 0 invalid.
- Tools (`tool_checks.py`): 130/136 — list_sessions 20/20, get_metric 25/25, get_turn 26/28, query_db 11/11, write/escape rejects 26/26, query_db privacy 16/20, tool privacy 2/2, HTTP 4/4. Wrapper parity with /api/tools confirmed.
- Chats (`chat_quality.py`, G = groundedness, H = helpfulness): a 5/4, b 4/4, c 5/4 (turnInvestigator, budget enforced), d 4/4 (no sqlAnalyst delegation), e (injection) 5/3 refused + clipped, f 5/5, g 4/3. All citations valid, no >80-char leaks. Python fallback equivalent.
- Abort: trace shows `aborted` within ~1 s; /health ok.

## Bugs
1. **Med · agent.py `_SENSITIVE`/`_guarded`** — `metric_evidence.note` (paths, up to 110 chars), `session.cwd`/`repo` (87), `session.branch`, `git_change.files/author` not clipped to 80 (only the 200 cap).
2. **Low-Med · translate.ts `translate` + agent.py `_loop`** — text written before a tool call is concatenated into the answer without a break ("…days.Your…").
3. **Low · agent.py `_get_turn`** — `file_edits` silently cut to 40, no total.
4. **Low · server.ts `makeServer`** — no CORS / OPTIONS (same-origin only).
5. **Low · chat.ts `runChat`** — stats line missing on some aborts (`opts.stats` not in `finally`).
6. **Low · translate.ts** — thinking-step `agent` field dropped; nesting by event window only.
7. **Info · serve.py `sanitize_event`** — tool-start SQL clipped to 80 → not auditable/replayable via /api/chat.
8. **Info · agent.py `_query_db`** — `group_concat(prompt_text)` yields 200-char cells of clipped fragments (no leak, but breaks the "no string > 80" rule).
9. **Low (quality) · system prompt** — analyst claims the DB "only stores 80 chars" (false; tools clip).
10. **Low (quality) · `_list_sessions`** — UTC timestamps (answers quote UTC); no citations.
11. **Info (quality) · agents.ts `DELEGATION_SECTION`** — quantitative questions don't delegate to sqlAnalyst unless asked.

## Re-run
```
export QA_DIR=/tmp/scorer-qa                    # keep outside the repo (chat logs hold ≤80-char excerpts)
agent-mastra/qa/stack.sh start                  # DB copy; sidecar :4190, scorer(mastra) :8790, scorer(python) :8791
open "http://127.0.0.1:4190/?day=$(date +%F)"
uv run python agent-mastra/qa/tool_checks.py --url http://127.0.0.1:8790 --db $QA_DIR/scorer.db
uv run python agent-mastra/qa/chat_quality.py --db $QA_DIR/scorer.db --out $QA_DIR/chats --traces $QA_DIR/home/traces [--only a+b,c,d,e,f,g,d-direct,a-direct,a+b-python] [--reanalyse]
python3 agent-mastra/qa/abort_check.py http://127.0.0.1:4190 $QA_DIR/home/traces
agent-mastra/qa/stack.sh stop
```
chat_quality.py needs ANTHROPIC_API_KEY (~15 analyst turns per full run).
