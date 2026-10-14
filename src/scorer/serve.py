"""Local dashboard: stdlib ThreadingHTTPServer over the scorer SQLite DB.

    serve(db: str | None, port: int) -> None     # blocks; Ctrl-C to stop
    make_server(db, port, host="127.0.0.1") -> ThreadingHTTPServer   # tests: port 0

Static files come from ``scorer/static`` (index.html, app.css, app.js, fonts/).
Every request opens its own read connection (sqlite3 is not shared across
threads). Metrics always come from the latest ``ingest_run`` (db.latest_run_id).

Privacy: any human text (prompt_text, args_summary, initiator_rule, cwd/repo/
branch) leaves this module only through ``model.clip(..., 80)``. Event ``data``
JSON is never sent.

JSON endpoints (all GET, ``Cache-Control: no-store``)
-----------------------------------------------------
/api/days
    {"run_id": int|null, "today": "YYYY-MM-DD",
     "days": [{"day", "turns", "sessions"}]}            newest first, local tz
/api/day/<YYYY-MM-DD>
    {"day", "run_id", "start", "end",                     # local-day bounds, ISO UTC
     "sessions": [{"id", "short", "agent", "repo", "branch", "turns": [
         {"id", "idx", "start", "end", "open", "kind", "initiator",
          "interrupted", "clipped_start", "clipped_end"}],
       "interrupts": [{"at", "turn_id"}]}],
     "overlap": [{"start", "end"}],                     # >=2 sessions active
     "gaps": [{"session_id", "start", "end", "seconds", "bucket", "switched"}],
     "computed": {"active_s", "overlap_s", "turns", "sessions", "interrupts"},
     "scored": {"V"|"V_fail_share"|"V_final"|"V_human"|"H"|"B_min":
                    {"value", "unit", "n", "evidence": [{"turn_id", "tool_call_id", "note"}]} | null,
                "C": [{"metric", "label", "value", "unit", "n", "evidence": [...]}]},
     "diagnostics": [{"metric", "base", "key", "value", "unit", "n"}],   # every other day-scoped metric
     "leverage": {"subagents": number|null, "compactions": number|null,  # not scored
                  "tooling": {"categories": {"skill"|"command"|"mcp"|"plugin":
                                  {"human", "agent", "default", "total"}},   # numbers or null
                              "human_share": number|null,
                              "items": [{"category", "name", "initiator", "count"}]}},  # from evidence notes
     "verdict": {"where": str, "change": str, "basis": metric-id|null}}
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
    {"days": [YYYY-MM-DD...], "series": {"V", "H", "B_min", "C", "active_h": [number|null...]}}
GET /api/chat/status
    {"available": bool, "model": str|null, "reason": str|null}     # agent.has_key(), agent.MODEL
POST /api/chat   body {"messages": [{"role": "user"|"assistant", "content": str}], "day"?: "YYYY-MM-DD"}
    Server-Sent Events (text/event-stream), one `data: <json>\n\n` per event from
    scorer.agent.stream_chat(conn, messages, day):
      {"type": "text-delta", "delta"}
      {"type": "tool-start", "id", "name", "input"}                 # string inputs clipped to 80
      {"type": "tool-result", "id", "name", "ok", "summary",       # summary/labels clipped to 80
       "citations": [{"n", "turn_id", "session_id", "label"}]}
      {"type": "error", "message"}   {"type": "done"}              # stream always ends with done
    501 {"error": "not_implemented"} when scorer.agent is not importable; 400 on a bad body.
Unknown paths -> 404 {"error"}; bad day/turn -> 400/404.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db as dbm
from .model import clip, iso, local_day, parse_ts

STATIC = Path(__file__).parent / "static"
CLIP = 80
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCORED = ("V", "V_fail_share", "V_final", "V_human", "H", "B_min")
GAP_BUCKETS = ((30, "<30s"), (120, "30s-2m"), (300, "2-5m"), (900, "5-15m"))
BREAK_S = 900
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml",
         ".txt": "text/plain; charset=utf-8", ".json": "application/json"}


# --- helpers ---------------------------------------------------------------
def _basename(p: str | None) -> str:
    if not p:
        return ""
    return clip(os.path.basename(p.rstrip("/")) or p, CLIP)


def day_bounds(day: str) -> tuple[datetime, datetime]:
    y, m, d = map(int, day.split("-"))
    start = datetime(y, m, d).astimezone()            # local midnight, aware
    end = (datetime(y, m, d) + timedelta(days=1)).astimezone()
    return start, end


def union_len(iv: list[tuple[datetime, datetime]]) -> float:
    tot, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(iv):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                tot += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        tot += (cur_e - cur_s).total_seconds()
    return tot


def overlap_intervals(by_session: dict[str, list[tuple[datetime, datetime]]]) -> list[tuple[datetime, datetime]]:
    """Intervals where >= 2 distinct sessions are active (sweep line)."""
    pts: list[tuple[datetime, int, str]] = []
    for sid, iv in by_session.items():
        # merge each session's own intervals first so one session never counts twice
        merged: list[list[datetime]] = []
        for s, e in sorted(iv):
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        for s, e in merged:
            if e > s:
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


# --- queries ---------------------------------------------------------------
def _metrics(conn, run_id, scope_type, scope_id):
    if run_id is None:
        return []
    return conn.execute(
        "SELECT id, metric, value, unit, n FROM metric_value WHERE run_id=? AND scope_type=? AND scope_id=?"
        " ORDER BY metric", (run_id, scope_type, str(scope_id))).fetchall()


def _evidence(conn, mv_ids):
    out = defaultdict(list)
    if not mv_ids:
        return out
    q = ",".join("?" * len(mv_ids))
    for r in conn.execute(f"SELECT metric_value_id, turn_id, tool_call_id, note FROM metric_evidence"
                          f" WHERE metric_value_id IN ({q}) LIMIT 2000", mv_ids):
        lst = out[r["metric_value_id"]]
        if len(lst) < 50:
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
        "SELECT t.*, s.agent, s.repo, s.cwd, s.branch FROM turn t JOIN session s ON s.id=t.session_id"
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


def day_payload(conn, day) -> dict:
    run_id = dbm.latest_run_id(conn)
    turns, start, end = _day_turns(conn, day)
    sessions: dict[str, dict] = {}
    by_s = defaultdict(list)
    for t, s, e, open_ in turns:
        sid = t["session_id"]
        sess = sessions.get(sid)
        if sess is None:
            sess = sessions[sid] = {"id": sid, "short": sid[:8], "agent": t["agent"],
                                    "repo": _basename(t["repo"] or t["cwd"]), "branch": clip(t["branch"], 40),
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
    rows = _metrics(conn, run_id, "day", day)
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
    order = sorted(sessions.values(), key=lambda s: s["turns"][0]["start"])
    leverage = leverage_block(conn, rows)
    return {"day": day, "run_id": run_id, "start": iso(start), "end": iso(end), "sessions": order,
            "overlap": [{"start": iso(s), "end": iso(e)} for s, e in ov], "gaps": gaps, "computed": computed,
            "scored": scored, "diagnostics": diagnostics, "leverage": leverage,
            "verdict": verdict(computed, scored, {d["metric"]: d["value"] for d in diagnostics})}


TOOL_CATS = ("skill", "command", "mcp", "plugin")
TOOL_INITS = ("human", "agent", "default")
NOTE_RE = re.compile(r"^(skill|command|mcp|plugin):(.+):(human|agent|default) x(\d+)$")


def leverage_block(conn, rows) -> dict:
    """Subagents + tooling use (skills/commands/MCP/plugins by initiator). Diagnostic, not scored."""
    vals = {r["metric"]: r["value"] for r in rows}
    cats = {}
    for c in TOOL_CATS:
        d = {i: vals.get(f"tooling:{c}:{i}") for i in TOOL_INITS}
        tot = vals.get(f"tooling:{c}")
        if tot is None and any(v is not None for v in d.values()):
            tot = sum(v or 0 for v in d.values())
        cats[c] = {**d, "total": tot}
    ids = [r["id"] for r in rows if r["metric"].startswith("tooling")]
    items: Counter = Counter()
    if ids:
        q = ",".join("?" * len(ids))
        for r in conn.execute(f"SELECT DISTINCT note FROM metric_evidence WHERE metric_value_id IN ({q})", ids):
            m = NOTE_RE.match((r["note"] or "").strip())
            if m:
                items[(m.group(1), clip(m.group(2), 60), m.group(3))] = max(items[(m.group(1), clip(m.group(2), 60), m.group(3))], int(m.group(4)))
    return {"subagents": vals.get("subagents"), "compactions": vals.get("compactions"),
            "tooling": {"categories": cats, "human_share": vals.get("tooling_human_share"),
                        "items": [{"category": c, "name": n, "initiator": i, "count": k}
                                  for (c, n, i), k in sorted(items.items(), key=lambda kv: -kv[1])][:100]}}


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


def trend_payload(conn, end_day: str, n: int) -> dict:
    run_id = dbm.latest_run_id(conn)
    y, m, d = map(int, end_day.split("-"))
    days = [(datetime(y, m, d) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]
    series: dict[str, list] = {k: [] for k in ("V", "H", "B_min", "C", "active_h")}
    for day in days:
        vals = {r["metric"]: r["value"] for r in _metrics(conn, run_id, "day", day)}
        for k in ("V", "H", "B_min"):
            series[k].append(vals.get(k))
        cs = [v for k, v in vals.items() if k == "C" or k.startswith("C:")]
        series["C"].append(sum(v or 0 for v in cs) if cs else None)
        act = vals.get("active_s")
        if act is None:
            c = computed_day(conn, day)
            act = c["active_s"] if c["turns"] else None
        series["active_h"].append(round(act / 3600, 2) if act is not None else None)
    return {"days": days, "series": series}


# --- chat -------------------------------------------------------------------
def _load_agent():
    try:
        from . import agent  # written by the chat-agent owner
        return agent
    except ImportError:
        return None


def chat_status() -> dict:
    agent = _load_agent()
    if agent is None:
        return {"available": False, "model": None, "reason": "agent module not installed"}
    try:
        ok = bool(agent.has_key())
    except Exception:
        ok = False
    return {"available": ok, "model": getattr(agent, "MODEL", None),
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


def sanitize_event(ev) -> dict:
    """Privacy guard on agent events: everything except model prose is clipped to 80 chars."""
    if not isinstance(ev, dict):
        return {"type": "error", "message": "bad event"}
    t = ev.get("type")
    if t == "text-delta":
        return {"type": t, "delta": str(ev.get("delta") or "")}
    if t == "tool-start":
        return {"type": t, "id": clip(str(ev.get("id")), 64), "name": clip(ev.get("name"), 40),
                "input": _clip_tree(ev.get("input"))}
    if t == "tool-result":
        cites = [{"n": c.get("n"), "turn_id": c.get("turn_id"), "session_id": clip(c.get("session_id"), 64),
                  "label": clip(c.get("label"), CLIP)} for c in (ev.get("citations") or [])[:50] if isinstance(c, dict)]
        return {"type": t, "id": clip(str(ev.get("id")), 64), "name": clip(ev.get("name"), 40),
                "ok": bool(ev.get("ok")), "summary": clip(ev.get("summary"), CLIP), "citations": cites}
    if t == "error":
        return {"type": t, "message": clip(ev.get("message"), CLIP)}
    if t == "done":
        return {"type": "done"}
    return {"type": "error", "message": f"unknown event type {clip(str(t), 30)}"}


# --- HTTP -----------------------------------------------------------------
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

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/chat":
            return self._json(404, {"error": "unknown endpoint"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict) or not isinstance(body.get("messages", []), list):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "body must be {messages: [{role, content}], day?}"})
        return self._chat(body)

    def _chat(self, body):
        agent = _load_agent()
        if agent is None or not hasattr(agent, "stream_chat"):
            return self._json(501, {"error": "not_implemented", "detail": "chat backend not available yet"})
        msgs = [{"role": m.get("role"), "content": str(m.get("content") or "")}
                for m in body.get("messages", []) if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
        day = body.get("day") if isinstance(body.get("day"), str) and DAY_RE.match(body.get("day")) else None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            return
        conn = dbm.connect(self.db_path)
        sent_done = False
        try:
            for ev in agent.stream_chat(conn, msgs, day):
                ev = sanitize_event(ev)
                sent_done = sent_done or ev.get("type") == "done"
                if not self._sse(ev):
                    return
        except Exception as e:  # never leak data; report type only
            self._sse({"type": "error", "message": f"agent failed: {type(e).__name__}"})
        finally:
            conn.close()
        if not sent_done:
            self._sse({"type": "done"})

    def _sse(self, ev) -> bool:
        try:
            self.wfile.write(b"data: " + json.dumps(ev, default=str).encode() + b"\n\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _api(self, path, qs):
        conn = dbm.connect(self.db_path)
        try:
            if path == "/api/chat/status":
                return self._json(200, chat_status())
            if path == "/api/days":
                return self._json(200, list_days(conn))
            m = re.fullmatch(r"/api/day/([^/]+)", path)
            if m:
                day = m.group(1)
                if day == "today":
                    day = local_day(datetime.now(timezone.utc))
                if not DAY_RE.match(day):
                    return self._json(400, {"error": "day must be YYYY-MM-DD"})
                return self._json(200, day_payload(conn, day))
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
