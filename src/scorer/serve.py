"""Local dashboard: stdlib ThreadingHTTPServer over the scorer SQLite DB.

    serve(db: str | None, port: int) -> None     # blocks; Ctrl-C to stop
    make_server(db, port, host="127.0.0.1") -> ThreadingHTTPServer   # tests: port 0

Static files come from ``scorer/static`` (index.html, app.css, app.js, fonts/).
Every request opens its own connection (sqlite3 is not shared across threads), so a slow LLM call
(narrative, summary) only blocks its own request. Metrics come from the latest ``ingest_run``
(db.latest_run_id), except the judge's C-family rows (C, C:*, C_*): ingest's compute_all writes a new
run without them, so those come from the newest run that has C rows for the day.

Day bounds are metrics.day_bounds (config day_cutoff, default 04:00 local), the same bucketing as
model.local_day, /api/days and every stored metric.

Temp/test sessions: a session whose cwd is under a temp dir (/private/var/folders, /var/folders, /tmp,
/private/tmp, tempfile.gettempdir()) or has a path part named scorer-e2e.* / scorer-capture-repo.* is
hidden from /api/day unless ``?include_temp=1``. Hidden sessions also leave computed numbers, hours,
gaps, overlap, scored V / B_min (recomputed without them) and all scored evidence lists.

Privacy: any human text (prompt_text, args_summary, initiator_rule, cwd/repo/branch, paths) leaves this
module only through ``model.clip(..., 80)`` (cwd: ``lclip``, same 80 chars keeping the end). Event
``data`` JSON is never sent. Generated prose (verdict/tomorrow sentences, narrative markdown, summary)
is not human text and may be longer.

JSON endpoints (all GET, ``Cache-Control: no-store``)
-----------------------------------------------------
/api/days
    {"run_id": int|null, "today": "YYYY-MM-DD",
     "days": [{"day", "turns", "sessions"}]}            newest first, local tz
/api/day/<YYYY-MM-DD|today>[?include_temp=1]
    {"day", "run_id", "start", "end",                     # day bounds, ISO UTC
     "include_temp": bool, "temp_hidden": int,           # sessions hidden by the temp filter
     "sessions": [{"id", "short", "agent", "repo", "branch", "cwd", "temp", "parent_id",
         "active_s", "cost_usd", "tokens": {"in", "cached", "out"},
         "models": [{"model", "calls", "tokens", "cost_usd"}],
         "subagents": {"count", "tokens", "cost_usd"},
         "verification": {"batches", "verified_pass", "verified_fail", "inspected", "none"},
         "corrections": int,
         "turns": [{"id", "idx", "start", "end", "open", "kind", "initiator",
                    "interrupted", "clipped_start", "clipped_end", "end_source"}],
         "interrupts": [{"at", "turn_id"}]}],
     "hours": [{"start", "active_s", "waiting_s"}],      # local hours; waiting = non-break gaps - active
     "overlap": [{"start", "end"}],                     # >=2 sessions active
     "gaps": [{"session_id", "start", "end", "seconds", "bucket", "switched"}],
     "computed": {"active_s", "overlap_s", "turns", "sessions", "interrupts"},
     "scored": {"V"|"V_fail_share"|"V_final"|"V_human"|"H"|"B_min":
                    {"value", "unit", "n", "evidence": [{"turn_id", "tool_call_id", "note"}]} | null,
                "C": [{"metric", "label", "value", "unit", "n", "evidence": [...]}]},
     "diagnostics": [{"metric", "base", "key", "value", "unit", "n"}],   # every other day-scoped metric
     "leverage": {"subagents": number|null, "compactions": number|null,  # not scored
                  "subagent_types": [{"name", "runs", "initiators": {"human", "agent"}, "median_s",
                                      "model", "tokens", "cost_usd"}],
                  "tooling": {"categories": {"skill"|"command"|"mcp"|"plugin":
                                  {"human", "agent", "default", "total"}},   # numbers or null
                              "human_share": number|null,
                              "items": [{"category", "name", "initiator", "count"}]}},  # metrics.tooling
     "verdict": {"where", "change", "basis": metric-id|null, "text", "category", "minutes", "evidence_turns"}}
         # = report.facts() verdict + tomorrow (fallback: a short computed sentence when facts() has no data)
/api/turn/<turn_id>
    {"id", "session_id", "agent", "repo", "idx", "prompt", "prompt_kind", "initiator",
     "start", "agent_started_at", "end", "end_source", "interrupted",
     "tier": "verified_pass"|"verified_fail"|"inspected"|"none"|"no_edits", "tier_source",
     "tools": [{"id", "name", "kind", "bash_intent", "initiator", "initiator_rule",
                "args", "started_at", "duration_ms", "is_error", "has_result", "sidechain"}],
     "api": {"calls", "input", "cached", "output", "cost_usd", "unpriced", "models": {model: n},
             "context_peak"},
     "events": [{"kind", "at"}], "metrics": [{"metric", "value", "unit"}]}   # turn-scoped
/api/trend?end=YYYY-MM-DD&days=7
    {"days": [YYYY-MM-DD...], "models": [model...],
     "series": {"V", "V_n", "H", "B_min", "C", "active_h", "overlap_h", "interrupts", "thrown_min",
                "gap_median_s", "context_p50", "context_p90", "tokens_in", "tokens_cached", "tokens_out",
                "subagents", "subagent_tokens", "subagent_cost_usd", "compactions",
                "tooling_human_share", "cost_usd",
                "gap_bucket:<b>", "cost_usd:<model>", "tokens:<model>", "C:cause:<cause>": [number|null...]}}
    null = no data that day (V: no edit batches; H: not measured; C:*: judge has not run).
/api/report/<YYYY-MM-DD|today>
    report.facts(conn, day) with every string clipped to 80 (generated sentences: text, change,
    prevent, judge_doubt up to 400). unverified_now drops temp sessions unless ?include_temp=1;
    corrections come from the newest judged run. 404 {"error": "no data for <day>"} when facts() is None.
/api/narrative?day=YYYY-MM-DD
    {"markdown": str|null, "citations": [{"n", "turn_id", "session_id", "label"}], "cached": bool,
     "model"?, "generated_at"?}   agent.narrative() with [turn:<id>] -> [n]; cached in ui_cache by
    (day, latest run). No key / SCORER_NO_LLM=1 / no data / failure -> {"markdown": null}. 501 without agent.
/api/summary?session_id=<id>[&turn_id=<id>]
    {"summary": str, "cached": bool}  1-2 sentences from config judge_model (Anthropic API, 15 s
    timeout); input is clipped prompts (<= 80 chars, <= 15 turns), tool intents + counts, tiers,
    file basenames, durations. Cached in ui_cache by (session, turn, latest run).
    404 no such session/turn or no API key; 502 on an API failure.
/api/actions?day=YYYY-MM-DD
    {"day", "available": bool, "items": [{"text" (<=160), "category", "count", "turn_ids", "example_quote" (<=80),
     "claude_md_line" (<=160)|null, "uncalibrated", "source": "judge"|"metrics"}], "error"?}
    from judge.action_items(conn, day); available=false (empty items) until that function exists.
GET /api/chat/status
    {"available": bool, "model": str|null, "backend": "python"|"mastra", "reason": str|null}
GET /api/agent/system-prompt
    {"system_prompt": str, "today": "YYYY-MM-DD", "max_steps": int}   # agent._SYSTEM, for agent-mastra/
POST /api/tools/<query_db|get_turn|get_metric|list_sessions>   body {"input": {...}, "n0"?: int, "day"?}
    agent.run_tool(conn, name, input, n0=n0, day=day) -> {ok, data|error, summary, citations}
    The Mastra sidecar's only data access (read-only, free text clipped to 80 chars).
POST /api/chat   body {"messages": [{"role": "user"|"assistant", "content": str}], "day"?: "YYYY-MM-DD"}
    Server-Sent Events (text/event-stream), one `data: <json>\n\n` per event from
    scorer.agent.stream_chat(conn, messages, day):
      {"type": "text-delta", "delta"}
      {"type": "tool-start", "id", "name", "input", "subagent_id"?}   # string inputs clipped to 80
      {"type": "tool-result", "id", "name", "ok", "summary",       # summary/labels clipped to 80
       "citations": [{"n", "turn_id", "session_id", "label"}]}
      {"type": "subagent-start", "id", "name", "task", "parent_id"?}  # mastra backend only
      {"type": "subagent-result", "id", "name", "ok", "summary", "citations"}
      {"type": "suggestions", "items": [{"label", "prompt"}]}     # <= 4; label <= 40, prompt <= 200; before done
      {"type": "error", "message"}   {"type": "done", "model"?, "backend"}   # stream always ends with done
    A tool-start without its own subagent_id/parent_id gets the innermost open subagent's id.
    If SCORER_MASTRA_URL is set and <url>/health answers within 300 ms, the stream is proxied from
    <url>/chat (agent-mastra/); otherwise agent.stream_chat serves it. Both pass EventMapper.
    501 {"error": "not_implemented"} when scorer.agent is not importable; 400 on a bad body.
Unknown paths -> 404 {"error"}; bad day/turn -> 400/404.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import statistics
import tempfile
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import cache as uic
from . import db as dbm
from . import metrics as mx
from . import prices
from .model import clip, iso, local_day, local_tz, parse_ts

STATIC = Path(__file__).parent / "static"
CLIP = 80
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCORED = ("V", "V_fail_share", "V_final", "V_human", "H", "B_min")
GAP_BUCKETS = ((30, "<30s"), (120, "30s-2m"), (300, "2-5m"), (900, "5-15m"))
BREAK_S = 900
SUMMARY_TIMEOUT_S = 15.0
SUMMARY_MAX_TURNS = 15
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml",
         ".txt": "text/plain; charset=utf-8", ".json": "application/json"}


# --- helpers ---------------------------------------------------------------
def _basename(p: str | None) -> str:
    if not p:
        return ""
    return clip(os.path.basename(p.rstrip("/")) or p, CLIP)


def lclip(text: str | None, n: int = CLIP) -> str:
    """Like model.clip (single line, <= n chars) but keeps the END: for paths whose tail matters."""
    if not text:
        return ""
    s = " ".join(str(text).split())
    return s if len(s) <= n else "…" + s[-(n - 1):]


def _temp_roots() -> tuple[str, ...]:
    roots = {"/private/var/folders/", "/var/folders/", "/tmp/", "/private/tmp/"}
    try:
        t = os.path.realpath(tempfile.gettempdir()).rstrip("/") + "/"
        if t not in ("/", ""):
            roots.add(t)
    except Exception:
        pass
    return tuple(roots)


TEMP_ROOTS = _temp_roots()
TEMP_NAME_RE = re.compile(r"^(scorer-e2e|scorer-capture-repo)\.")


def is_temp(cwd: str | None) -> bool:
    """Test/temp session: cwd under a temp dir, or a scorer e2e/capture scratch repo."""
    if not cwd:
        return False
    c = cwd.rstrip("/") + "/"
    if c.startswith(TEMP_ROOTS):
        return True
    return any(TEMP_NAME_RE.match(p) for p in cwd.split("/"))


def day_bounds(day: str) -> tuple[datetime, datetime]:
    """Report day [start, end): metrics.day_bounds (day_cutoff in the configured tz)."""
    return mx.day_bounds(day)


def _merge(iv):
    out: list[list[datetime]] = []
    for s, e in sorted(iv):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _subtract(iv, cut):
    out = []
    for a, b in iv:
        for c, e in cut:
            if e <= a or c >= b:
                continue
            if c > a:
                out.append((a, c))
            a = max(a, e)
            if a >= b:
                break
        if a < b:
            out.append((a, b))
    return out


def _within(iv, a, b) -> float:
    return sum(max(0.0, (min(e, b) - max(s, a)).total_seconds()) for s, e in iv)


def union_len(iv: list[tuple[datetime, datetime]]) -> float:
    return sum((e - s).total_seconds() for s, e in _merge(iv))


def overlap_intervals(by_session: dict[str, list[tuple[datetime, datetime]]]) -> list[tuple[datetime, datetime]]:
    """Intervals where >= 2 distinct sessions are active (sweep line)."""
    pts: list[tuple[datetime, int, str]] = []
    for sid, iv in by_session.items():
        for s, e in _merge(iv):   # merge each session's own intervals first so one session never counts twice
            pts += [(s, 1, sid), (e, -1, sid)]
    pts.sort(key=lambda p: (p[0], p[1]))
    out, active, since = [], 0, None
    for t, delta, _ in pts:
        before = active
        active += delta
        if before < 2 <= active:
            since = t
        elif before >= 2 > active and since is not None:
            if t > since:
                out.append((since, t))
            since = None
    return out


def _bucket(sec: float) -> str:
    for lim, name in GAP_BUCKETS:
        if sec < lim:
            return name
    return "break"


def _fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "—"
    m = int(round(sec / 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _json_obj(raw) -> dict:
    try:
        d = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _turn_sessions(conn, ids) -> dict[int, str]:
    ids = [i for i in set(ids) if i is not None]
    out: dict[int, str] = {}
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        q = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT id, session_id FROM turn WHERE id IN ({q})", part):
            out[r[0]] = r[1]
    return out


# --- queries ---------------------------------------------------------------
def _metrics(conn, run_id, scope_type, scope_id):
    if run_id is None:
        return []
    return conn.execute(
        "SELECT id, metric, value, unit, n FROM metric_value WHERE run_id=? AND scope_type=? AND scope_id=?"
        " ORDER BY metric", (run_id, scope_type, str(scope_id))).fetchall()


def _is_c(metric: str) -> bool:
    return metric == "C" or metric.startswith(("C:", "C_"))


def _c_run(conn, day: str) -> int | None:
    """Newest run with judge (C-family) rows for this day: compute_all re-runs drop them."""
    r = conn.execute("SELECT max(run_id) FROM metric_value WHERE scope_type='day' AND scope_id=?"
                     " AND (metric='C' OR metric LIKE 'C:%' OR substr(metric, 1, 2)='C_')", (day,)).fetchone()
    return r[0] if r else None


def day_rows(conn, run_id, day: str) -> list:
    """Day-scope metric rows: latest run, with the C family from the newest run that has it."""
    rows = [r for r in _metrics(conn, run_id, "day", day) if not _is_c(r["metric"])]
    cr = _c_run(conn, day)
    if cr is not None:
        rows += [r for r in _metrics(conn, cr, "day", day) if _is_c(r["metric"])]
    return sorted(rows, key=lambda r: r["metric"])


def _evidence(conn, mv_ids):
    out = defaultdict(list)
    if not mv_ids:
        return out
    q = ",".join("?" * len(mv_ids))
    for r in conn.execute(f"SELECT metric_value_id, turn_id, tool_call_id, note FROM metric_evidence"
                          f" WHERE metric_value_id IN ({q}) LIMIT 4000", mv_ids):
        lst = out[r["metric_value_id"]]
        if len(lst) < 200:
            lst.append({"turn_id": r["turn_id"], "tool_call_id": r["tool_call_id"], "note": clip(r["note"], CLIP)})
    return out


def _turn_end(conn, t) -> tuple[datetime, bool]:
    end = parse_ts(t["agent_finished_at"])
    if end:
        return end, False
    cands = [parse_ts(t["agent_started_at"]), parse_ts(t["prompt_at"])]
    r = conn.execute("SELECT max(at) FROM api_call WHERE turn_id=?", (t["id"],)).fetchone()
    cands.append(parse_ts(r[0]) if r else None)
    r = conn.execute("SELECT max(started_at) FROM tool_call WHERE turn_id=?", (t["id"],)).fetchone()
    cands.append(parse_ts(r[0]) if r else None)
    best = max(c for c in cands if c)
    return max(best, parse_ts(t["prompt_at"]) + timedelta(seconds=30)), True


def list_days(conn) -> dict:
    counts: dict[str, dict] = {}
    for r in conn.execute("SELECT session_id, prompt_at FROM turn WHERE prompt_at IS NOT NULL"):
        d = local_day(r["prompt_at"])
        if not d:
            continue
        c = counts.setdefault(d, {"day": d, "turns": 0, "sessions": set()})
        c["turns"] += 1
        c["sessions"].add(r["session_id"])
    days = [{"day": d, "turns": c["turns"], "sessions": len(c["sessions"])}
            for d, c in sorted(counts.items(), reverse=True)]
    return {"run_id": dbm.latest_run_id(conn), "today": local_day(datetime.now(timezone.utc)), "days": days}


def _day_turns(conn, day):
    start, end = day_bounds(day)
    lo, hi = iso(start - timedelta(days=2)), iso(end)
    rows = conn.execute(
        "SELECT t.*, s.agent, s.repo, s.cwd, s.branch, s.forked_from AS s_forked FROM turn t"
        " JOIN session s ON s.id=t.session_id"
        " WHERE t.prompt_at >= ? AND t.prompt_at < ? ORDER BY t.prompt_at", (lo, hi)).fetchall()
    out = []
    for t in rows:
        s = parse_ts(t["prompt_at"])
        if not s:
            continue
        e, open_ = _turn_end(conn, t)
        if e <= start or s >= end:
            continue
        out.append((t, s, e, open_))
    return out, start, end


def computed_day(conn, day) -> dict:
    turns, start, end = _day_turns(conn, day)
    by_s = defaultdict(list)
    for t, s, e, _ in turns:
        by_s[t["session_id"]].append((max(s, start), min(e, end)))
    allv = [iv for v in by_s.values() for iv in v]
    ov = overlap_intervals(by_s)
    return {"active_s": round(union_len(allv)), "overlap_s": round(sum((e - s).total_seconds() for s, e in ov)),
            "turns": len(turns), "sessions": len(by_s)}


def hours_block(allv, gaps, day_start, day_end) -> list[dict]:
    """Per local hour: agent-active seconds (union) and waiting seconds (gaps < 15 min, minus active)."""
    act = _merge(allv)
    if not act:
        return []
    wait = _merge([(parse_ts(g["start"]), parse_ts(g["end"])) for g in gaps if g["seconds"] < BREAK_S])
    wait = _subtract(wait, act)
    h = max(act[0][0], day_start).astimezone(local_tz()).replace(minute=0, second=0, microsecond=0)
    h = h.astimezone(timezone.utc)
    stop = min(act[-1][1], day_end)
    out = []
    while h < stop:
        h2 = h + timedelta(hours=1)
        out.append({"start": iso(h), "active_s": round(_within(act, h, h2)), "waiting_s": round(_within(wait, h, h2))})
        h = h2
    return out


def _api_cost(r) -> float | None:
    c = r.get("cost")
    if c is None:
        c = r.get("cost_usd")
    return c


SPAWN_TOOLS = ("Agent", "Task", "spawn_agent")


def subagent_runs(conn, day: str, api: list[dict] | None = None) -> list[dict]:
    """One row per subagent run on `day` (event kind=subagent): Claude by data.agent_id (end = last
    sidechain api/tool activity), Codex by data.agent_thread_id (started..completed; tokens from the
    child session). initiator = the spawning Agent/Task/spawn_agent call (by tool_use_id, else the
    nearest earlier one in the turn), else the turn's initiator: approximate, human | agent."""
    api = mx.api_rows(conn, day) if api is None else api
    runs: dict[str, dict] = {}
    for e in conn.execute("SELECT e.id, e.session_id, e.turn_id, e.at, e.data, t.prompt_at, t.initiator AS t_init"
                          " FROM event e LEFT JOIN turn t ON t.id=e.turn_id WHERE e.kind='subagent'"):
        if local_day(e["prompt_at"] or e["at"]) != day:
            continue
        d = _json_obj(e["data"])
        codex = bool(d.get("agent_thread_id"))
        key = d.get("agent_id") or d.get("agent_thread_id") or d.get("tool_use_id") or f"event:{e['id']}"
        r = runs.get(key)
        if r is None:
            r = runs[key] = {"key": key, "session_id": e["session_id"], "turn_id": e["turn_id"],
                             "agent": "codex" if codex else "claude",
                             "name": clip(d.get("agent_type") or ("codex subagent" if codex else "subagent"), 40),
                             "model": d.get("model"), "start": None, "end": None, "tool_use_id": None,
                             "t_init": e["t_init"]}
        at = parse_ts(e["at"])
        if at:
            if d.get("kind") == "completed":
                r["end"] = max(r["end"], at) if r["end"] else at
            else:
                r["start"] = min(r["start"], at) if r["start"] else at
        if d.get("tool_use_id"):
            r["tool_use_id"] = d["tool_use_id"]
        if r["turn_id"] is None:
            r["turn_id"] = e["turn_id"]
            r["t_init"] = e["t_init"]
    if not runs:
        return []
    side: dict[str, list] = defaultdict(list)      # claude: sidechain_id -> api rows
    child: dict[str, list] = defaultdict(list)     # codex: child session id -> api rows
    for a in api:
        if a.get("sidechain_id"):
            side[a["sidechain_id"]].append(a)
        child[a.get("session_id")].append(a)
    keys = list(runs)
    q = ",".join("?" * len(keys))
    tool_end: dict[str, datetime] = {}
    for c in conn.execute(f"SELECT sidechain_id, started_at, duration_ms FROM tool_call WHERE sidechain_id IN ({q})", keys):
        t = parse_ts(c["started_at"])
        if t:
            t = t + timedelta(milliseconds=c["duration_ms"] or 0)
            tool_end[c["sidechain_id"]] = max(tool_end.get(c["sidechain_id"], t), t)
    for r in runs.values():
        rows = child.get(r["key"], []) if r["agent"] == "codex" else side.get(r["key"], [])
        if r["agent"] == "claude":
            ends = [parse_ts(a.get("at")) for a in rows] + [tool_end.get(r["key"])]
            ends = [x for x in ends if x]
            if ends and r["start"]:
                r["end"] = max(ends)
        if not r["model"] and rows:
            r["model"] = Counter(a.get("model") or "unknown" for a in rows).most_common(1)[0][0]
        r["tokens"] = sum((a.get("input_tok") or 0) + (a.get("cached_tok") or 0) + (a.get("output_tok") or 0)
                          for a in rows)
        cs = [_api_cost(a) for a in rows if _api_cost(a) is not None]
        r["cost_usd"] = round(sum(cs), 4) if cs else None
        r["duration_s"] = (r["end"] - r["start"]).total_seconds() if r["start"] and r["end"] else None
        init = None
        if r["tool_use_id"]:
            x = conn.execute("SELECT initiator FROM tool_call WHERE tool_use_id=?", (r["tool_use_id"],)).fetchone()
            init = x[0] if x else None
        if init is None and r["turn_id"] is not None:
            cands = conn.execute(f"SELECT initiator, started_at FROM tool_call WHERE turn_id=? AND sidechain_id IS NULL"
                                 f" AND name IN ({','.join('?' * len(SPAWN_TOOLS))}) ORDER BY started_at",
                                 (r["turn_id"], *SPAWN_TOOLS)).fetchall()
            before = [c for c in cands if r["start"] and parse_ts(c["started_at"]) and parse_ts(c["started_at"]) <= r["start"]]
            pick = (before or cands or [None])[-1 if before else 0]
            init = pick["initiator"] if pick else r["t_init"]
        r["initiator"] = "human" if init == "human" else "agent"
    return list(runs.values())


def subagent_types(runs: list[dict]) -> list[dict]:
    by: dict[str, list] = defaultdict(list)
    for r in runs:
        by[r["name"]].append(r)
    out = []
    for name, rs in by.items():
        ds = [r["duration_s"] for r in rs if r["duration_s"] is not None]
        models = Counter(r["model"] for r in rs if r["model"])
        cs = [r["cost_usd"] for r in rs if r["cost_usd"] is not None]
        out.append({"name": clip(name, 40), "runs": len(rs),
                    "initiators": {"human": sum(r["initiator"] == "human" for r in rs),
                                   "agent": sum(r["initiator"] != "human" for r in rs)},
                    "median_s": round(statistics.median(ds)) if ds else None,
                    "model": clip(models.most_common(1)[0][0], 40) if models else None,
                    "tokens": sum(r["tokens"] for r in rs), "cost_usd": round(sum(cs), 4) if cs else None})
    return sorted(out, key=lambda x: (-x["runs"], x["name"]))


def _session_extras(conn, day, sessions: dict, by_s, api, batches, runs, c_turns: Counter) -> None:
    """active_s, cost/tokens/models, subagents, verification and corrections per visible session."""
    parent = {r[0]: r[1] for r in conn.execute(
        "SELECT id, forked_from FROM session WHERE agent='codex' AND forked_from IS NOT NULL")}
    per_api: dict[str, list] = defaultdict(list)
    for a in api:
        per_api[a.get("session_id")].append(a)
    per_runs: dict[str, list] = defaultdict(list)
    for r in runs:
        per_runs[r["session_id"]].append(r)
    per_b: dict[str, Counter] = defaultdict(Counter)
    for b in batches:
        per_b[b.session_id][b.tier] += 1
        per_b[b.session_id]["batches"] += 1
    for sid, sess in sessions.items():
        rows = per_api.get(sid, [])
        cs = [_api_cost(a) for a in rows if _api_cost(a) is not None]
        mix: dict[str, dict] = {}
        for a in rows:
            m = mix.setdefault(a.get("model") or "unknown", {"calls": 0, "tokens": 0, "cost": []})
            m["calls"] += 1
            m["tokens"] += (a.get("input_tok") or 0) + (a.get("cached_tok") or 0) + (a.get("output_tok") or 0)
            if _api_cost(a) is not None:
                m["cost"].append(_api_cost(a))
        rs = per_runs.get(sid, [])
        rcost = [r["cost_usd"] for r in rs if r["cost_usd"] is not None]
        vb = per_b.get(sid, Counter())
        sess.update({
            "active_s": round(union_len(by_s.get(sid, []))),
            "cost_usd": round(sum(cs), 4) if cs else None,
            "tokens": {"in": sum(a.get("input_tok") or 0 for a in rows),
                       "cached": sum(a.get("cached_tok") or 0 for a in rows),
                       "out": sum(a.get("output_tok") or 0 for a in rows)},
            "models": sorted(({"model": clip(k, 40), "calls": v["calls"], "tokens": v["tokens"],
                               "cost_usd": round(sum(v["cost"]), 4) if v["cost"] else None}
                              for k, v in mix.items()), key=lambda x: -x["tokens"]),
            "subagents": {"count": len(rs), "tokens": sum(r["tokens"] for r in rs),
                          "cost_usd": round(sum(rcost), 4) if rcost else None},
            "verification": {"batches": vb["batches"], "verified_pass": vb["verified_pass"],
                             "verified_fail": vb["verified_fail"], "inspected": vb["inspected"], "none": vb["none"]},
            "corrections": c_turns.get(sid, 0),
            "parent_id": clip(parent.get(sid), 64) or None,
        })


def day_payload(conn, day, include_temp: bool = False) -> dict:
    run_id = dbm.latest_run_id(conn)
    turns, start, end = _day_turns(conn, day)
    temp_ids = {t["session_id"] for t, *_ in turns if is_temp(t["cwd"])}
    hidden = set() if include_temp else temp_ids
    turns = [x for x in turns if x[0]["session_id"] not in hidden]
    sessions: dict[str, dict] = {}
    by_s = defaultdict(list)
    for t, s, e, open_ in turns:
        sid = t["session_id"]
        sess = sessions.get(sid)
        if sess is None:
            sess = sessions[sid] = {"id": sid, "short": sid[:8], "agent": t["agent"],
                                    "repo": _basename(t["repo"] or t["cwd"]), "branch": clip(t["branch"], 40),
                                    "cwd": lclip(t["cwd"]), "temp": sid in temp_ids,
                                    "turns": [], "interrupts": []}
        cs, ce = max(s, start), min(e, end)
        by_s[sid].append((cs, ce))
        sess["turns"].append({"id": t["id"], "idx": t["idx"], "start": iso(cs), "end": iso(ce), "open": open_,
                              "kind": t["prompt_kind"], "initiator": t["initiator"], "interrupted": False,
                              "clipped_start": s < start, "clipped_end": e > end,
                              "end_source": t["end_source"]})
    # interrupts: events + interrupt-kind prompts
    tid_map = {tt["id"]: tt for sess in sessions.values() for tt in sess["turns"]}
    if sessions:
        q = ",".join("?" * len(sessions))
        for r in conn.execute(f"SELECT session_id, turn_id, at FROM event WHERE kind='interrupt' AND session_id IN ({q})",
                              list(sessions)):
            at = parse_ts(r["at"])
            if at and start <= at < end:
                sessions[r["session_id"]]["interrupts"].append({"at": iso(at), "turn_id": r["turn_id"]})
                if r["turn_id"] in tid_map:
                    tid_map[r["turn_id"]]["interrupted"] = True
    for sess in sessions.values():
        for tt in sess["turns"]:
            if tt["kind"] == "interrupt" and not any(i["turn_id"] == tt["id"] for i in sess["interrupts"]):
                sess["interrupts"].append({"at": tt["start"], "turn_id": tt["id"]})
    # gaps
    gaps = []
    all_prompts = sorted((s, t["session_id"]) for t, s, _, _ in turns)
    for sid, sess in sessions.items():
        tl = sess["turns"]
        for a, b in zip(tl, tl[1:]):
            if a["open"]:
                continue
            gs, ge = parse_ts(a["end"]), parse_ts(b["start"])
            sec = (ge - gs).total_seconds()
            if sec <= 0:
                continue
            switched = any(gs < p < ge and osid != sid for p, osid in all_prompts)
            gaps.append({"session_id": sid, "start": iso(gs), "end": iso(ge), "seconds": round(sec),
                         "bucket": _bucket(sec), "switched": switched})
    ov = overlap_intervals(by_s)
    allv = [iv for v in by_s.values() for iv in v]
    computed = {"active_s": round(union_len(allv)), "overlap_s": round(sum((e - s).total_seconds() for s, e in ov)),
                "turns": len(turns), "sessions": len(sessions),
                "interrupts": sum(len(s["interrupts"]) for s in sessions.values())}
    # metrics
    rows = day_rows(conn, run_id, day)
    ev = _evidence(conn, [r["id"] for r in rows if r["metric"] in SCORED or r["metric"].startswith("C")])
    scored: dict = {k: None for k in SCORED}
    scored["C"] = []
    diagnostics = []
    for r in rows:
        m = r["metric"]
        base, _, key = m.partition(":")
        item = {"metric": m, "value": r["value"], "unit": r["unit"], "n": r["n"]}
        if m in SCORED:
            scored[m] = {**item, "evidence": ev.get(r["id"], [])}
        elif base == "C" or m == "C":
            scored["C"].append({**item, "label": key or "episodes", "evidence": ev.get(r["id"], [])})
        else:
            diagnostics.append({**item, "base": base, "key": key})
    api = mx.api_rows(conn, day)
    batches = mx.verification_batches(conn, day)
    runs = subagent_runs(conn, day, api)
    ev_tids = [e["turn_id"] for x in [*scored.values()] if isinstance(x, dict) for e in x["evidence"]]
    ev_tids += [e["turn_id"] for c in scored["C"] for e in c["evidence"]]
    tsess = _turn_sessions(conn, ev_tids)
    if hidden:
        _drop_hidden(conn, day, scored, tsess, hidden, batches)
        batches = [b for b in batches if b.session_id not in hidden]
        runs = [r for r in runs if r["session_id"] not in hidden]
    c_turns: Counter = Counter()
    for c in scored["C"]:
        if c["metric"].startswith("C:cause:") or (c["metric"] == "C" and not any(
                x["metric"].startswith("C:cause:") for x in scored["C"])):
            for tid in {e["turn_id"] for e in c["evidence"]}:
                if tsess.get(tid):
                    c_turns[tsess[tid]] += 1
    _session_extras(conn, day, sessions, by_s, api, batches, runs, c_turns)
    order = sorted(sessions.values(), key=lambda s: s["turns"][0]["start"])
    leverage = leverage_block(conn, rows, day, runs)
    dvals = {d["metric"]: d["value"] for d in diagnostics}
    if hidden:   # stored day totals include the hidden sessions: use the computed ones
        for k in ("active_s", "overlap_s"):
            dvals.pop(k, None)
    return {"day": day, "run_id": run_id, "start": iso(start), "end": iso(end),
            "include_temp": include_temp, "temp_hidden": len(temp_ids) if not include_temp else 0,
            "sessions": order, "hours": hours_block(allv, gaps, start, end),
            "overlap": [{"start": iso(s), "end": iso(e)} for s, e in ov], "gaps": gaps, "computed": computed,
            "scored": scored, "diagnostics": diagnostics, "leverage": leverage,
            "verdict": (facts_verdict(conn, day) if computed["turns"] else None) or verdict(computed, scored, dvals)}


def facts_verdict(conn, day) -> dict | None:
    """The day verdict from report.facts() (single source of truth with `scorer report` and /api/report):
    the largest of blocked min, thrown-away min, unverified batches x 5 min, hand repairs x 3 min."""
    from . import report
    try:
        f = report.facts(conn, day)
    except Exception:
        return None
    if not f:
        return None
    v, tm = f["verdict"], f["tomorrow"]
    return {"where": clip(v["text"], 400), "change": clip(tm["change"], 400), "basis": v["metric"],
            "text": clip(v["text"], 400), "category": v.get("category"), "minutes": v.get("minutes"),
            "evidence_turns": list(tm.get("evidence_turns") or [])[:50]}


def _drop_hidden(conn, day, scored, tsess, hidden, batches) -> None:
    """Remove hidden (temp) sessions from scored evidence; recompute V and B_min without them."""
    def keep(e):
        return tsess.get(e["turn_id"]) not in hidden
    for k, x in list(scored.items()):
        if isinstance(x, dict):
            x["evidence"] = [e for e in x["evidence"] if keep(e)]
    for c in scored["C"]:
        c["evidence"] = [e for e in c["evidence"] if keep(e)]
    vis = [b for b in batches if b.session_id not in hidden]
    if len(vis) != len(batches):
        if not vis:
            scored["V"] = None
        elif scored.get("V"):
            ver = sum(b.tier in ("verified_pass", "verified_fail") for b in vis)
            scored["V"].update(value=ver / len(vis), n=len(vis))
    bl = mx.blocked(conn, day)
    vb = [b for b in bl if b["session_id"] not in hidden]
    if len(vb) != len(bl) and scored.get("B_min"):
        scored["B_min"].update(value=sum(b["blocked_s"] for b in vb) / 60.0, n=len(vb))


TOOL_CATS = ("skill", "command", "mcp", "plugin")
TOOL_INITS = ("human", "agent", "default")
NOTE_RE = re.compile(r"^(skill|command|mcp|plugin):(.+):(human|agent|default) x(\d+)$")


def leverage_block(conn, rows, day: str | None = None, runs: list[dict] | None = None) -> dict:
    """Subagents + tooling use (skills/commands/MCP/plugins by initiator). Diagnostic, not scored."""
    vals = {r["metric"]: r["value"] for r in rows}
    cats = {}
    for c in TOOL_CATS:
        d = {i: vals.get(f"tooling:{c}:{i}") for i in TOOL_INITS}
        tot = vals.get(f"tooling:{c}")
        if tot is None and any(v is not None for v in d.values()):
            tot = sum(v or 0 for v in d.values())
        cats[c] = {**d, "total": tot}
    items: Counter = Counter()
    tl = mx.tooling(conn, day) if day else []
    if tl:   # complete per-name counts from the tooling events
        for x in tl:
            items[(x["category"], clip(x["name"], 60), x["initiator"])] += 1
    else:    # legacy: evidence notes "<cat>:<name>:<initiator> xN"
        ids = [r["id"] for r in rows if r["metric"].startswith("tooling")]
        if ids:
            q = ",".join("?" * len(ids))
            for r in conn.execute(f"SELECT DISTINCT note FROM metric_evidence WHERE metric_value_id IN ({q})", ids):
                m = NOTE_RE.match((r["note"] or "").strip())
                if m:
                    k = (m.group(1), clip(m.group(2), 60), m.group(3))
                    items[k] = max(items[k], int(m.group(4)))
    if runs is None:
        runs = subagent_runs(conn, day) if day else []
    return {"subagents": vals.get("subagents"), "compactions": vals.get("compactions"),
            "subagent_types": subagent_types(runs),
            "tooling": {"categories": cats, "human_share": vals.get("tooling_human_share"),
                        "items": [{"category": c, "name": n, "initiator": i, "count": k}
                                  for (c, n, i), k in sorted(items.items(), key=lambda kv: (-kv[1], kv[0]))][:500]}}


def verdict(computed, scored, diag) -> dict:
    active = diag.get("active_s", computed["active_s"])
    overlap = diag.get("overlap_s", computed["overlap_s"])
    if not computed["turns"]:
        return {"where": "No agent activity recorded on this day.", "change": "Nothing to change.", "basis": None}
    where = (f"{_fmt_dur(active)} of agent time across {computed['sessions']} session"
             f"{'s' if computed['sessions'] != 1 else ''}, {_fmt_dur(overlap)} of it in parallel.")
    val = lambda k: (scored.get(k) or {}).get("value")  # noqa: E731
    b, v, h, thrown = val("B_min"), val("V"), val("H"), diag.get("thrown_s")
    if b is not None and b >= 10:
        return {"where": where, "basis": "B_min",
                "change": f"Agents sat blocked {round(b)} min waiting on you: answer the stopped session before starting new work."}
    if v is not None and v < 0.7:
        share = round((1 - v) * 100)
        return {"where": where, "basis": "V",
                "change": f"{share}% of edit batches went unverified: run the tests before the next prompt."}
    if h is not None and h > 0:
        return {"where": where, "basis": "H",
                "change": f"{round(h)} hand-edits outside the agent: say what you fixed so the agent learns it."}
    if thrown and thrown >= 300:
        return {"where": where, "basis": "thrown_s",
                "change": f"{_fmt_dur(thrown)} of agent work thrown away by interrupts: state the goal before it starts."}
    if v is None and b is None:
        return {"where": where, "basis": None, "change": "No scored data yet: run scorer ingest to compute metrics."}
    return {"where": where, "basis": None, "change": "Nothing stands out today: keep verifying before you move on."}


def _tier(tools) -> str:
    """Spec §3 tier for one turn, derived from its tool calls (fallback when no turn metric)."""
    edit_seen, tier = False, None
    for c in tools:
        is_edit = c["kind"] == "edit" or c["bash_intent"] == "edit"
        if is_edit:
            edit_seen, tier = True, None
        elif edit_seen and c["bash_intent"] in ("test", "build") and c["has_result"]:
            tier = "verified_fail" if c["is_error"] else "verified_pass"
        elif edit_seen and tier is None and (c["kind"] == "read" or (c["bash_intent"] == "git")):
            tier = "inspected"
    if not edit_seen:
        return "no_edits"
    return tier or "none"


def turn_payload(conn, turn_id: int) -> dict | None:
    t = conn.execute("SELECT t.*, s.agent, s.repo, s.cwd FROM turn t JOIN session s ON s.id=t.session_id"
                     " WHERE t.id=?", (turn_id,)).fetchone()
    if not t:
        return None
    tools = conn.execute("SELECT * FROM tool_call WHERE turn_id=? ORDER BY started_at, id", (turn_id,)).fetchall()
    api = conn.execute("SELECT model, input_tok, cached_tok, output_tok, cost_usd, context_window FROM api_call"
                       " WHERE turn_id=?", (turn_id,)).fetchall()
    events = conn.execute("SELECT kind, at FROM event WHERE turn_id=? ORDER BY at", (turn_id,)).fetchall()
    run_id = dbm.latest_run_id(conn)
    tm = _metrics(conn, run_id, "turn", turn_id)
    tier, tier_src = _tier(tools), "derived"
    for r in tm:
        if r["metric"] in ("V_tier", "verify_tier", "tier"):
            tier_src = "metric"
            tier = {1.0: "verified_pass", 0.5: "inspected", 0.0: "none", -1.0: "verified_fail"}.get(r["value"], tier)
    end, open_ = _turn_end(conn, t)
    ctx = [(a["input_tok"] or 0) + (a["cached_tok"] or 0) for a in api]
    return {
        "id": t["id"], "session_id": t["session_id"], "agent": t["agent"], "repo": _basename(t["repo"] or t["cwd"]),
        "idx": t["idx"], "prompt": clip(t["prompt_text"], CLIP), "prompt_kind": t["prompt_kind"],
        "initiator": t["initiator"], "start": t["prompt_at"], "agent_started_at": t["agent_started_at"],
        "end": iso(end), "open": open_, "end_source": t["end_source"],
        "interrupted": any(e["kind"] == "interrupt" for e in events),
        "tier": tier, "tier_source": tier_src,
        "tools": [{"id": c["id"], "name": clip(c["name"], 40), "kind": c["kind"], "bash_intent": c["bash_intent"],
                   "initiator": c["initiator"], "initiator_rule": clip(c["initiator_rule"], CLIP),
                   "args": clip(c["args_summary"], CLIP), "started_at": c["started_at"],
                   "duration_ms": c["duration_ms"], "is_error": bool(c["is_error"]),
                   "has_result": bool(c["has_result"]), "sidechain": bool(c["sidechain_id"])} for c in tools[:200]],
        "tools_total": len(tools),
        "api": {"calls": len(api), "input": sum(a["input_tok"] or 0 for a in api),
                "cached": sum(a["cached_tok"] or 0 for a in api), "output": sum(a["output_tok"] or 0 for a in api),
                "cost_usd": round(sum(a["cost_usd"] or 0 for a in api), 4),
                "unpriced": sum(1 for a in api if a["cost_usd"] is None),
                "models": dict(Counter(clip(a["model"], 40) or "unknown" for a in api)),
                "context_peak": max(ctx) if ctx else None},
        "events": [{"kind": e["kind"], "at": e["at"]} for e in events],
        "metrics": [{"metric": r["metric"], "value": r["value"], "unit": r["unit"]} for r in tm],
    }


_C_NOT_CAUSE = ("C:event:", "C:detection:", "C:defect_caught", "C:resolved")


def c_total(vals: dict) -> float | None:
    """Correction count for one day, counted by cause only (matches report.Metrics.c_total).
    judge.py writes C:cause:<cause> plus C:event:* / C:detection:* / C:defect_caught / C:resolved
    for the same episodes, so summing every C:* row would count each correction ~3x."""
    if "C" in vals:
        return vals["C"]
    causes = [v for k, v in vals.items() if k.startswith("C:cause:")]
    if not causes:
        causes = [v for k, v in vals.items() if k.startswith("C:") and not k.startswith(_C_NOT_CAUSE)]
    if causes:
        return sum(v or 0 for v in causes)
    return 0.0 if any(k.startswith(("C:", "C_")) for k in vals) else None


TREND_PLAIN = ("B_min", "interrupts", "gap_median_s", "context_p50", "context_p90", "tokens_in", "tokens_cached",
               "tokens_out", "subagents", "compactions", "tooling_human_share", "cost_usd")
TREND_PREFIX = ("gap_bucket:", "cost_usd:", "tokens:")


def _subagent_api_by_day(conn, days: set[str]) -> dict[str, list[float]]:
    """day -> [tokens, cost] of api calls that ran inside a subagent (Claude sidechain, Codex child thread)."""
    out: dict[str, list] = {}
    for r in conn.execute("SELECT a.model, a.input_tok, a.cached_tok, a.output_tok, a.cost_usd, t.prompt_at"
                          " FROM api_call a JOIN turn t ON t.id=a.turn_id JOIN session s ON s.id=t.session_id"
                          " WHERE a.sidechain_id IS NOT NULL OR (s.agent='codex' AND s.forked_from IS NOT NULL)"):
        d = local_day(r["prompt_at"])
        if d not in days:
            continue
        x = out.setdefault(d, [0, 0.0])
        x[0] += (r["input_tok"] or 0) + (r["cached_tok"] or 0) + (r["output_tok"] or 0)
        c = r["cost_usd"]
        if c is None:
            c = prices.cost_usd(r["model"], r["input_tok"], r["cached_tok"], 0, r["output_tok"])
        x[1] += c or 0.0
    return out


def trend_payload(conn, end_day: str, n: int) -> dict:
    run_id = dbm.latest_run_id(conn)
    y, m, d = map(int, end_day.split("-"))
    days = [(datetime(y, m, d) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]
    per = []
    for day in days:
        rows = day_rows(conn, run_id, day)
        per.append(({r["metric"]: r["value"] for r in rows}, {r["metric"]: (r["unit"], r["n"]) for r in rows}))
    keys = ("V", "V_n", "H", "C", "active_h", "overlap_h", "thrown_min", "subagent_tokens", "subagent_cost_usd") + TREND_PLAIN
    series: dict[str, list] = {k: [] for k in keys}
    pref = sorted({k for vals, _ in per for k in vals if k.startswith(TREND_PREFIX + ("C:cause:",))})
    for k in pref:
        series[k] = []
    sub = _subagent_api_by_day(conn, set(days))
    for day, (vals, meta) in zip(days, per):
        have = bool(vals)
        vn = (meta.get("V") or (None, None))[1]
        series["V"].append(vals.get("V") if vn else None)      # no edit batches -> no V (a gap, not 0%)
        series["V_n"].append(vn)
        h_unit = (meta.get("H") or ("", None))[0] or ""
        series["H"].append(None if (h_unit.endswith("(partial)") and not vals.get("H")) else vals.get("H"))
        series["C"].append(c_total(vals))
        for k in TREND_PLAIN:
            series[k].append(vals.get(k))
        act = vals.get("active_s")
        ov = vals.get("overlap_s")
        if act is None:
            c = computed_day(conn, day)
            act = c["active_s"] if c["turns"] else None
            ov = c["overlap_s"] if c["turns"] else None
        series["active_h"].append(round(act / 3600, 2) if act is not None else None)
        series["overlap_h"].append(round(ov / 3600, 2) if ov is not None else None)
        th = vals.get("thrown_s")
        series["thrown_min"].append(round(th / 60, 1) if th is not None else None)
        s = sub.get(day)
        series["subagent_tokens"].append(s[0] if s else (0 if have else None))
        series["subagent_cost_usd"].append(round(s[1], 4) if s else (0.0 if have else None))
        judged = any(_is_c(k) for k in vals)
        for k in pref:
            v = vals.get(k)
            if v is None:
                v = (0 if judged else None) if k.startswith("C:cause:") else (0 if have else None)
            series[k].append(v)
    models = sorted({k.split(":", 1)[1] for k in pref if k.startswith("cost_usd:")})
    return {"days": days, "models": [clip(x, 60) for x in models], "series": series}


# --- report / narrative / summary -----------------------------------------------------------
LONG_KEYS = {"text", "change", "prevent", "judge_doubt"}   # generated sentences, not human text


def clip_strings(o, key=None):
    """Every string clipped to 80 (generated-sentence keys to 400); structure kept as is."""
    if isinstance(o, str):
        return clip(o, 400) if key in LONG_KEYS else clip(o, CLIP)
    if isinstance(o, dict):
        return {str(k): clip_strings(v, k) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [clip_strings(v, key) for v in o]
    return o


def report_payload(conn, day, include_temp: bool = False) -> dict | None:
    from . import report
    f = report.facts(conn, day)
    if f is None:
        return None
    cr, latest = _c_run(conn, day), dbm.latest_run_id(conn)
    if cr is not None and cr != latest:   # re-ingest dropped the judge rows: corrections from the judged run
        g = report.facts(conn, day, run_id=cr)
        if g:
            f["corrections"], f["judged"], f["judge_doubt"] = g["corrections"], g["judged"], g["judge_doubt"]
    if not include_temp:   # same default as /api/day: test/temp sessions are not "right now" work
        cwd = {r[0]: r[1] for r in conn.execute("SELECT id, cwd FROM session")}
        f["unverified_now"] = [u for u in f.get("unverified_now") or [] if not is_temp(cwd.get(u.get("session_id")))]
    return clip_strings(f)


TURN_REF = re.compile(r"\[turn:(\d+)\]")


def number_turn_refs(conn, md: str) -> tuple[str, list[dict]]:
    """[turn:<id>] -> [n] (first appearance order) + citations with session_id and a clipped label."""
    order: list[int] = []
    for m in TURN_REF.finditer(md):
        tid = int(m.group(1))
        if tid not in order:
            order.append(tid)
    info = {}
    for k in range(0, len(order), 500):
        part = order[k:k + 500]
        q = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT id, session_id, prompt_text FROM turn WHERE id IN ({q})", part):
            info[r["id"]] = r
    num = {tid: i + 1 for i, tid in enumerate(order)}
    out = TURN_REF.sub(lambda m: f"[{num[int(m.group(1))]}]", md)
    cites = [{"n": num[tid], "turn_id": tid, "session_id": clip(info[tid]["session_id"], 64) if tid in info else None,
              "label": clip(info[tid]["prompt_text"], CLIP) if tid in info else f"turn {tid}"} for tid in order]
    return out, cites


def _no_llm() -> bool:
    return os.environ.get("SCORER_NO_LLM") == "1"


def narrative_payload(conn, day: str, agent) -> dict:
    run_id = dbm.latest_run_id(conn)
    key = uic.make_key("narrative", day, run_id)
    hit = uic.get(conn, key)
    if hit:
        return {**hit["value"], "cached": True}
    empty = {"markdown": None, "citations": [], "cached": False}
    try:
        ok = not _no_llm() and bool(agent.has_key())
    except Exception:
        ok = False
    if not ok:
        return empty
    from . import report
    f = report.facts(conn, day)
    if f is None:
        return empty
    with uic.lock(key):
        hit = uic.get(conn, key)
        if hit:
            return {**hit["value"], "cached": True}
        try:
            md = agent.narrative(conn, day, f)
        except Exception:
            md = None
        if not isinstance(md, str) or not md.strip():
            return empty
        text, cites = number_turn_refs(conn, md.strip())
        val = {"markdown": text, "citations": cites, "model": clip(str(getattr(agent, "MODEL", "") or ""), 40) or None,
               "generated_at": datetime.now(timezone.utc).isoformat()}
        uic.put(conn, key, val)
    return {**val, "cached": False}


def _api_key() -> str | None:
    agent = _load_agent()
    try:
        if agent is not None and hasattr(agent, "_resolve_key"):
            return agent._resolve_key()
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY") or None


def _summary_client(key: str):
    import anthropic
    return anthropic.Anthropic(api_key=key, timeout=SUMMARY_TIMEOUT_S, max_retries=0)


def _intent(c) -> str:
    k = c["kind"] or "other"
    return f"{k}:{c['bash_intent']}" if k == "bash" and c["bash_intent"] else k


def summary_input(conn, session_id: str, turn_id: int | None) -> str:
    """Model input for /api/summary. Human text only as clip(..., 80); paths as basenames."""
    s = conn.execute("SELECT agent, repo, cwd, branch FROM session WHERE id=?", (session_id,)).fetchone()
    if turn_id is not None:
        turns = conn.execute("SELECT * FROM turn WHERE id=? AND session_id=?", (turn_id, session_id)).fetchall()
    else:
        turns = conn.execute("SELECT * FROM turn WHERE session_id=? ORDER BY idx", (session_id,)).fetchall()
    shown = turns[:SUMMARY_MAX_TURNS]
    lines = [f"{'One turn of a' if turn_id is not None else 'A'} {s['agent']} coding-agent session in repo "
             f"{_basename(s['repo'] or s['cwd']) or 'unknown'}" + (f" (branch {clip(s['branch'], 40)})" if s["branch"] else "")
             + f", {len(turns)} turn(s)."]
    files: Counter = Counter()
    total = 0.0
    for t in shown:
        tools = conn.execute("SELECT id, kind, bash_intent, is_error, has_result FROM tool_call WHERE turn_id=?"
                             " ORDER BY started_at, id", (t["id"],)).fetchall()
        end, _ = _turn_end(conn, t)
        start = parse_ts(t["prompt_at"])
        sec = (end - start).total_seconds() if start else 0
        total += max(sec, 0)
        ints = conn.execute("SELECT count(*) FROM event WHERE turn_id=? AND kind='interrupt'", (t["id"],)).fetchone()[0]
        counts = Counter(_intent(c) for c in tools)
        errs = sum(1 for c in tools if c["is_error"])
        lines.append(f"- turn {t['idx']}: prompt \"{clip(t['prompt_text'], CLIP)}\"; {_fmt_dur(sec)}; "
                     f"verification {_tier(tools)}; tools " + (", ".join(f"{k} x{v}" for k, v in counts.most_common(8)) or "none")
                     + (f"; {errs} tool errors" if errs else "") + ("; interrupted by the user" if ints else ""))
        ids = [c["id"] for c in tools]
        if ids:
            q = ",".join("?" * len(ids))
            for r in conn.execute(f"SELECT path FROM file_edit WHERE tool_call_id IN ({q})", ids):
                if r[0]:
                    files[_basename(r[0])[:40]] += 1
    if len(turns) > len(shown):
        lines.append(f"- (+{len(turns) - len(shown)} more turns not shown)")
    if files:
        lines.append("Files edited: " + ", ".join(f for f, _ in files.most_common(12)) + ".")
    lines.append(f"Agent time in the shown turns: {_fmt_dur(total)}.")
    return "\n".join(lines)


SUMMARY_SYSTEM = ("You summarize coding-agent activity for a developer's dashboard. Reply with 1-2 plain sentences "
                  "(max 45 words): what the session (or turn) worked on and how it went (verified, failed checks, "
                  "interrupted). No preamble, no markdown, no quotes of the prompts.")


def summarize(client, model: str, text: str) -> str:
    resp = client.messages.create(model=model, max_tokens=1024, system=SUMMARY_SYSTEM,
                                  messages=[{"role": "user", "content": text}])
    out = " ".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    return clip(out.strip(), 400)


def summary_payload(conn, session_id: str, turn_id: int | None) -> tuple[int, dict]:
    if not conn.execute("SELECT 1 FROM session WHERE id=?", (session_id,)).fetchone():
        return 404, {"error": "no such session"}
    if turn_id is not None and not conn.execute("SELECT 1 FROM turn WHERE id=? AND session_id=?",
                                                (turn_id, session_id)).fetchone():
        return 404, {"error": "no such turn in this session"}
    key = uic.make_key("summary", session_id, turn_id, dbm.latest_run_id(conn))
    hit = uic.get(conn, key)
    if hit:
        return 200, {**hit["value"], "cached": True}
    api_key = None if _no_llm() else _api_key()
    if not api_key:
        return 404, {"error": "no API key"}
    from .config import load
    text = summary_input(conn, session_id, turn_id)
    with uic.lock(key):
        hit = uic.get(conn, key)
        if hit:
            return 200, {**hit["value"], "cached": True}
        try:
            out = summarize(_summary_client(api_key), load().judge_model, text)
        except Exception as e:
            return 502, {"error": f"summary failed: {type(e).__name__}"}
        if not out:
            return 502, {"error": "empty summary"}
        uic.put(conn, key, {"summary": out})
    return 200, {"summary": out, "cached": False}


ACTION_SOURCES = ("judge", "metrics")


def sanitize_action(it) -> dict | None:
    """One action item: text <= 160, quote <= 80 (human text), CLAUDE.md line <= 160, ids as ints."""
    if not isinstance(it, dict) or not it.get("text"):
        return None
    try:
        count = int(it.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    tids = []
    for t in it.get("turn_ids") or []:
        try:
            tids.append(int(t))
        except (TypeError, ValueError):
            continue
    out = {"text": clip(it.get("text"), 160), "category": clip(it.get("category"), 40) or None, "count": count,
           "turn_ids": tids[:20], "example_quote": clip(it.get("example_quote"), CLIP) or None,
           "claude_md_line": clip(it.get("claude_md_line"), 160) or None,
           "uncalibrated": bool(it.get("uncalibrated")),
           "source": it.get("source") if it.get("source") in ACTION_SOURCES else "metrics"}
    return out


def actions_payload(conn, day: str) -> dict:
    """judge.action_items(conn, day), sanitized. Empty state while the function does not exist yet."""
    try:
        from . import judge
        fn = getattr(judge, "action_items", None)
    except Exception:
        fn = None
    if fn is None:
        return {"day": day, "available": False, "items": []}
    try:
        raw = fn(conn, day) or []
    except Exception as e:
        return {"day": day, "available": True, "items": [], "error": f"action items failed: {type(e).__name__}"}
    items = [x for x in (sanitize_action(it) for it in list(raw)[:20]) if x]
    return {"day": day, "available": True, "items": items}


# --- chat -------------------------------------------------------------------
def _load_agent():
    try:
        from . import agent  # written by the chat-agent owner
        return agent
    except ImportError:
        return None


MASTRA_ENV = "SCORER_MASTRA_URL"
MASTRA_HEALTH_TIMEOUT_S = 0.3
TOOL_NAMES = ("query_db", "get_turn", "get_metric", "list_sessions")


def mastra_url() -> str | None:
    """The optional Mastra sidecar (agent-mastra/), if SCORER_MASTRA_URL is set and /health answers
    within 300 ms. Anything else -> None and the Python agent serves the chat (the default)."""
    url = (os.environ.get(MASTRA_ENV) or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return None
    try:
        with urllib.request.urlopen(url + "/health", timeout=MASTRA_HEALTH_TIMEOUT_S) as r:
            return url if r.status == 200 else None
    except Exception:
        return None


def chat_status() -> dict:
    agent = _load_agent()
    backend = "mastra" if mastra_url() else "python"
    if agent is None:
        return {"available": backend == "mastra", "model": None, "backend": backend,
                "reason": None if backend == "mastra" else "agent module not installed"}
    try:
        ok = bool(agent.has_key())
    except Exception:
        ok = False
    return {"available": ok, "model": getattr(agent, "MODEL", None), "backend": backend,
            "reason": None if ok else "no ANTHROPIC_API_KEY"}


def _clip_tree(o, depth=0):
    if depth > 4:
        return None
    if isinstance(o, str):
        return clip(o, CLIP)
    if isinstance(o, dict):
        return {str(k)[:40]: _clip_tree(v, depth + 1) for k, v in list(o.items())[:20]}
    if isinstance(o, (list, tuple)):
        return [_clip_tree(v, depth + 1) for v in list(o)[:50]]
    return o


def _cites(raw) -> list[dict]:
    return [{"n": c.get("n"), "turn_id": c.get("turn_id"), "session_id": clip(c.get("session_id"), 64),
             "label": clip(c.get("label"), CLIP)} for c in (raw or [])[:50] if isinstance(c, dict)]


def _first(ev: dict, *keys):
    for k in keys:
        v = ev.get(k)
        if v not in (None, ""):
            return v
    return None


def sanitize_event(ev) -> dict:
    """Privacy guard on agent events: everything except model prose is clipped to 80 chars.
    Also normalizes field names (Mastra/other backends: parentId, subagentId, agent, prompt, input.task)."""
    if not isinstance(ev, dict):
        return {"type": "error", "message": "bad event"}
    t = ev.get("type")
    if t == "text-delta":
        return {"type": t, "delta": str(ev.get("delta") or "")}
    if t == "tool-start":
        out = {"type": t, "id": clip(str(ev.get("id")), 64), "name": clip(ev.get("name"), 40),
               "input": _clip_tree(ev.get("input"))}
        sid = _first(ev, "subagent_id", "subagentId", "parent_id", "parentId")
        if sid is not None:
            out["subagent_id"] = clip(str(sid), 64)
        return out
    if t == "tool-result":
        return {"type": t, "id": clip(str(ev.get("id")), 64), "name": clip(ev.get("name"), 40),
                "ok": bool(ev.get("ok")), "summary": clip(ev.get("summary"), CLIP), "citations": _cites(ev.get("citations"))}
    if t == "subagent-start":
        inp = ev.get("input") if isinstance(ev.get("input"), dict) else {}
        out = {"type": t, "id": clip(str(_first(ev, "id", "toolCallId")), 64),
               "name": clip(_first(ev, "name", "agent", "agentName") or "subagent", 40),
               "task": clip(_first(ev, "task", "prompt") or _first(inp, "task", "prompt", "question"), CLIP)}
        pid = _first(ev, "parent_id", "parentId")
        if pid is not None:
            out["parent_id"] = clip(str(pid), 64)
        return out
    if t == "subagent-result":
        summary = clip(ev.get("summary"), CLIP)
        ok = bool(ev["ok"]) if "ok" in ev else not summary.lower().startswith("failed")
        return {"type": t, "id": clip(str(_first(ev, "id", "toolCallId")), 64),
                "name": clip(_first(ev, "name", "agent", "agentName") or "subagent", 40),
                "ok": ok, "summary": summary, "citations": _cites(ev.get("citations"))}
    if t == "suggestions":   # model-written follow-up prompts (not human text): label <= 40, prompt <= 200
        items = []
        for it in ev.get("items") or []:
            if isinstance(it, dict) and it.get("prompt"):
                items.append({"label": clip(it.get("label") or it.get("prompt"), 40), "prompt": clip(it.get("prompt"), 200)})
            if len(items) == 4:
                break
        return {"type": t, "items": items}
    if t == "error":
        return {"type": t, "message": clip(ev.get("message"), CLIP)}
    if t == "done":
        out = {"type": "done"}
        if ev.get("model"):
            out["model"] = clip(str(ev.get("model")), 40)
        if ev.get("backend") in ("python", "mastra"):
            out["backend"] = ev["backend"]
        return out
    return {"type": "error", "message": f"unknown event type {clip(str(t), 30)}"}


class EventMapper:
    """sanitize_event + subagent bookkeeping: a tool-start without its own subagent_id gets the id of
    the innermost subagent that is still open (Mastra forwards sub-agent tool steps without one)."""

    def __init__(self):
        self.open: list[str] = []

    def __call__(self, ev) -> dict:
        out = sanitize_event(ev)
        t = out["type"]
        if t == "subagent-start":
            self.open.append(out["id"])
        elif t == "subagent-result":
            if out["id"] in self.open:
                self.open.remove(out["id"])
        elif t == "tool-start" and "subagent_id" not in out and self.open:
            out["subagent_id"] = self.open[-1]
        return out


# --- HTTP -----------------------------------------------------------------
def _q1(qs, k):
    v = qs.get(k)
    return v[0] if v else None


class Handler(BaseHTTPRequestHandler):
    db_path: str | None = None
    server_version = "scorer"

    def log_message(self, fmt, *args):  # quiet; never log query strings
        pass

    def _send(self, code: int, body: bytes, ctype: str, cache: bool = False):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        try:
            if path.startswith("/api/"):
                return self._api(path, parse_qs(u.query))
            return self._static(path)
        except Exception as e:  # never leak data in errors
            return self._json(500, {"error": type(e).__name__})

    def _body(self):
        n = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError
        return body

    def do_POST(self):  # noqa: N802
        try:
            return self._post(urlparse(self.path).path)
        except Exception as e:  # never leak data in errors
            return self._json(500, {"error": type(e).__name__})

    def _post(self, path):
        m = re.fullmatch(r"/api/tools/([a-z_]+)", path)
        if m:
            return self._tool(m.group(1))
        if path != "/api/chat":
            return self._json(404, {"error": "unknown endpoint"})
        try:
            body = self._body()
            if not isinstance(body.get("messages", []), list):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "body must be {messages: [{role, content}], day?}"})
        return self._chat(body)

    def _tool(self, name):
        """POST /api/tools/<name> {input, n0?, day?} -> agent.run_tool(...). The Mastra sidecar's only
        data access: read-only, free text clipped to 80, citations numbered from n0."""
        if name not in TOOL_NAMES:
            return self._json(404, {"error": f"unknown tool, expected one of {', '.join(TOOL_NAMES)}"})
        agent = _load_agent()
        if agent is None:
            return self._json(501, {"error": "not_implemented"})
        try:
            body = self._body()
            inp = body.get("input", {})
            n0 = int(body.get("n0", 1) or 1)
            if not isinstance(inp, dict) or n0 < 1:
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            return self._json(400, {"error": "body must be {input: {...}, n0?: int >= 1, day?: YYYY-MM-DD}"})
        day = body.get("day") if isinstance(body.get("day"), str) and DAY_RE.match(body.get("day")) else None
        conn = dbm.connect(self.db_path)
        try:
            r = agent.run_tool(conn, name, inp, n0=n0, day=day)
        finally:
            conn.close()
        return self._json(200, r)

    def _sse_head(self) -> bool:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _chat(self, body):
        msgs = [{"role": m.get("role"), "content": str(m.get("content") or "")}
                for m in body.get("messages", []) if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
        day = body.get("day") if isinstance(body.get("day"), str) and DAY_RE.match(body.get("day")) else None
        upstream = mastra_url()
        if upstream:
            return self._chat_proxy(upstream, msgs, day)
        agent = _load_agent()
        if agent is None or not hasattr(agent, "stream_chat"):
            return self._json(501, {"error": "not_implemented", "detail": "chat backend not available yet"})
        if not self._sse_head():
            return
        conn = dbm.connect(self.db_path)
        sent_done = False
        mapper = EventMapper()
        try:
            for ev in agent.stream_chat(conn, msgs, day):
                if isinstance(ev, dict) and ev.get("type") == "done":
                    ev = {**ev, "backend": "python"}
                ev = mapper(ev)
                sent_done = sent_done or ev.get("type") == "done"
                if not self._sse(ev):
                    return
        except Exception as e:  # never leak data; report type only
            self._sse({"type": "error", "message": f"agent failed: {type(e).__name__}"})
        finally:
            conn.close()
        if not sent_done:
            self._sse({"type": "done", "backend": "python"})

    def _chat_proxy(self, upstream: str, msgs, day):
        """Relay the Mastra sidecar's SSE stream; every event still passes EventMapper here."""
        me = f"http://127.0.0.1:{self.server.server_address[1]}"
        req = urllib.request.Request(upstream + "/chat", method="POST",
                                     data=json.dumps({"messages": msgs, "day": day}).encode(),
                                     headers={"Content-Type": "application/json", "X-Scorer-Url": me})
        if not self._sse_head():
            return
        sent_done = False
        mapper = EventMapper()
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    ev = mapper(ev)
                    sent_done = sent_done or ev.get("type") == "done"
                    if not self._sse(ev):
                        return
                    if sent_done:
                        break
        except Exception as e:
            self._sse({"type": "error", "message": f"mastra backend failed: {type(e).__name__}"})
        if not sent_done:
            self._sse({"type": "done", "backend": "mastra"})

    def _sse(self, ev) -> bool:
        try:
            self.wfile.write(b"data: " + json.dumps(ev, default=str).encode() + b"\n\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _day_arg(self, raw):
        day = raw or "today"
        if day == "today":
            day = local_day(datetime.now(timezone.utc))
        return day if DAY_RE.match(day) else None

    def _api(self, path, qs):
        conn = dbm.connect(self.db_path)
        try:
            if path == "/api/chat/status":
                return self._json(200, chat_status())
            if path == "/api/agent/system-prompt":
                agent = _load_agent()
                if agent is None:
                    return self._json(501, {"error": "not_implemented"})
                return self._json(200, {"system_prompt": agent._SYSTEM, "today": agent._today(),
                                        "max_steps": agent.MAX_STEPS})
            if path == "/api/days":
                return self._json(200, list_days(conn))
            m = re.fullmatch(r"/api/day/([^/]+)", path)
            if m:
                day = self._day_arg(m.group(1))
                if not day:
                    return self._json(400, {"error": "day must be YYYY-MM-DD"})
                inc = _q1(qs, "include_temp") in ("1", "true", "yes")
                return self._json(200, day_payload(conn, day, include_temp=inc))
            m = re.fullmatch(r"/api/report/([^/]+)", path)
            if m:
                day = self._day_arg(m.group(1))
                if not day:
                    return self._json(400, {"error": "day must be YYYY-MM-DD"})
                f = report_payload(conn, day, include_temp=_q1(qs, "include_temp") in ("1", "true", "yes"))
                return self._json(200, f) if f is not None else self._json(404, {"error": f"no data for {day}"})
            if path == "/api/narrative":
                day = self._day_arg(_q1(qs, "day"))
                if not day:
                    return self._json(400, {"error": "day must be YYYY-MM-DD"})
                agent = _load_agent()
                if agent is None or not hasattr(agent, "narrative"):
                    return self._json(501, {"error": "not_implemented"})
                return self._json(200, narrative_payload(conn, day, agent))
            if path == "/api/actions":
                day = self._day_arg(_q1(qs, "day"))
                if not day:
                    return self._json(400, {"error": "day must be YYYY-MM-DD"})
                return self._json(200, actions_payload(conn, day))
            if path == "/api/summary":
                sid = _q1(qs, "session_id")
                tid = _q1(qs, "turn_id")
                if not sid or (tid is not None and not tid.isdigit()):
                    return self._json(400, {"error": "session_id required; turn_id must be an integer"})
                code, obj = summary_payload(conn, sid, int(tid) if tid is not None else None)
                return self._json(code, obj)
            m = re.fullmatch(r"/api/turn/(\d+)", path)
            if m:
                p = turn_payload(conn, int(m.group(1)))
                return self._json(200, p) if p else self._json(404, {"error": "no such turn"})
            if path == "/api/trend":
                end = (qs.get("end") or [local_day(datetime.now(timezone.utc))])[0]
                if not DAY_RE.match(end):
                    return self._json(400, {"error": "end must be YYYY-MM-DD"})
                try:
                    n = max(1, min(31, int((qs.get("days") or ["7"])[0])))
                except ValueError:
                    n = 7
                return self._json(200, trend_payload(conn, end, n))
            return self._json(404, {"error": "unknown endpoint"})
        finally:
            conn.close()

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if not f.is_relative_to(STATIC.resolve()) or not f.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = TYPES.get(f.suffix) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype, cache=f.suffix == ".woff2")


def make_server(db: str | None, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    conn = dbm.connect(db)  # create schema / fail fast on a bad path
    conn.execute(uic.DDL)
    conn.commit()
    conn.close()
    handler = type("BoundHandler", (Handler,), {"db_path": db})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(db: str | None, port: int) -> None:
    srv = make_server(db, port)
    print(f"scorer dashboard: http://127.0.0.1:{srv.server_address[1]}/  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
