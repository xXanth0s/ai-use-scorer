"""Analyst agent (build-brief 7b): chat over the scorer DB with Anthropic tool use.

Public API (serve.py / report.py call these):
  has_key() -> bool
  stream_chat(conn, messages, day) -> Iterator[dict]   # text-delta | tool-start | tool-result | error | done
  narrative(conn, day, facts) -> str | None            # markdown, cites turns as [turn:<id>]
  run_tool(conn, name, input) -> dict                  # {ok, data, summary, citations}

Privacy: prompt_text / args_summary / paths leave the DB only via model.clip(..., 80), including
tool results sent to the model. query_db runs on a separate read-only connection where those
columns are only reachable through clipping temp views (direct reads are denied by an authorizer).

Threading: every tool opens its own read-only connection from the DB file path, which is resolved
eagerly when stream_chat() is called, so the returned generator may be consumed on another thread.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from typing import Any, Iterator

from .db import DDL, latest_run_id
from .model import clip, local_day, parse_ts

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_MODEL = "claude-sonnet-5-5"
MAX_STEPS = 8
MAX_TOKENS = 4000          # per step; thinking (always on for Opus 5.5) counts against it
EFFORT = "low"
ROW_CAP = 200
QUERY_TIMEOUT_S = 2.0
NO_KEY_MSG = "no API key — the deterministic report still works"

_INFISICAL_CMD = [
    "infisical", "secrets", "get", "ANTHROPIC_API_KEY",
    "--projectId=8354a384-a191-4230-a254-898dc2cd1449", "--env=dev", "--path=/apps/mastra-agent",
    "--domain=https://eu.infisical.com", "--plain", "--silent",
]
_CACHE: dict[str, Any] = {}

# Raw columns that must only surface clipped. (table, column)
_SENSITIVE = {
    ("turn", "prompt_text"), ("tool_call", "args_summary"), ("tool_call", "initiator_rule"),
    ("file_edit", "path"), ("file_edit", "manifest_dir"), ("event", "data"),
    ("session", "transcript_path"),
}
_CLIP_RESULT_COLS = {"prompt_text", "args_summary", "path"}


# --- key ------------------------------------------------------------------------------------------

def _infisical_key() -> str | None:
    """One Infisical lookup, 5 s timeout. Never prints or logs the value."""
    try:
        r = subprocess.run(_INFISICAL_CMD, capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    v = (r.stdout or "").strip()
    return v if v.startswith("sk-") and "\n" not in v else None


def _resolve_key() -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    if "infisical" not in _CACHE:
        _CACHE["infisical"] = _infisical_key()
    return _CACHE["infisical"]


def has_key() -> bool:
    return bool(_resolve_key())


def _models() -> tuple[str, ...]:
    """(primary, fallback): primary = config analyst_model (default claude-opus-5-5)."""
    try:
        from .config import load
        primary = load().analyst_model or DEFAULT_MODEL
    except Exception:
        primary = DEFAULT_MODEL
    return (primary,) if primary == FALLBACK_MODEL else (primary, FALLBACK_MODEL)


def __getattr__(name: str):
    if name == "MODEL":          # agent.MODEL -> the configured primary model (read by serve.py)
        return _models()[0]
    if name == "MODELS":
        return _models()
    raise AttributeError(name)


def _today() -> str:
    return local_day(datetime.now().astimezone()) or datetime.now().strftime("%Y-%m-%d")


def _make_client(key: str):
    import anthropic
    return anthropic.Anthropic(api_key=key, max_retries=1, timeout=120.0)


# --- system prompt ----------------------------------------------------------------------------------

_DEFINITIONS = """Principle: score the human's choices, not the agent. Every action carries initiator = human | agent | default.

## Scored metrics (metrics-v1 §1)
- V — Verification after changes: share of edit batches followed by a *relevant* test/build/typecheck before the next edit batch; final state verified? Only initiator=human counts in candidate mode. Counter-metric V_fail_share: share of verifications that fail or cause a follow-up edit (0% = decorative). Source: hooks + Bash intent classifier + git snapshot. Tier: fact.
- C — Correction episodes: each correction labelled on 3 axes: event (correction / new requirement / clarification / uncertain), cause (instruction violated / info missing / requirement changed / env failure / unknown), detection (human review / automated check / agent self-review / unknown). Scored by cause, never by count. Source: LLM judge with quoted evidence and abstention. In this build the judge may be pending (no rows) or a regex stand-in — say so.
- H — Hand-edits outside the agent: git delta per turn minus agent edit calls (incl. Bash writes). Split H_repair (repair of agent-touched files) vs H_own (own work). Without git snapshots at Stop it is partial: unit "edits(partial)" = only userModified edits seen in transcripts.
- B — Blocked-agent minutes (B_min): per session, Stop → next prompt while another session is active; plus "attended gaps" (gap < 2 min and prompt ≤ 5 words).
- E — Response to evidence (candidate): after a failing test / contradiction: investigate, revise, or ship anyway, over the next 2 turns. Judge; usually not computed.

## Diagnostics (shown, not scored)
Agent-active time & overlap (active_s = union wall time; overlap_s/parallel_s = time with ≥ 2 sessions); between-turn gaps bucketed gap_bucket:lt30s | 30s-2m | 2-5m | 5-15m | break (> 15 min, excluded), gap_median_s, switch_share (a prompt in another session during the gap); interrupts + thrown_s (agent time before each interrupt); context_p50/context_p90 (% of model window per request); orientation_s (SessionStart → first file change); subagents, compactions, model_mix:<model>, tokens_in/tokens_cached/tokens_out, cost_usd (usage × price table), unpriced_calls, tool_calls, tool_error_share, turns, sessions. V_tier:<verified_pass|verified_fail|inspected|none> counts batches per tier; V_final = share of sessions whose last edit batch is verified; V_human = share of verifications the human asked for; V_fail_share. H_repair / H_own split H when git snapshots exist. Per-model breakdowns: cost_usd:<model>, tokens:<model>; tooling:* = tool-usage diagnostics. Metric ids with a ':' suffix are families — get_metric accepts the prefix (e.g. "V_tier", "tooling", "cost_usd").

## Definitions (metrics-v1 §3)
Prompt = user entry, not meta / compact summary / sidechain, text not starting with "<" (slash commands count).
Turn start = prompt ts (dequeue ts if queued). End precedence: Stop-hook ts → turn_duration (start+durationMs) → last assistant/tool_result ts flagged crash. An interrupt marker ends the interval at the marker ts (turn.end_source = interrupt; event kind=interrupt).
Resumed sessions split at local midnight for day attribution. Subagents fold into the parent turn. Queued prompt: prompt_kind=queued, gap 0. Permission wait: inside the turn.
ActiveTime: union of (turn.start, turn.end) over all sessions/agents of the day; overlap = time covered by ≥ 2 distinct sessions.
Gaps: for consecutive turns of a session, gap = next.start − t.end; prompt in another session during the gap → switched; > 15 min → break (excluded).
Verify: batch = maximal run of file edits not split by a prompt or a test|build Bash call. verified(batch) = Bash with intent test|build, relevant, after the batch and before the next prompt, WITH a tool result. Tier: verified_pass | verified_fail | inspected (git diff / Read of the edited file) | none. final = last batch verified and no later edit.
Initiator precedence: 1 executed by hook → default; 2 inside a skill/slash command → default; 3 this turn's prompt requests the action class (test|run|lint|verify) → human; 4 CLAUDE.md/AGENTS.md requests it → default (config); 5 else agent.
Bash intent: split on && ; |, strip cd/env/npx/uv run wrappers, classify first token; precedence test > build > edit > git > run > search > read. test = pytest|vitest|jest|cargo test|go test|npm test…; build = tsc|mypy|pyright|eslint|ruff|cargo build/check|go build/vet|npm run build/lint/typecheck…; edit = redirects, sed -i, tee, mv/cp/rm/mkdir/touch.
Relevant test: nearest manifest dir (package.json|pyproject.toml|go.mod|Cargo.toml) of the edited files vs cwd + path args.

## Storage
Times are ISO-8601 UTC strings. A "day" is the report day: the configured timezone shifted by day_cutoff (default 04:00 — a turn at 02:30 belongs to the previous day). Never derive days with substr(prompt_at,1,10); use the day values that list_sessions / get_metric return (metric scope_id). metric_value rows belong to an ingest run: always filter run_id = (SELECT max(id) FROM ingest_run). scope_type is "day" (scope_id YYYY-MM-DD local), "session" (session id) or "all". metric_evidence links metric_value rows to turns/tool calls with a short note.
"""

_SYSTEM = f"""<identity>
You are the analyst for one human's own coding-agent usage (Claude Code and Codex sessions on their machine), embedded next to their daily scorer dashboard. Principle: score the human's choices, not the agent. You explain the numbers, find the turns behind them, and suggest one concrete change in how the human drives the agents.
</identity>

<definitions>
{_DEFINITIONS}
SQLite schema (query_db is read-only; turn.prompt_text, tool_call.args_summary, file_edit.path and other free-text columns are already clipped to 80 chars — that is deliberate privacy, do not try to work around it):
{DDL.strip()}
</definitions>

<tools>
- list_sessions(day): sessions active that day — start here for "what did I do".
- get_metric(metric, day, scope_type): metric values from the latest run plus evidence turns, each with a citation number n.
- get_turn(turn_id): one turn in detail (clipped prompt, timings, tool calls, edits, evidence); it gets a citation number n.
- query_db(sql, limit): anything else, single SELECT/WITH, ≤ 200 rows. Rows that include a turn_id column get citation numbers.
</tools>

<grounding>
Every number you state must come from a tool result in this conversation; never estimate or invent. Cite the evidence with its citation number in square brackets right after the claim, e.g. "3 of 8 edit batches were never tested [2][3]". Use only the n values that tool results gave you. If data is missing or stubbed, say so plainly (e.g. H is partial without git snapshots; C = judge pending; no turns for that day). If a tool returns an error, adjust or say what you could not check.
</grounding>

<style>
Answer first in one or two sentences, then at most a few short bullets of evidence, then one concrete next action the human can take tomorrow. Plain markdown, no headings for short answers. Never quote prompts or commands beyond the 80-char excerpts the tools give you. Refer to sessions by repo name, not raw ids.
</style>

<stop_conditions>
Usually ≤ 4 tool calls; hard cap {MAX_STEPS} model steps. Prefer one well-aimed query over many. Never repeat an identical tool call — reuse the earlier result. When you have enough to answer, answer.
</stop_conditions>
"""

_STR = {"type": "string"}
TOOLS = [
    {
        "name": "query_db",
        "description": "Run one read-only SQL SELECT (or WITH … SELECT) against the scorer SQLite DB. "
                       "Free-text columns come back clipped to 80 chars. Returns {columns, rows, truncated}. "
                       "Filter metric_value by run_id = (SELECT max(id) FROM ingest_run).",
        "input_schema": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "A single SELECT/WITH statement."},
            "limit": {"type": "integer", "description": "Max rows (1-200, default 50)."},
        }, "required": ["sql"]},
    },
    {
        "name": "get_turn",
        "description": "Details of one turn: session (agent, repo), clipped prompt, timings, interrupted, "
                       "tool calls (intent, clipped args, errors), API calls summary, file edits, metric evidence.",
        "input_schema": {"type": "object", "properties": {"turn_id": {"type": "integer"}},
                         "required": ["turn_id"]},
    },
    {
        "name": "get_metric",
        "description": "Values of one metric id (e.g. V, H, B_min, active_s, context_p90, or a prefix like "
                       "gap_bucket / V_tier / model_mix) from the latest run, with up to 20 evidence turns.",
        "input_schema": {"type": "object", "properties": {
            "metric": _STR,
            "day": {"type": "string", "description": "Local YYYY-MM-DD; default = the day on screen."},
            "scope_type": {"type": "string", "enum": ["day", "session", "all"]},
        }, "required": ["metric"]},
    },
    {
        "name": "list_sessions",
        "description": "Sessions active on a local day: id, agent, repo, turns, first/last prompt, active seconds.",
        "input_schema": {"type": "object", "properties": {
            "day": {"type": "string", "description": "Local YYYY-MM-DD; default = the day on screen."}}},
    },
]


# --- DB helpers -------------------------------------------------------------------------------------

def _db_file(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            return row[2] or ""
    return ""


def _rows(c: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    cur = c.execute(sql, args)
    cols = [d[0] for d in cur.description or ()]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class _Src:
    """Where tools read from: a DB file path (preferred) or, for in-memory DBs, the given conn."""

    def __init__(self, conn_or_path):
        if isinstance(conn_or_path, sqlite3.Connection):
            self.path = _db_file(conn_or_path)
            self.conn = None if self.path else conn_or_path
        else:
            self.path, self.conn = str(conn_or_path), None

    def open(self) -> sqlite3.Connection:
        if self.conn is not None:
            return self.conn
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2)

    def close(self, c: sqlite3.Connection) -> None:
        if c is not self.conn:
            c.close()


def _base(p: str | None) -> str:
    if not p:
        return ""
    return os.path.basename(str(p).rstrip("/")) or str(p)


def _secs(a: str | None, b: str | None) -> float | None:
    x, y = parse_ts(a), parse_ts(b)
    return round((y - x).total_seconds(), 1) if x and y else None


# --- query_db -----------------------------------------------------------------------------------------

def _strip_sql(sql: str) -> str:
    """Remove comments and string literals (for statement-shape checks only)."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif ch in ("'", '"', "`", "["):
            close = "]" if ch == "[" else ch
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            out.append(" x ")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _check_sql(sql: str) -> str | None:
    if not isinstance(sql, str) or not sql.strip():
        return "sql must be a non-empty string"
    bare = _strip_sql(sql).strip().rstrip(";").strip()
    if ";" in bare:
        return "only a single statement is allowed"
    first = (bare.split(None, 1) or [""])[0].upper()
    if first not in ("SELECT", "WITH"):
        return "only SELECT / WITH queries are allowed"
    return None


_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)
_DENIED_FUNCS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}


def _authorizer(action, a1, a2, dbname, src):
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and src is None and dbname != "temp" and (a1, a2) in _SENSITIVE:
        return sqlite3.SQLITE_DENY   # only reachable through the clipping temp views
    if action == sqlite3.SQLITE_FUNCTION and (a2 or "").lower() in _DENIED_FUNCS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _guarded(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    c.create_function("_clip80", 1, lambda s: None if s is None else clip(s, 80), deterministic=True)
    for table in sorted({t for t, _ in _SENSITIVE}):
        cols = [r[1] for r in c.execute(f"PRAGMA main.table_info({table})").fetchall()]
        if not cols:
            continue
        sel = ", ".join(f"_clip80({col}) AS {col}" if (table, col) in _SENSITIVE else col for col in cols)
        c.execute(f"CREATE TEMP VIEW {table} AS SELECT {sel} FROM main.{table}")
    c.execute("PRAGMA query_only=ON")
    c.set_authorizer(_authorizer)
    return c


def _query_db(src: _Src, sql: Any, limit: Any = 50) -> dict:
    err = _check_sql(sql)
    if err:
        return {"error": err}
    try:
        limit = max(1, min(ROW_CAP, int(limit if limit is not None else 50)))
    except (TypeError, ValueError):
        limit = 50
    if not src.path:
        return {"error": "query_db needs a file-backed database"}
    try:
        c = _guarded(src.path)
    except sqlite3.Error as e:
        return {"error": f"cannot open database read-only: {e}"}
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    try:
        cur = c.execute(sql.strip().rstrip(";"))
        cols = [d[0] for d in cur.description or ()]
        raw = cur.fetchmany(limit + 1)
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "interrupted" in msg:
            return {"error": f"query exceeded {QUERY_TIMEOUT_S:.0f}s and was cancelled"}
        if "prohibited" in msg or "not authorized" in msg:
            return {"error": f"{msg} (read-only; raw free-text columns are only available clipped via the "
                             "unqualified table names, not main.<table>)"}
        return {"error": msg}
    except (sqlite3.Error, sqlite3.Warning, ValueError) as e:
        return {"error": str(e)}
    finally:
        c.close()
    rows = []
    for r in raw[:limit]:
        row = []
        for col, v in zip(cols, r):
            if isinstance(v, bytes):
                v = f"<{len(v)} bytes>"
            if isinstance(v, str) and (col.lower() in _CLIP_RESULT_COLS or len(v) > 200):
                v = clip(v, 80) if col.lower() in _CLIP_RESULT_COLS else clip(v, 200)
            row.append(v)
        rows.append(row)
    return {"columns": cols, "rows": rows, "truncated": len(raw) > limit}


# --- get_turn ----------------------------------------------------------------------------------------

def _get_turn(src: _Src, turn_id: Any) -> dict:
    try:
        turn_id = int(turn_id)
    except (TypeError, ValueError):
        return {"error": "turn_id must be an integer"}
    c = src.open()
    try:
        t = _rows(c, "SELECT t.*, s.agent, s.repo, s.cwd FROM turn t LEFT JOIN session s ON s.id = t.session_id"
                     " WHERE t.id = ?", (turn_id,))
        if not t:
            return {"error": f"turn {turn_id} not found"}
        t = t[0]
        interrupted = bool(_rows(c, "SELECT 1 AS x FROM event WHERE turn_id = ? AND kind = 'interrupt' LIMIT 1",
                                 (turn_id,)))
        calls = _rows(c, "SELECT id, name, kind, bash_intent, initiator, args_summary, is_error, has_result,"
                         " duration_ms, sidechain_id FROM tool_call WHERE turn_id = ? ORDER BY started_at, id",
                      (turn_id,))
        api = _rows(c, "SELECT model, input_tok, cached_tok, output_tok, context_window, cost_usd FROM api_call"
                       " WHERE turn_id = ?", (turn_id,))
        edits = _rows(c, "SELECT f.path, f.lines_added, f.lines_removed, f.user_modified FROM file_edit f"
                         " JOIN tool_call c ON c.id = f.tool_call_id WHERE c.turn_id = ?", (turn_id,))
        rid = latest_run_id(c) if _has_table(c, "metric_value") else None
        ev = _rows(c, "SELECT v.metric, v.scope_type, v.scope_id, e.note FROM metric_evidence e"
                      " JOIN metric_value v ON v.id = e.metric_value_id WHERE e.turn_id = ? AND v.run_id = ?"
                      " LIMIT 30", (turn_id, rid)) if rid is not None else []
    finally:
        src.close(c)
    occ = [100.0 * ((a["input_tok"] or 0) + (a["cached_tok"] or 0)) / a["context_window"]
           for a in api if a["context_window"]]
    models: dict[str, int] = {}
    for a in api:
        models[a["model"] or "unknown"] = models.get(a["model"] or "unknown", 0) + 1
    cost = [a["cost_usd"] for a in api if a["cost_usd"] is not None]
    data = {
        "turn_id": turn_id,
        "session": {"id": t["session_id"], "agent": t["agent"], "repo": _base(t["repo"]), "cwd": _base(t["cwd"])},
        "idx": t["idx"],
        "prompt": clip(t["prompt_text"], 80),
        "prompt_kind": t["prompt_kind"],
        "initiator": t["initiator"],
        "local_day": local_day(t["prompt_at"]),
        "prompt_at": t["prompt_at"],
        "agent_started_at": t["agent_started_at"],
        "agent_finished_at": t["agent_finished_at"],
        "end_source": t["end_source"],
        "duration_s": _secs(t["prompt_at"], t["agent_finished_at"]),
        "interrupted": interrupted,
        "tool_calls_total": len(calls),
        "tool_calls": [{"id": x["id"], "name": x["name"], "kind": x["kind"], "bash_intent": x["bash_intent"],
                        "initiator": x["initiator"], "args": clip(x["args_summary"], 80),
                        "is_error": bool(x["is_error"]), "has_result": bool(x["has_result"]),
                        "duration_ms": x["duration_ms"], "subagent": bool(x["sidechain_id"])}
                       for x in calls[:40]],
        "api_calls": {"count": len(api), "models": models,
                      "input_tok": sum(a["input_tok"] or 0 for a in api),
                      "cached_tok": sum(a["cached_tok"] or 0 for a in api),
                      "output_tok": sum(a["output_tok"] or 0 for a in api),
                      "context_pct_max": round(max(occ), 1) if occ else None,
                      "cost_usd": round(sum(cost), 4) if cost else None},
        "file_edits": [{"file": clip(_base(e["path"]), 80), "lines": f"+{e['lines_added'] or 0}/-{e['lines_removed'] or 0}",
                        "user_modified": bool(e["user_modified"])} for e in edits[:40]],
        "metric_evidence": [{"metric": e["metric"], "scope": f"{e['scope_type']}:{e['scope_id']}",
                             "note": clip(e["note"], 80)} for e in ev],
    }
    label = f"turn {turn_id} · {_base(t['repo']) or t['agent'] or ''} · {clip(t['prompt_text'], 40)}"
    cite = [{"turn_id": turn_id, "session_id": t["session_id"], "label": clip(label, 80), "_slot": data}]
    summary = (f"turn {turn_id} · {len(calls)} tool calls · {len(edits)} edits"
               + (" · interrupted" if interrupted else ""))
    return {"data": data, "summary": summary, "cites": cite}


def _has_table(c, name) -> bool:
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


# --- get_metric ----------------------------------------------------------------------------------------

def _get_metric(src: _Src, metric: Any, day: Any = None, scope_type: Any = "day", default_day: str | None = None) -> dict:
    if not isinstance(metric, str) or not metric.strip():
        return {"error": "metric must be a non-empty string"}
    metric = metric.strip()
    scope_type = scope_type if scope_type in ("day", "session", "all") else "day"
    day = day if isinstance(day, str) and day.strip() else None
    if day and day.lower() == "today":
        day = _today()
    if scope_type == "day" and not day:
        day = default_day
    c = src.open()
    try:
        rid = latest_run_id(c)
        if rid is None:
            return {"error": "no ingest run yet — run `scorer ingest`"}
        names = [r["metric"] for r in _rows(c, "SELECT DISTINCT metric FROM metric_value WHERE run_id = ?", (rid,))]
        pref = metric.rstrip(":")
        wanted = [m for m in names if m == metric] or [m for m in names if m.startswith(pref + ":")]
        if not wanted:
            bases = sorted({m.split(":")[0] for m in names})
            hint = " (C and E are judge metrics — judge pending)" if pref in ("C", "E") else ""
            return {"error": f"metric {metric!r} not found in run {rid}{hint}", "available": bases[:80]}
        ph = ",".join("?" * len(wanted))
        args: list = [rid, *wanted, scope_type]
        sql = (f"SELECT id, metric, scope_type, scope_id, value, unit, n FROM metric_value WHERE run_id = ?"
               f" AND metric IN ({ph}) AND scope_type = ?")
        if scope_type == "day" and day:
            sql += " AND scope_id = ?"
            args.append(day)
        sql += " ORDER BY scope_id DESC, metric LIMIT 60"
        vals = _rows(c, sql, args)
        if not vals and scope_type == "day" and day:
            return {"error": f"no {metric} value for day {day} in run {rid}",
                    "days_with_values": [r["scope_id"] for r in _rows(
                        c, f"SELECT DISTINCT scope_id FROM metric_value WHERE run_id = ? AND metric IN ({ph})"
                           " AND scope_type = 'day' ORDER BY scope_id DESC LIMIT 14", [rid, *wanted])]}
        focus = vals[0]["scope_id"] if vals else None
        ids = [v["id"] for v in vals if v["scope_id"] == focus]
        ev = []
        if ids:
            ev = _rows(c, f"SELECT e.metric_value_id, e.turn_id, e.tool_call_id, e.note, t.session_id, s.repo"
                          f" FROM metric_evidence e LEFT JOIN turn t ON t.id = e.turn_id"
                          f" LEFT JOIN session s ON s.id = t.session_id"
                          f" WHERE e.metric_value_id IN ({','.join('?' * len(ids))}) LIMIT 20", ids)
    finally:
        src.close(c)
    mname = {v["id"]: v["metric"] for v in vals}
    evidence, cites = [], []
    for e in ev:
        row = {"metric": mname.get(e["metric_value_id"]), "turn_id": e["turn_id"],
               "tool_call_id": e["tool_call_id"], "note": clip(e["note"], 80)}
        evidence.append(row)
        if e["turn_id"] is not None:
            cites.append({"turn_id": e["turn_id"], "session_id": e["session_id"],
                          "label": clip(f"turn {e['turn_id']} · {_base(e['repo'])} · {row['note']}", 80), "_slot": row})
    data = {"metric": metric, "run_id": rid, "scope_type": scope_type,
            "values": [{"metric": v["metric"], "scope_id": v["scope_id"], "value": v["value"], "unit": v["unit"],
                        "n": v["n"]} for v in vals],
            "evidence_scope": focus, "evidence": evidence}
    if any(v["unit"] == "edits(partial)" for v in vals):
        data["note"] = "H is partial: no git snapshots, only userModified edits counted"
    head = vals[0] if vals else None
    summary = (f"{metric} {focus}: {head['value']:.3g} {head['unit'] or ''} (n={head['n']}) · {len(evidence)} evidence"
               if head and len(wanted) == 1 else f"{metric}: {len(vals)} values · {len(evidence)} evidence")
    return {"data": data, "summary": summary, "cites": cites}


# --- list_sessions -------------------------------------------------------------------------------------

def _list_sessions(src: _Src, day: Any = None, default_day: str | None = None) -> dict:
    day = day if isinstance(day, str) and day.strip() else default_day
    if not day or day.lower() == "today":
        day = _today()
    c = src.open()
    try:
        rows = _rows(c, "SELECT t.session_id, t.prompt_at, t.agent_finished_at, s.agent, s.repo, s.cwd"
                        " FROM turn t LEFT JOIN session s ON s.id = t.session_id WHERE t.prompt_at IS NOT NULL")
    finally:
        src.close(c)
    by: dict[str, dict] = {}
    for r in rows:
        if local_day(r["prompt_at"]) != day:
            continue
        s = by.setdefault(r["session_id"], {"id": r["session_id"], "agent": r["agent"],
                                            "repo": _base(r["repo"] or r["cwd"]), "turns": 0,
                                            "first_ts": r["prompt_at"], "last_ts": r["prompt_at"], "active_s": 0.0})
        s["turns"] += 1
        s["first_ts"] = min(s["first_ts"], r["prompt_at"])
        s["last_ts"] = max(s["last_ts"], r["agent_finished_at"] or r["prompt_at"])
        d = _secs(r["prompt_at"], r["agent_finished_at"])
        if d and d > 0:
            s["active_s"] += d
    out = sorted(by.values(), key=lambda s: s["first_ts"])
    for s in out:
        s["active_s"] = round(s["active_s"], 1)
    return {"data": {"day": day, "sessions": out},
            "summary": f"{len(out)} sessions on {day}", "cites": []}


# --- dispatch + citations ----------------------------------------------------------------------------

def _dispatch(src: _Src, name: str, inp: dict, default_day: str | None) -> dict:
    if not isinstance(inp, dict):
        return {"error": "tool input must be an object"}
    try:
        if name == "query_db":
            r = _query_db(src, inp.get("sql"), inp.get("limit", 50))
            if "error" in r:
                return r
            cites = []
            cols = [col.lower() for col in r["columns"]]
            if "turn_id" in cols:
                i = cols.index("turn_id")
                seen = set()
                for row in r["rows"]:
                    tid = row[i]
                    if isinstance(tid, int) and tid not in seen and len(seen) < 20:
                        seen.add(tid)
                        cites.append({"turn_id": tid, "session_id": None, "label": f"turn {tid}"})
            return {"data": r, "summary": f"{len(r['rows'])} rows" + (" (truncated)" if r["truncated"] else ""),
                    "cites": cites}
        if name == "get_turn":
            return _get_turn(src, inp.get("turn_id"))
        if name == "get_metric":
            return _get_metric(src, inp.get("metric"), inp.get("day"), inp.get("scope_type", "day"), default_day)
        if name == "list_sessions":
            return _list_sessions(src, inp.get("day"), default_day)
        return {"error": f"unknown tool {name!r}"}
    except sqlite3.Error as e:
        return {"error": f"database error: {e}"}
    except Exception as e:  # tools never raise into the loop
        return {"error": f"{type(e).__name__}: {clip(str(e), 120)}"}


def _number(r: dict, n0: int) -> tuple[dict, list[dict]]:
    """Assign citation numbers n0, n0+1, … and write them into the model-facing data."""
    cites = []
    turn_n: dict[int, int] = {}
    for c in r.get("cites", []):
        slot = c.pop("_slot", None)
        tid = c.get("turn_id")
        if tid in turn_n:          # same turn twice in one result -> same number
            n = turn_n[tid]
        else:
            n = n0 + len(cites)
            turn_n[tid] = n
            cites.append({"n": n, **c})
        if slot is not None:
            slot["n"] = n
    if r.get("data", {}).get("rows") is not None and cites:   # query_db: map turn_id -> n
        r["data"]["citations"] = {str(c["turn_id"]): c["n"] for c in cites}
    return r, cites


def run_tool(conn, name: str, input: dict, *, n0: int = 1, day: str | None = None) -> dict:
    """Run one tool. Returns {ok, data | error, summary, citations}; never raises."""
    r = _dispatch(_Src(conn), name, input, day)
    if "error" in r:
        return {"ok": False, "error": r["error"], **({"available": r["available"]} if "available" in r else {}),
                **({"days_with_values": r["days_with_values"]} if "days_with_values" in r else {}),
                "summary": f"error: {clip(r['error'], 100)}", "citations": []}
    r, cites = _number(r, n0)
    return {"ok": True, "data": r["data"], "summary": r["summary"], "citations": cites}


# --- loop -----------------------------------------------------------------------------------------

def _fallbackable(e: Exception) -> bool:
    try:
        import anthropic
    except ImportError:
        return False
    if isinstance(e, (anthropic.NotFoundError, anthropic.InternalServerError)):
        return True
    if hasattr(anthropic, "OverloadedError") and isinstance(e, anthropic.OverloadedError):
        return True
    if isinstance(e, anthropic.APIStatusError) and getattr(e, "status_code", 0) >= 500:
        return True
    return isinstance(e, (anthropic.APIConnectionError,))


def _err_text(e: Exception) -> str:
    try:
        import anthropic
        if isinstance(e, anthropic.AuthenticationError):
            return "the API key was rejected"
        if isinstance(e, anthropic.RateLimitError):
            return "rate limited by the API — try again in a minute"
        if isinstance(e, anthropic.APIStatusError):
            return f"API error {e.status_code}: {clip(getattr(e, 'message', str(e)), 160)}"
        if isinstance(e, anthropic.APIConnectionError):
            return "could not reach the Anthropic API"
    except ImportError:
        pass
    return f"{type(e).__name__}: {clip(str(e), 160)}"


def _to_api_messages(messages: list[dict], day: str | None) -> list[dict]:
    out = []
    for m in messages or []:
        role = m.get("role") if isinstance(m, dict) else None
        content = m.get("content") if isinstance(m, dict) else None
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    while out and out[-1]["role"] != "user":
        out.pop()
    if out:
        today = _today()
        ctx = f"\n\n<dashboard_context>day on screen: {day or 'none (all days)'}; today: {today}</dashboard_context>"
        out[-1] = {"role": "user", "content": out[-1]["content"] + ctx}
    return out


def _loop(client, src: _Src, api_messages: list[dict], day: str | None, *, effort: str = EFFORT,
          max_steps: int = MAX_STEPS, citations_out: list | None = None) -> Iterator[dict]:
    models = _models()
    model_i = 0
    model_used = models[0]
    n_next = 1
    seen: dict[str, list[int]] = {}
    msgs = list(api_messages)
    step = 0
    while step < max_steps:
        last = step == max_steps - 1
        params: dict[str, Any] = dict(
            model=models[model_i], max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS, messages=msgs, output_config={"effort": effort},
        )
        if last:
            params["tool_choice"] = {"type": "none"}
        emitted = False
        try:
            with client.messages.stream(**params) as stream:
                for ev in stream:
                    if getattr(ev, "type", None) == "text" and getattr(ev, "text", ""):
                        emitted = True
                        yield {"type": "text-delta", "delta": ev.text}
                final = stream.get_final_message()
        except Exception as e:
            if not emitted and model_i + 1 < len(models) and _fallbackable(e):
                model_i += 1
                continue          # retry the same step on the fallback model
            yield {"type": "error", "message": _err_text(e)}
            yield {"type": "done", "model": model_used}
            return
        model_used = getattr(final, "model", None) or models[model_i]
        content = list(getattr(final, "content", None) or [])
        stop = getattr(final, "stop_reason", None)
        tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
        if stop == "refusal":
            yield {"type": "error", "message": "the model declined to answer this request"}
            break
        if stop == "pause_turn":
            msgs = msgs + [{"role": "assistant", "content": content}]
            step += 1
            continue
        if not tool_uses or stop != "tool_use":
            if stop == "max_tokens" and tool_uses:
                yield {"type": "error", "message": "response was cut off (max tokens) before a tool call completed"}
            break
        if last:
            yield {"type": "error", "message": f"step cap ({max_steps}) reached before a final answer"}
            break
        results = []
        for b in tool_uses:
            inp = b.input if isinstance(b.input, dict) else {}
            yield {"type": "tool-start", "id": b.id, "name": b.name, "input": inp}
            key = f"{b.name}:{json.dumps(inp, sort_keys=True, default=str)}"
            if key in seen:
                r = {"ok": False, "error": "identical call already made in this answer — reuse that result",
                     "earlier_citations": seen[key], "summary": "duplicate call skipped", "citations": []}
            else:
                r = run_tool(src.path or src.conn, b.name, inp, n0=n_next, day=day) if (src.path or src.conn) \
                    else {"ok": False, "error": "no database", "summary": "error: no database", "citations": []}
                seen[key] = [c["n"] for c in r["citations"]]
            n_next += len(r["citations"])
            if citations_out is not None:
                citations_out.extend(r["citations"])
            yield {"type": "tool-result", "id": b.id, "name": b.name, "ok": r["ok"], "summary": r["summary"],
                   "citations": r["citations"]}
            body = {k: v for k, v in r.items() if k not in ("summary", "citations", "ok")}
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(body, default=str, ensure_ascii=False),
                            **({"is_error": True} if not r["ok"] else {})})
        msgs = msgs + [{"role": "assistant", "content": content}, {"role": "user", "content": results}]
        step += 1
    yield {"type": "done", "model": model_used}


def stream_chat(conn, messages: list[dict], day: str | None = None, *, client=None) -> Iterator[dict]:
    """Stream one assistant answer. `conn` is only touched here (to find the DB file), so the returned
    iterator can be consumed on another thread."""
    src = _Src(conn)
    api_messages = _to_api_messages(messages, day)

    def gen() -> Iterator[dict]:
        cl = client
        if cl is None:
            key = _resolve_key()
            if not key:
                yield {"type": "error", "message": NO_KEY_MSG}
                yield {"type": "done", "model": None}
                return
            try:
                cl = _make_client(key)
            except Exception as e:
                yield {"type": "error", "message": _err_text(e)}
                yield {"type": "done", "model": None}
                return
        if not api_messages:
            yield {"type": "error", "message": "empty question"}
            yield {"type": "done", "model": None}
            return
        yield from _loop(cl, src, api_messages, day)

    return gen()


# --- narrative -------------------------------------------------------------------------------------

_CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def _turn_cites(text: str, cites: list[dict]) -> str:
    by_n = {c["n"]: c.get("turn_id") for c in cites}

    def sub(m):
        out = [f"[turn:{by_n[int(x)]}]" for x in re.split(r"\s*,\s*", m.group(1))
               if by_n.get(int(x)) is not None]
        return "".join(out)
    return _CITE_RE.sub(sub, text)


def narrative(conn, day: str, facts: dict, *, client=None) -> str | None:
    """Markdown narrative for the daily report, citing turns as [turn:<id>]. None if no key or on failure."""
    if client is None:
        key = _resolve_key()
        if not key:
            return None
        try:
            client = _make_client(key)
        except Exception:
            return None
    facts_json = json.dumps(facts, default=str, ensure_ascii=False)
    if len(facts_json) > 12000:
        facts_json = facts_json[:12000] + " …(truncated)"
    prompt = (
        f"Write the narrative for my daily scorer report for {day}.\n"
        f"Deterministic facts already computed by scorer (JSON):\n{facts_json}\n\n"
        "Use get_metric (with evidence) to find the turns behind the biggest cost — at most 3 tool calls. "
        "Then output markdown only, ≤ 120 words, exactly three bullets:\n"
        "- **Verdict:** the one thing that cost most today, with its number.\n"
        "- **Tomorrow:** one concrete change, with the evidence line.\n"
        "- **Repeat:** one pattern worth repeating, with evidence.\n"
        "Cite evidence turns with their [n]. No preamble, no headings."
    )
    cites: list[dict] = []
    parts: list[str] = []
    try:
        for ev in _loop(client, _Src(conn), _to_api_messages([{"role": "user", "content": prompt}], day), day,
                        effort="medium", max_steps=5, citations_out=cites):
            if ev["type"] == "text-delta":
                parts.append(ev["delta"])
            elif ev["type"] == "tool-start":
                parts = []        # text before a tool call is preamble; keep the final answer only
            elif ev["type"] == "error":
                return None
    except Exception:
        return None
    text = "".join(parts).strip()
    return _turn_cites(text, cites) or None
