"""Bridge to "how do you ai" (ari-version/): one builder, two writers (docs/spec/bridge-contract.md).

build(conn, day) -> Bundle with Ari-shaped Session / Report objects + our ScorerMetrics.
write_local(bundle, out)  -> upsert sessions/reports rows into ari-version/.local/sessions.sqlite.
sync(...)                 -> hosted analysis service: /api/sync, /api/analyze, poll, /api/sync(report).
Read-only on our DB; never calls a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .model import clip, local_day, parse_ts, scorer_home

LOCAL_TEXT_CAP = 12_000
HOSTED_TEXT_CAP = 1_800
EXCERPT_CAP = 320
TILE_EVIDENCE = 8
METRIC_LINE_BASE = 9_000_000
PROMPT_BUDGET = 115_000
POSITIVE = ("good_spec", "verification_catch", "early_redirect", "scoped_turn", "knew_when_not_to_delegate")
DIMENSIONS = ("Framing", "Context selection", "Iteration", "Verification judgment",
              "Delegation and coordination", "Tool judgment", "Outcome evidence")

# Ari's exact DDL (ari-version/lib/db.ts).
ARI_DDL = """CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT NOT NULL, fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);"""


# --- small helpers ------------------------------------------------------------------------------

def _iso(ts: str | None) -> str | None:
    d = parse_ts(ts) if ts else None
    if not d:
        return None
    return d.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_TZ_OVERRIDE: str | None = None


def _tz() -> str:
    """Label tz: --tz, else REPORT_TIMEZONE, else config tz, else system tz. Day assignment stays
    model.local_day (config tz + cutoff), so numbers match facts(day)."""
    from .config import load
    tz = _TZ_OVERRIDE or os.environ.get("REPORT_TIMEZONE") or getattr(load(), "timezone", None)
    if tz:
        return tz
    try:
        return str(Path(os.path.realpath("/etc/localtime"))).split("zoneinfo/")[-1]
    except OSError:
        return "UTC"


def _cap(text: str, n: int) -> tuple[str, bool]:
    text = text or ""
    return (text, False) if len(text) <= n else (text[: n - 1] + "…", True)


def _pct(x) -> str:
    return "—" if x is None else f"{round(x * 100)}%"


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _model_slug(m: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", (m or "unknown").lower().replace("claude-", ""))
    return s


def ari_id(agent: str, path: str | None, sid: str) -> str:
    """Their Session.id: sha256(f"{agent}:{path}")[:24]; demo rows without a path hash `demo:<id>`."""
    return hashlib.sha256(f"{agent}:{path or f'demo:{sid}'}".encode()).hexdigest()[:24]


def _is_demo(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM event WHERE kind='demo_marker' LIMIT 1").fetchone())


# --- sessions + evidence ------------------------------------------------------------------------

def _day_turns(conn, day: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in _rows(conn, "SELECT * FROM turn ORDER BY session_id, idx"):
        if local_day(t["prompt_at"]) == day:
            out.setdefault(t["session_id"], []).append(t)
    return out


def _attempts(conn, day: str) -> dict[str, dict]:
    out = {}
    for e in _rows(conn, "SELECT session_id, at, data FROM event WHERE kind='attempt'"):
        if local_day(e["at"]) == day:
            try:
                out[e["session_id"]] = json.loads(e["data"] or "{}")
            except ValueError:
                out[e["session_id"]] = {}
    return out


class _Ev:
    """One evidence record built once; `local` (≤12k) and `hosted` (≤1800) views share every field
    except text/truncated, so the hosted object is byte-identical in analyze input and evidenceIndex."""

    def __init__(self, sid, line, ts, kind, text, **extra):
        self.id = f"{sid}:{line}" if kind != "metric" else extra.pop("mid")
        self.sid, self.line, self.ts, self.kind, self.text = sid, line, ts or "", kind, text or ""
        self.extra = {k: v for k, v in extra.items() if v is not None}

    def view(self, cap: int) -> dict:
        text, cut = _cap(self.text, cap)
        d = {"id": self.id, "sessionId": self.sid, "line": self.line, "timestamp": self.ts,
             "kind": self.kind, "text": text, **self.extra}
        if cut:
            d["truncated"] = True
        return d


def _session_events(conn, s: dict, turns: list[dict]) -> tuple[list[_Ev], list[dict], dict, list[str], int]:
    """Evidence, TaskSpans, tokenUsage, tool names, tool count for one session's day turns."""
    sid = s["_aid"]
    evs: list[_Ev] = []
    tasks, records, interrupts = [], [], []
    tools: set[str] = set()
    ntools = 0
    intr = {r["turn_id"]: r["at"] for r in _rows(conn, "SELECT turn_id, at FROM event WHERE kind='interrupt'"
                                                    " AND session_id=?", (s["id"],))}
    for t in turns:
        base = t["idx"] * 1000
        evs.append(_Ev(sid, base, _iso(t["prompt_at"]), "user", t["prompt_text"] or ""))
        tcs = _rows(conn, "SELECT * FROM tool_call WHERE turn_id=? ORDER BY started_at, id", (t["id"],))
        ntools += len(tcs)
        for k, c in enumerate(tcs[:499], start=1):
            tools.add(c["name"] or "tool")
            ts = _iso(c["started_at"]) or _iso(t["prompt_at"])
            evs.append(_Ev(sid, base + 2 * k - 1, ts, "tool_call", c["args_summary"] or c["name"] or "",
                           tool=c["name"], callId=c["tool_use_id"]))
            if c["has_result"]:
                dur = f" · {c['duration_ms']} ms" if c["duration_ms"] is not None else ""
                evs.append(_Ev(sid, base + 2 * k, ts, "tool_result",
                               ("error" if c["is_error"] else "ok") + dur,
                               tool=c["name"], callId=c["tool_use_id"], error=bool(c["is_error"])))
        aborted = t["id"] in intr or t["end_source"] == "interrupt"
        if aborted:
            interrupts.append({"sessionId": sid, "at": _iso(intr.get(t["id"]) or t["prompt_at"]),
                               "evidenceId": f"{sid}:{base}"})
        if t["prompt_kind"] == "command":
            pass  # command turns (/clear, /model) are not tasks
        elif t["prompt_kind"] == "queued" and tasks:
            # queued prompt folds into the open task
            tasks[-1]["end"] = _iso(t["agent_finished_at"]) or tasks[-1]["end"]
            tasks[-1]["aborted"] = tasks[-1]["aborted"] or aborted
        elif t["agent_started_at"] or t["agent_finished_at"]:
            tid = t["first_uuid"] if s["agent"] == "codex" and t["first_uuid"] else f"{s['id']}:{t['idx']}"
            tasks.append({"id": tid, "start": _iso(t["prompt_at"]), "end": _iso(t["agent_finished_at"]),
                          "aborted": aborted, "reason": t["end_source"]})
        for a in _rows(conn, "SELECT request_id, at, input_tok, cached_tok, output_tok FROM api_call"
                             " WHERE turn_id=? ORDER BY at", (t["id"],)):
            inp = int(a["input_tok"] or 0) + int(a["cached_tok"] or 0)  # input_tok already holds cache_write
            out = int(a["output_tok"] or 0)
            records.append({"id": a["request_id"] or f"{sid}:a{len(records)}", "timestamp": _iso(a["at"]) or "",
                            "inputTokens": inp, "outputTokens": out, "cachedInputTokens": int(a["cached_tok"] or 0),
                            "cacheWriteInputTokens": 0, "totalTokens": inp + out})
    # no stop_reason stored: never claim final outputs (tokensPerFinalOutput stays unavailable)
    usage = {"records": records[:10000], "finalOutputs": [], "finalOutputCountKnown": False}
    s["_interrupts"] = interrupts
    return evs, tasks, usage, sorted(tools), ntools


def _parent_id(conn, forked_from: str | None) -> str | None:
    if not forked_from:
        return None
    r = conn.execute("SELECT agent, transcript_path FROM session WHERE id=?", (forked_from,)).fetchone()
    return ari_id(r[0], r[1], forked_from) if r else ari_id("codex", None, forked_from)


def _title(s: dict, turns: list[dict]) -> str:
    first = next((t["prompt_text"] for t in turns if t["prompt_text"] and t["prompt_kind"] != "command"), "")
    base = os.path.basename((s["cwd"] or "").rstrip("/")) or s["agent"]
    return clip(f"{base} · {clip(first, 60)}" if first else base, 120)


# --- metrics -> tiles / findings ----------------------------------------------------------------

class _Ctx:
    """Everything one report needs: sessions, evidence, turn->evidence id map, metric rows."""

    def __init__(self, conn, day: str, session_ids: list[str], turns: dict[str, list[dict]]):
        self.conn, self.day = conn, day
        self.sessions: list[dict] = []
        self.evs: dict[str, list[_Ev]] = {}
        self.turn_ev: dict[int, str] = {}
        self.turn_text: dict[int, str] = {}
        self.turn_sid: dict[int, str] = {}
        srows = {r["id"]: r for r in _rows(conn, "SELECT * FROM session WHERE id IN (%s)"
                                           % ",".join("?" * len(session_ids)), session_ids)} if session_ids else {}
        self.aid: dict[str, str] = {}
        for our in session_ids:
            s, ts = dict(srows[our]), turns[our]
            sid = s["_aid"] = ari_id(s["agent"], s["transcript_path"], our)
            self.aid[our] = sid
            evs, tasks, usage, tools, ntools = _session_events(conn, s, ts)
            for t in ts:
                self.turn_ev[t["id"]] = f"{sid}:{t['idx'] * 1000}"
                self.turn_text[t["id"]] = t["prompt_text"] or ""
                self.turn_sid[t["id"]] = sid
            self.evs[sid] = evs
            started = _iso(ts[0]["prompt_at"]) or _iso(s["started_at"]) or ""
            ends = [_iso(t["agent_finished_at"]) or _iso(t["prompt_at"]) for t in ts]
            self.sessions.append({
                "id": sid, "nativeId": our, "agent": s["agent"] if s["agent"] in ("claude", "codex") else "claude",
                "title": _title(s, ts), "cwd": s["cwd"] or "", "path": s["transcript_path"] or "",
                "startedAt": started, "updatedAt": max([e for e in ends if e] or [started]),
                "parentId": _parent_id(conn, s["forked_from"]), "source": "scorer", "hidden": False,
                "userCount": len(ts), "assistantCount": 0, "toolCount": ntools, "tools": tools,
                "malformedLines": 0, "truncated": False, "tasks": tasks, "tokenUsage": usage,
                "capture": {"provider": "hooks", "storage": "snapshot"},
                "_repo": s["repo"], "_branch": s["branch"], "_interrupts": s["_interrupts"],
            })
        self.busiest = self.aid[max(session_ids, key=lambda x: len(turns[x]))] if session_ids else None
        self.metric_ev: dict[str, _Ev] = {}  # metric name -> metric Evidence
        self.metric_names: list[str] = []      # sorted day metric names (line numbering)

    def user_ids(self, turn_ids, n: int = TILE_EVIDENCE) -> list[str]:
        out = []
        for t in turn_ids or []:
            e = self.turn_ev.get(t)
            if e and e not in out:
                out.append(e)
            if len(out) >= n:
                break
        return out

    def add_metric(self, name: str, row: dict, ev_rows: list[dict]) -> str:
        """Evidence kind 'metric' for one metric_value row; id metric:<metric_value_id>."""
        if name in self.metric_ev:
            return self.metric_ev[name].id
        refs = [self.turn_ev.get(r["turn_id"]) or f"turn {r['turn_id']}" for r in ev_rows if r["turn_id"]]
        notes = [r["note"] for r in ev_rows if r["note"]]
        sid = next((self.turn_sid[r["turn_id"]] for r in ev_rows if r["turn_id"] in self.turn_sid), None)
        orphan = sid is None
        sid = sid or self.busiest
        val = row["value"]
        vtxt = "null" if val is None else (f"{val:.4g}" if isinstance(val, float) else str(val))
        text = f"{name} = {vtxt} {row['unit'] or ''} (n={row['n']})".strip()
        if notes:
            text += " · " + "; ".join(clip(x, 80) for x in notes[:6])
        if refs:
            text += " · turns " + ", ".join(dict.fromkeys(refs))
        if orphan and ev_rows:
            text += " · (no turn in this report; attached to the day's busiest session)"
        if name not in self.metric_names:
            self.metric_names = sorted({*self.metric_names, name})
        slug = re.sub(r"[^A-Za-z0-9_-]", "_", name)
        ev = _Ev(sid, METRIC_LINE_BASE + self.metric_names.index(name), f"{self.day}T12:00:00.000Z",
                 "metric", text, mid=f"metric:{self.day}:{slug}")
        self.metric_ev[name] = ev
        self.evs[sid].append(ev)
        return ev.id

    def all_ids(self) -> set[str]:
        return {e.id for evs in self.evs.values() for e in evs}

    def excerpt(self, eid: str) -> dict:
        for sid, evs in self.evs.items():
            for e in evs:
                if e.id == eid:
                    return {"id": eid, "sessionId": sid, "excerpt": _cap(e.text, EXCERPT_CAP)[0]}
        raise KeyError(eid)


def _metric_rows(conn, run_id, day) -> dict[str, dict]:
    out = {}
    for r in _rows(conn, "SELECT id, metric, value, unit, n FROM metric_value WHERE run_id=?"
                         " AND scope_type='day' AND scope_id=?", (run_id, day)):
        out[r["metric"]] = r
    return out


def _metric_evidence(conn, mid: int) -> list[dict]:
    return _rows(conn, "SELECT turn_id, tool_call_id, note FROM metric_evidence WHERE metric_value_id=?"
                       " ORDER BY rowid", (mid,))


def _confidence(n) -> str:
    n = n or 0
    return "High" if n >= 20 else "Moderate" if n >= 5 else "Low"


def _finding(dim, rating, conf, assessment, ids) -> dict:
    if rating == "Insufficient evidence":
        conf = "Low"
    elif not ids:
        rating, conf = "Insufficient evidence", "Low"
    return {"dimension": dim, "rating": rating, "confidence": conf, "assessment": assessment,
            "evidenceIds": list(dict.fromkeys(ids))}


def _labels(conn, day) -> list[tuple[dict, dict]]:
    from . import judge
    try:
        return [(c, lab) for c, lab in judge._day_all(conn, day, first=True) if lab]
    except Exception:  # noqa: BLE001 - judge cache optional
        return []


def _episode_title(ep: str) -> str:
    return ep.replace("_", " ").capitalize()


# --- the builder --------------------------------------------------------------------------------

def _build_day(conn, day: str, ctx: _Ctx, *, demo: bool) -> tuple[dict, dict]:
    """(content, scorerMetrics) for the whole day (self report)."""
    from . import report as rep
    from .db import latest_run_id
    os.environ.setdefault("SCORER_NO_LLM", "1")
    run_id = latest_run_id(conn)
    f = rep.facts(conn, day, run_id=run_id) or {}
    rows = _metric_rows(conn, run_id, day) if run_id is not None else {}
    ctx.metric_names = sorted(rows)
    for name, row in rows.items():  # every metric with evidence rows becomes citable
        evr = _metric_evidence(conn, row["id"])
        if evr:
            ctx.add_metric(name, row, evr)

    def mref(name):
        r = rows.get(name)
        return ctx.add_metric(name, r, _metric_evidence(conn, r["id"])) if r else None

    def val(name):
        r = rows.get(name)
        return r["value"] if r else None

    attr = f.get("attribution") or {}
    ver = attr.get("verification") or {}
    vcounts = {k: (v or {}).get("count", 0) for k, v in ver.items()}
    vtotal = sum(vcounts.values())
    asked = (vcounts.get("asked_by_you", 0) / vtotal) if vtotal else val("V_human")
    corr = attr.get("corrections") or {}
    diag = f.get("diagnostics") or {}
    he = f.get("hand_edits") or {}
    labels = _labels(conn, day)
    episodes = [(c, lab) for c, lab in labels if lab.get("positive_episode") in POSITIVE]
    corrections = [(c, lab) for c, lab in labels if lab.get("event") == "correction"]

    # tiles ------------------------------------------------------------------------------------
    def tile(*a, **k):
        return _mk_tile(ctx, *a, **k)

    base = {s["metric"]: s.get("baseline") for s in f.get("scored", [])}
    v_unver = ((ver.get("none") or {}).get("turn_ids")) or (f.get("tomorrow") or {}).get("evidence_turns") or []
    tiles = [tile("V", "Verified edit batches", val("V"), "share", _pct(val("V")),
                  f"asked by you {_pct(asked)} · {vcounts.get('none', 0)} unverified" if vtotal else
                  f"asked by you {_pct(asked)}",
                  [mref("V")] + ctx.user_ids(v_unver, TILE_EVIDENCE - 1), base.get("V"))]
    c_total = sum((v or {}).get("count", 0) for v in corr.values()) if corr else val("C")
    causes = {k[len("C:cause:"):]: int(r["value"] or 0) for k, r in rows.items() if k.startswith("C:cause:")}
    c_turns = [t for v in corr.values() for t in (v or {}).get("turn_ids", [])] or [c["turn_id"] for c, _ in corrections]
    tiles.append(tile("C", "Corrections", c_total, "count", "—" if c_total is None else str(int(c_total)),
                      "by cause: " + " · ".join(f"{k.replace('_', ' ')} {v}" for k, v in
                                                sorted(causes.items(), key=lambda kv: -kv[1])) if causes else None,
                      [mref("C") or next((ctx.metric_ev[k].id for k in ctx.metric_ev if k.startswith("C:cause:")), None)]
                      + ctx.user_ids(c_turns, TILE_EVIDENCE - 1)))
    ep_titles = list(dict.fromkeys(_episode_title(lab["positive_episode"]) for _, lab in episodes))
    tiles.append(tile("episodes", "Did well", len(episodes), "count", str(len(episodes)),
                      " · ".join(ep_titles) or None, ctx.user_ids([c["turn_id"] for c, _ in episodes])))
    h_un = he.get("unmeasured")
    tiles.append(tile("H", "Hand edits", None if h_un else val("H"), "count",
                      "not measured" if h_un or val("H") is None else str(int(val("H"))),
                      None if h_un else f"repair {int(he.get('repair') or 0)} · own {int(he.get('own') or 0)}",
                      [mref("H")] + ctx.user_ids([e["turn_id"] for e in he.get("evidence", [])], TILE_EVIDENCE - 1),
                      base.get("H")))
    act_min = (val("active_s") or 0) / 60
    tiles.append(tile("B_min", "Agent blocked on you", val("B_min"), "min",
                      "—" if val("B_min") is None else f"{round(val('B_min'))} min",
                      f"of {act_min / 60:.1f} h active" if act_min else None, [mref("B_min")], base.get("B_min")))
    models = [{"model": m["model"], "usd": round(m.get("cost_usd") or 0, 2), "tokens": int(m.get("tokens") or 0)}
              for m in diag.get("models", []) if isinstance(m, dict)]

    flat = _flat(rows, asked, causes)
    sm = {"day": day, "tz": _tz(), "source": "scorer", "demo": demo,
          "verdict": (f.get("verdict") or {}).get("text") or "No dominant cost today.",
          "tiles": tiles, "leverage": _leverage(conn, day, ctx, rows),
          "breakdown": {"cCauses": causes, "costByModel": models}, "flat": flat}

    # findings ---------------------------------------------------------------------------------
    fx = []
    ep_ids = ctx.user_ids([c["turn_id"] for c, lab in episodes
                           if lab["positive_episode"] in ("good_spec", "scoped_turn")])
    fx.append(_finding("Framing", ("Effective" if ep_ids else "Insufficient evidence"),
                       "High" if len(ep_ids) >= 2 else "Moderate",
                       f"{len(ep_ids)} gated framing episode(s) (good spec / scoped turn) with no later correction."
                       if ep_ids else "No gated framing episode today.", ep_ids))
    fx.append(_finding("Context selection", "Insufficient evidence", "Low",
                       "Not scored by the harness yet.", []))
    if f.get("judged"):
        up = (corr.get("user_preventable") or {}).get("count", 0)
        labelled = max(1, sum((v or {}).get("count", 0) for v in corr.values()))
        p = up / labelled
        ids = [mref("C") or mref("C_labelled")] + ctx.user_ids(c_turns, 5)
        if not c_total:
            fx.append(_finding("Iteration", "Effective", "Moderate", "Judged turns show no corrections.",
                               [mref("C_labelled")]))
        else:
            fx.append(_finding("Iteration", "Effective" if p <= 0.25 else "Needs support", _confidence(labelled),
                               f"{int(c_total)} corrections; {up} user-preventable ({_pct(p)}). Thresholds: ≤25% Effective.",
                               ids))
    else:
        fx.append(_finding("Iteration", "Insufficient evidence", "Low", "Judge has not labelled this day.", []))
    vn = rows.get("V", {}).get("n") if "V" in rows else 0
    vv = val("V")
    if not vn or vn < 5 or vv is None:
        fx.append(_finding("Verification judgment", "Insufficient evidence", "Low",
                           f"Only {vn or 0} edit batches.", []))
    else:
        r = "Strongly demonstrated" if vv >= 0.75 else "Effective" if vv >= 0.5 else "Needs support"
        fx.append(_finding("Verification judgment", r, _confidence(vn),
                           f"{_pct(vv)} of {vn} edit batches verified; you asked for {_pct(asked)}. "
                           "Thresholds: ≥75% strong, ≥50% effective.",
                           [mref("V")] + ctx.user_ids(v_unver, 5)))
    bm = val("B_min")
    if act_min < 30 or bm is None:
        fx.append(_finding("Delegation and coordination", "Insufficient evidence", "Low",
                           "Less than 30 active minutes.", []))
    else:
        ratio = bm / act_min
        fx.append(_finding("Delegation and coordination", "Effective" if ratio <= 0.05 else "Needs support",
                           _confidence(rows["B_min"]["n"]),
                           f"Agents waited {round(bm)} min on you ({_pct(ratio)} of {act_min / 60:.1f} h active). "
                           "Threshold: ≤5% Effective.", [mref("B_min")]))
    th = val("tooling_human_share")
    tn = rows.get("tooling_human_share", {}).get("n") or 0
    if th is None or tn < 5:
        fx.append(_finding("Tool judgment", "Insufficient evidence", "Low", "Fewer than 5 tooling events.", []))
    else:
        fx.append(_finding("Tool judgment", "Effective" if th >= 0.1 else "Needs support", _confidence(tn),
                           f"You chose {_pct(th)} of {tn} skill/plugin/MCP uses. Threshold: ≥10% Effective.",
                           [mref("tooling_human_share")]))
    vf = val("V_final")
    if h_un or vf is None:
        fx.append(_finding("Outcome evidence", "Insufficient evidence", "Low",
                           "Hand edits not measured (needs git snapshots from hooks).", []))
    else:
        fx.append(_finding("Outcome evidence", "Effective" if vf >= 0.5 else "Needs support",
                           _confidence(rows["V_final"]["n"]),
                           f"{_pct(vf)} of sessions ended on a verified state; {int(val('H') or 0)} hand edits.",
                           [mref("V_final"), mref("H")]))
    for x in fx:
        x["evidenceIds"] = [i for i in x["evidenceIds"] if i]
        if x["rating"] != "Insufficient evidence" and not x["evidenceIds"]:
            x.update(rating="Insufficient evidence", confidence="Low")

    content = _content(ctx, f, fx, corrections, episodes, demo, day)
    return content, sm


HOW = {
    "V": "Share of edit batches followed by a test/build/lint run with a recorded result before the turn ended; "
         "asked-by-you = batches where your prompt asked for the check.",
    "C": "Turns the judge labelled as corrections of the agent's previous turn (quote must be in your prompt), "
         "grouped by cause. Uncalibrated.",
    "episodes": "Did-well episodes the judge proposed from your prompts (good spec, early redirect, scoped turn, "
                "verification catch, knew when not to delegate). Uncalibrated.",
    "H": "Lines you changed by hand in files the agent touched, from git snapshots at Stop minus the agent's own edits.",
    "B_min": "Minutes agents sat finished and waiting while you were active in another session.",
    "tooling_skill": "Skill invocations from the transcript, split by who triggered them: you (slash/prompt), "
                     "the agent, or a default hook/config.",
    "tooling_command": "Slash-command uses from the transcript, split by initiator (you, agent, default).",
    "tooling_mcp": "MCP tool calls from the transcript, split by initiator (you, agent, default).",
    "tooling_plugin": "Plugin tool/skill uses from the transcript, split by initiator (you, agent, default).",
    "top_tools": "Most-used named skills, commands, MCP and plugin tools today, counted from tooling events.",
    "subagents": "Subagent runs started today (Claude Task/Agent sidechains, Codex child threads), with their models.",
    "model_mix": "Share of API requests per model across all sessions today.",
    "parallel": "Share of your active wall time with more than one session working at once (overlap / active).",
    "interrupts": "Turns you interrupted (Esc / stop), with the agent time thrown away before the interrupt.",
    "gaps": "Median time between an agent finishing and your next prompt, bucketed; breaks are gaps over 15 min.",
    "context": "Context window fill per API request: median (p50) and p90 of input tokens / window size.",
    "tokens": "All recorded tokens: uncached input + cached input + output, deduped per request.",
    "cost": "List-price cost of every API request, summed per model.",
}


def _fmt_n(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    for d, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= d:
            return f"{x / d:.1f}{suf}"
    return str(int(round(x)))


def _mk_tile(ctx, key, label, value, unit, display, sub, ids, baseline=None) -> dict:
    ids = [i for i in ids if i][:TILE_EVIDENCE]
    return {"key": key, "label": label, "value": value, "unit": unit, "display": display,
            **({"sub": sub} if sub else {}), "baseline": baseline, "howMeasured": HOW.get(key, "")[:240],
            "evidence": [ctx.excerpt(i) for i in ids]}


def _leverage(conn, day: str, ctx: _Ctx, rows: dict, session: str | None = None) -> list[dict]:
    """Diagnostic tiles (not scored), same sources as serve.py's Leverage tab; session = our session.id
    restricts to one session (candidate attempts) and drops day-level metrics that mix sessions."""
    from collections import Counter
    from . import metrics as mx
    from . import serve
    vals = {} if session else {k: r["value"] for k, r in rows.items()}
    tl = [x for x in mx.tooling(conn, day) if not session or x["session_id"] == session]
    tiles = []
    for c in serve.TOOL_CATS:
        xs = [x for x in tl if x["category"] == c]
        by = Counter(x["initiator"] for x in xs)
        tot = vals.get(f"tooling:{c}") if not session else None
        tot = tot if tot is not None else len(xs)
        b = {i: int(vals.get(f"tooling:{c}:{i}") or 0) if not session else by.get(i, 0) for i in serve.TOOL_INITS}
        if not session and not any(b.values()) and xs:
            b = {i: by.get(i, 0) for i in serve.TOOL_INITS}
        tiles.append(_mk_tile(ctx, f"tooling_{c}", {"skill": "Skills", "command": "Slash commands", "mcp": "MCP tools",
                                                    "plugin": "Plugins"}[c], tot, "count", str(int(tot)),
                              f"by you {b['human']} · by agent {b['agent']} · default {b['default']}",
                              ctx.user_ids([x["turn_id"] for x in xs])))
    names = Counter(clip(x["name"], 60) for x in tl if x.get("name"))
    top = names.most_common(5)
    tiles.append(_mk_tile(ctx, "top_tools", "Top tools", top[0][1] if top else None, "count",
                          top[0][0] if top else "—", " · ".join(f"{n} {k}" for n, k in top) or None,
                          ctx.user_ids([x["turn_id"] for x in tl if top and clip(x["name"] or "", 60) == top[0][0]])))
    try:
        runs = [r for r in serve.subagent_runs(conn, day) if not session or r["session_id"] == session]
    except Exception:  # noqa: BLE001 - diagnostic only
        runs = []
    n_sub = len(runs) if (session or vals.get("subagents") is None) else vals["subagents"]
    models = Counter((r.get("model") or "unknown").replace("claude-", "") for r in runs)
    tiles.append(_mk_tile(ctx, "subagents", "Subagents", n_sub, "count", str(int(n_sub or 0)),
                          ("models " + " · ".join(f"{m} {k}" for m, k in models.most_common(4))) if models else None,
                          ctx.user_ids([r["turn_id"] for r in runs])))
    if session:
        api = _rows(conn, "SELECT a.model, a.input_tok, a.cached_tok, a.output_tok, a.cost_usd FROM api_call a"
                          " JOIN turn t ON t.id=a.turn_id WHERE t.session_id=?", (session,))
        mix = Counter(a["model"] or "unknown" for a in api)
    else:
        mix = Counter({k[len("model_mix:"):]: v or 0 for k, v in vals.items() if k.startswith("model_mix:")})
    n_req = sum(mix.values())
    tiles.append(_mk_tile(ctx, "model_mix", "Model mix", n_req or None, "count",
                          (mix.most_common(1)[0][0].replace("claude-", "") if mix else "—"),
                          " · ".join(f"{m.replace('claude-', '')} {_pct(k / n_req)}" for m, k in mix.most_common(5))
                          if n_req else None, []))
    act, ov = vals.get("active_s"), vals.get("overlap_s")
    par = (ov / act) if act and ov is not None else None
    tiles.append(_mk_tile(ctx, "parallel", "Parallel work", par, "share", _pct(par),
                          f"{ov / 3600:.1f} h of {act / 3600:.1f} h active" if par is not None else None, []))
    intr = [i for s_ in ctx.sessions for i in s_["_interrupts"] if not session or s_["nativeId"] == session]
    thrown = vals.get("thrown_s")
    n_int = vals.get("interrupts") if not session and vals.get("interrupts") is not None else len(intr)
    tiles.append(_mk_tile(ctx, "interrupts", "Interrupts", n_int, "count", str(int(n_int or 0)),
                          f"{thrown / 60:.1f} min agent time thrown away" if thrown is not None else None,
                          [i["evidenceId"] for i in intr]))
    gm = vals.get("gap_median_s")
    gb = {b: int(vals.get(f"gap_bucket:{b}") or 0) for b in ("lt30s", "30s-2m", "2-5m", "5-15m", "break")}
    tiles.append(_mk_tile(ctx, "gaps", "Gap to your next prompt (median)", gm, "count",
                          "—" if gm is None else (f"{gm:.0f}s" if gm < 120 else f"{gm / 60:.1f} min"),
                          None if gm is None else f"<30s {gb['lt30s']} · 30s-2m {gb['30s-2m']} · 2-5m {gb['2-5m']}"
                                                  f" · 5-15m {gb['5-15m']} · break {gb['break']}", []))
    p50, p90 = vals.get("context_p50"), vals.get("context_p90")
    tiles.append(_mk_tile(ctx, "context", "Context used (p50)", None if p50 is None else p50 / 100, "share",
                          "—" if p50 is None else f"{round(p50)}%", None if p90 is None else f"p90 {round(p90)}%", []))
    if session:
        ti = sum(int(a["input_tok"] or 0) for a in api)
        tc = sum(int(a["cached_tok"] or 0) for a in api)
        to = sum(int(a["output_tok"] or 0) for a in api)
        cost = sum(a["cost_usd"] or 0 for a in api) if api else None
        by_model = Counter()
        for a in api:
            by_model[(a["model"] or "unknown").replace("claude-", "")] += a["cost_usd"] or 0
    else:
        ti, tc, to = vals.get("tokens_in"), vals.get("tokens_cached"), vals.get("tokens_out")
        cost = vals.get("cost_usd")
        by_model = Counter({k[len("cost_usd:"):].replace("claude-", ""): v or 0
                            for k, v in vals.items() if k.startswith("cost_usd:")})
    total = None if ti is None else (ti or 0) + (tc or 0) + (to or 0)
    tiles.append(_mk_tile(ctx, "tokens", "Tokens", total, "count", _fmt_n(total),
                          None if total is None else f"in {_fmt_n(ti)} · out {_fmt_n(to)} · cached {_fmt_n(tc)}", []))
    tiles.append(_mk_tile(ctx, "cost", "Cost", cost, "usd", "—" if cost is None else f"${cost:.2f}",
                          " · ".join(f"{m} ${v:.2f}" for m, v in by_model.most_common(4) if v) or None, []))
    return tiles


def _flat(rows: dict, asked, causes: dict) -> dict:
    def g(k):
        r = rows.get(k)
        v = r["value"] if r else None
        return None if v is None else max(0.0, float(v))

    flat = {"V": g("V"), "V_asked_by_you": None if asked is None else float(asked), "V_fail_share": g("V_fail_share"),
            "V_final": g("V_final"), "B_min": g("B_min"), "C": float(sum(causes.values())) if causes else g("C"),
            "H": g("H"), "H_repair": g("H_repair"), "H_own": g("H_own"),
            "ctx_p50": g("context_p50"), "ctx_p90": g("context_p90"), "cost_usd": g("cost_usd"),
            "interrupts": g("interrupts"), "thrown_min": None if g("thrown_s") is None else g("thrown_s") / 60,
            "active_h": None if g("active_s") is None else g("active_s") / 3600,
            "overlap_pct": None if not g("active_s") or g("overlap_s") is None else 100 * g("overlap_s") / g("active_s")}
    for c, n in causes.items():
        flat[f"C_cause_{_model_slug(c)}"[:40]] = float(n)
    for k, r in rows.items():
        if k.startswith("cost_usd:"):
            flat[f"cost_usd_{_model_slug(k[9:])}"[:40]] = g(k)
    return flat


def _content(ctx: _Ctx, f: dict, findings, corrections, episodes, demo, day) -> dict:
    moments = []
    for c, lab in episodes[:2]:
        ids = ctx.user_ids([c["turn_id"]], 1)
        if ids:
            moments.append({"title": _episode_title(lab["positive_episode"]),
                            "observation": clip(lab.get("episode_reason") or lab.get("evidence") or "", 300),
                            "attribution": "Candidate", "evidenceIds": ids})
    for c, lab in corrections:
        if len(moments) >= 5:
            break
        ids = ctx.user_ids([c["turn_id"]], 1)
        if not ids:
            continue
        cause = lab.get("cause") or "unknown"
        attribution = ("Agent" if cause == "agent_error" else "Candidate" if lab.get("preventable_by_user")
                       else "Unknown")
        moments.append({"title": f"Correction: {cause.replace('_', ' ')}",
                        "observation": f"Detected by {(lab.get('detection') or 'unknown').replace('_', ' ')}: "
                                       f"“{clip(lab.get('evidence') or '', 160)}”"
                                       + (f" → {clip(lab['user_action'], 140)}" if lab.get("user_action") else ""),
                        "attribution": attribution, "evidenceIds": ids})
    opportunities = []
    for a in (f.get("action_items") or [])[:4]:
        ids = ctx.user_ids(a.get("turn_ids"), 5)
        if ids:
            sug = a.get("text") or ""
            if a.get("claude_md_line"):
                sug += f" CLAUDE.md: “{a['claude_md_line']}”"
            opportunities.append({"title": clip(a.get("text") or "", 80), "suggestion": sug, "evidenceIds": ids})
    verdict = (f.get("verdict") or {}).get("text") or "No dominant cost today."
    d = f.get("diagnostics") or {}
    summary = (f"{verdict}. {int(d.get('sessions') or len(ctx.sessions))} sessions, {int(d.get('turns') or 0)} turns, "
               f"{d.get('active_h', 0)} h active, ${d.get('cost_usd') or 0:.2f}.")
    limitations = ["Judge labels are uncalibrated (κ not measured).", "Hand edits need git snapshots from hooks."]
    if demo:
        limitations.append("Includes synthetic demo data.")
    cat = (f.get("verdict") or {}).get("category")
    follow = {
        "unverified": ["Which check would you run before accepting an edit in this repo?",
                       "Where did a verified batch get edited again without a re-run?",
                       "Which unverified batch would have been cheapest to check?"],
        "blocked": ["What kept agents waiting on you the longest?", "Could the blocking prompts be batched?",
                    "Which sessions could run unattended with a done-when line?"],
    }.get(cat or "", ["What would you change in tomorrow's first prompt?",
                      "Which correction could a CLAUDE.md line have prevented?",
                      "Where did the agent choose the check instead of you?"])
    return {"title": f"How you worked with AI on {day}", "summary": summary, "findings": findings,
            "moments": moments, "opportunities": opportunities, "limitations": limitations, "followUps": follow}


def _build_attempt(conn, day, ctx: _Ctx, sid: str, att: dict) -> tuple[dict, dict]:
    """Candidate attempt report: per-session numbers only (day metrics mix other people)."""
    from .db import latest_run_id
    run_id = latest_run_id(conn)
    srows = {r["metric"]: r for r in _rows(conn, "SELECT id, metric, value, unit, n FROM metric_value"
                                                 " WHERE run_id=? AND scope_type='session' AND scope_id=?", (run_id, sid))}
    turns = [t for t, s in ctx.turn_sid.items() if s == ctx.aid[sid]]
    labels = [(c, lab) for c, lab in _labels(conn, day) if c["session_id"] == sid]
    episodes = [(c, lab) for c, lab in labels if lab.get("positive_episode") in POSITIVE]
    corrections = [(c, lab) for c, lab in labels if lab.get("event") == "correction"]
    cost = conn.execute("SELECT sum(cost_usd) FROM api_call a JOIN turn t ON t.id=a.turn_id WHERE t.session_id=?",
                        (sid,)).fetchone()[0]
    vrow = srows.get("V")
    ctx.metric_names = sorted(srows)
    vid = ctx.add_metric("V", vrow, [{"turn_id": t, "note": None} for t in turns[:3]]) if vrow else None
    first = ctx.user_ids(turns, 1)

    def tile(*a, **k):
        return _mk_tile(ctx, *a, **k)

    vv = vrow["value"] if vrow else None
    tiles = [tile("V", "Verified edit batches", vv, "share", _pct(vv), f"outcome {att.get('outcome', '—')}", [vid]),
             tile("C", "Corrections", len(corrections), "count", str(len(corrections)), None,
                  ctx.user_ids([c["turn_id"] for c, _ in corrections])),
             tile("episodes", "Did well", len(episodes), "count", str(len(episodes)),
                  " · ".join(dict.fromkeys(_episode_title(l["positive_episode"]) for _, l in episodes)) or None,
                  ctx.user_ids([c["turn_id"] for c, _ in episodes])),
             tile("H", "Hand edits", None, "count", "not measured", None, []),
             tile("B_min", "Agent blocked on you", None, "min", "—",
                  f"budget {att.get('budget_min', '—')} min", [])]
    fx = [_finding(d, "Insufficient evidence", "Low", "Not assessed for single attempts.", []) for d in DIMENSIONS]
    if vv is not None and vid:
        fx[3] = _finding("Verification judgment",
                         "Strongly demonstrated" if vv >= 0.75 else "Effective" if vv >= 0.5 else "Needs support",
                         _confidence(vrow["n"]), f"{_pct(vv)} of {vrow['n']} edit batches verified; "
                                                 f"outcome {att.get('outcome', 'unknown')}.", [vid])
    if att.get("clarification_asked") and first:
        fx[0] = _finding("Framing", "Effective", "Low", "Asked a clarification question before building.",
                         ctx.user_ids(turns, 3))
    f = {"verdict": {"text": f"{att.get('candidate', 'candidate')}: {att.get('outcome', 'unknown')}"},
         "diagnostics": {"sessions": 1, "turns": len(turns), "active_h": round((srows.get('active_s') or {}).get('value') or 0) / 3600,
                         "cost_usd": cost}}
    content = _content(ctx, f, fx, corrections, episodes, True, day)
    content["title"] = f"{att.get('candidate', 'Candidate')} · {att.get('challenge', 'challenge')}"
    sm = {"day": day, "tz": _tz(), "source": "scorer", "demo": True, "verdict": f["verdict"]["text"],
          "tiles": tiles, "leverage": _leverage(conn, day, ctx, {}, session=sid),
          "breakdown": {"cCauses": {}, "costByModel": []},
          "flat": {"V": vv, "C": float(len(corrections)), "cost_usd": cost}}
    return content, sm


def _report(ctx: _Ctx, rid: str, name: str, role: str, content: dict, sm: dict, cap: int) -> dict:
    snapshot = []
    for s in ctx.sessions:
        evs = sorted(ctx.evs[s["id"]], key=lambda e: e.line)
        d = {k: v for k, v in s.items() if not k.startswith("_")}
        d["events"] = [e.view(cap) for e in evs]
        d["truncated"] = any(e.get("truncated") for e in d["events"])
        d["fingerprint"] = hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()
        snapshot.append(d)
    sm = dict(sm)
    sm["lanes"] = [{"sessionId": s["id"], "agent": s["agent"], "repo": s["_repo"], "branch": s["_branch"],
                    "cwd": s["cwd"]} for s in ctx.sessions]
    sm["interrupts"] = [i for s in ctx.sessions for i in s["_interrupts"]]
    return {"id": rid, "status": "complete", "createdAt": _now(), "name": name, "role": role,
            "sessionIds": [s["id"] for s in ctx.sessions], "snapshot": snapshot, "content": content,
            "error": None, "coverage": [f"{s['title']}: {len(ctx.evs[s['id']])} records from the scorer DB "
                                        f"(prompts, tool calls/results, metric evidence)." for s in ctx.sessions],
            "scorerMetrics": sm}


def validate(report: dict) -> None:
    """Mirror of their validateContent + our tile rule: every cited id exists in the snapshot."""
    ids = {e["id"] for s in report["snapshot"] for e in s["events"]}
    c = report["content"]
    for item in [*c["findings"], *c["moments"], *c["opportunities"]]:
        missing = [i for i in item["evidenceIds"] if i not in ids]
        if missing:
            raise ValueError(f"unknown evidence ids {missing[:3]}")
        if item.get("rating") and item["rating"] != "Insufficient evidence" and not item["evidenceIds"]:
            raise ValueError("assessment without evidence")
        if item.get("rating") == "Insufficient evidence" and item["confidence"] != "Low":
            raise ValueError("unassessed must be Low")
    for t in report["scorerMetrics"]["tiles"] + report["scorerMetrics"].get("leverage", []):
        for e in t["evidence"]:
            if e["id"] not in ids:
                raise ValueError(f"tile {t['key']}: unknown evidence {e['id']}")


def build(conn: sqlite3.Connection, day: str, *, report_prefix: str | None = None, tz: str | None = None) -> dict:
    """Bundle: {day, demo, tz, reports: [{id, local (12k text), hosted (1800 text)}]}."""
    global _TZ_OVERRIDE
    _TZ_OVERRIDE = tz
    demo = _is_demo(conn)
    prefix = report_prefix or ("scorer-demo" if demo else "scorer")
    turns = _day_turns(conn, day)
    attempts = _attempts(conn, day)
    self_ids = sorted((s for s in turns if s not in attempts), key=lambda s: turns[s][0]["prompt_at"] or "")
    out = {"day": day, "demo": demo, "tz": _tz(), "reports": []}
    if self_ids:
        ctx = _Ctx(conn, day, self_ids, turns)
        content, sm = _build_day(conn, day, ctx, demo=demo)
        name = f"{'scorer demo' if demo else 'scorer'} · {day}"
        out["reports"].append(_pack(ctx, f"{prefix}-{day}", name, f"self · {day}", content, sm))
    for sid, att in sorted(attempts.items()):
        ctx = _Ctx(conn, day, [sid], turns)
        content, sm = _build_attempt(conn, day, ctx, sid, att)
        cand = re.sub(r"[^a-z0-9-]", "-", str(att.get("candidate") or sid).lower())
        out["reports"].append(_pack(ctx, f"scorer-attempt-{cand}-{day}", str(att.get("candidate") or sid),
                                    f"candidate · {att.get('challenge', 'challenge')} · {day}", content, sm))
    return out


def _pack(ctx, rid, name, role, content, sm) -> dict:
    local = _report(ctx, rid, name, role, content, sm, LOCAL_TEXT_CAP)
    validate(local)
    hosted = _report(ctx, rid, name, role, content, sm, HOSTED_TEXT_CAP)
    return {"id": rid, "local": local, "hosted": hosted}


# --- local writer -------------------------------------------------------------------------------

def write_local(bundle: dict, out: str | Path) -> list[str]:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    c = sqlite3.connect(out, timeout=10)
    try:
        c.execute("PRAGMA busy_timeout=10000")
        c.executescript(ARI_DDL)
        written = []
        with c:
            for r in bundle["reports"]:
                rep = r["local"]
                for s in rep["snapshot"]:
                    c.execute("INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                              "data=excluded.data, fingerprint=excluded.fingerprint",
                              (s["id"], json.dumps(s), s["fingerprint"]))
                c.execute("INSERT INTO reports VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                          "data=excluded.data, created_at=excluded.created_at",
                          (rep["id"], json.dumps(rep), rep["createdAt"]))
                written.append(rep["id"])
        return written
    finally:
        c.close()


# --- hosted sync --------------------------------------------------------------------------------

def token_path() -> Path:
    return scorer_home() / "hdyai.json"


def _http(method: str, url: str, body=None, token: str | None = None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw[:200].decode(errors="replace")}


def activate(url: str, code: str) -> dict:
    if not re.fullmatch(r"invite_[A-Za-z0-9_-]{43}", code):
        raise ValueError("activation code must look like invite_<43 chars>")
    st, data = _http("POST", url.rstrip("/") + "/api/activate", {"activationCode": code})
    if st != 200 or "deviceToken" not in data:
        raise RuntimeError(f"activation failed ({st}): {data.get('error', '')}")
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"url": url.rstrip("/"), **data}, fh)
    os.chmod(p, 0o600)
    return {"userId": data.get("userId"), "expiresAt": data.get("expiresAt")}


def _summary(s: dict) -> dict:
    keys = ("id", "nativeId", "agent", "title", "startedAt", "updatedAt", "parentId", "source", "hidden",
            "userCount", "assistantCount", "toolCount", "tools", "malformedLines", "truncated", "tasks",
            "tokenUsage", "capture")
    d = {k: s[k] for k in keys if k in s}
    d["title"] = d["title"][:300]
    return d


def _metrics_ts(sessions: list[dict]) -> dict:
    """Port of ari-version/lib/metrics.ts metrics() (summary-level fields)."""
    measured = [s for s in sessions if (s.get("tokenUsage") or {}).get("records")]
    known = [s for s in sessions if (s.get("tokenUsage") or {}).get("finalOutputCountKnown")]
    matched = [s for s in measured if s["tokenUsage"].get("finalOutputCountKnown")]

    def tot(ss):
        recs = [r for s in ss for r in s["tokenUsage"]["records"]]
        if not recs:
            return None
        k = ("inputTokens", "outputTokens", "cachedInputTokens", "cacheWriteInputTokens", "totalTokens")
        return {x: sum(r[x] for r in recs) for x in k}

    tt, mt = tot(measured), tot(matched)
    mfo = sum(len(s["tokenUsage"]["finalOutputs"]) for s in matched) if matched else None
    tasks = []
    for s in sessions:
        for t in s.get("tasks") or []:
            if not t.get("end"):
                continue
            a, b = parse_ts(t["start"]), parse_ts(t["end"])
            if a and b and b >= a:
                tasks.append({**t, "session": s["id"], "a": a.timestamp() * 1000, "b": b.timestamp() * 1000})
    bounds = sorted([(t["a"], t["session"], 1) for t in tasks] + [(t["b"], t["session"], -1) for t in tasks],
                    key=lambda x: (x[0], x[2]))
    active: dict[str, int] = {}
    peak, parallel = 0, set()
    for _, sid, dlt in bounds:
        active[sid] = active.get(sid, 0) + dlt
        if active[sid] == 0:
            del active[sid]
        peak = max(peak, len(active))
        if len(active) > 1:
            parallel.update(active)
    complete = [t for t in tasks if not t["aborted"]]
    return {"tokenTotals": tt, "tokenUsageSessions": len(measured), "finalOutputSessions": len(known),
            "finalOutputs": sum(len(s["tokenUsage"]["finalOutputs"]) for s in known) if known else None,
            "tokenMetricSessions": len(matched), "tokenMetricTotalTokens": mt["totalTokens"] if mt else None,
            "tokenMetricFinalOutputs": mfo,
            "tokensPerFinalOutput": (mt["totalTokens"] / mfo) if mt and mfo else None,
            "sessions": len(sessions), "candidateMessages": sum(s["userCount"] for s in sessions),
            "toolCalls": sum(s["toolCount"] for s in sessions),
            "uniqueTools": len({t for s in sessions for t in s["tools"]}),
            "interruptions": sum(1 for t in tasks if t["aborted"]), "completedTasks": len(complete),
            "parallelSessions": len(parallel), "peakConcurrency": peak,
            "longestTaskMs": max((t["b"] - t["a"] for t in complete), default=None),
            "timingSessions": len({t["session"] for t in tasks})}


def analysis_request(packed: dict, hosted_id: str) -> dict:
    """analysisRequest (strict) with a sampled evidence set under PROMPT_BUDGET chars."""
    rep = packed["hosted"]
    flat = {f"scorer_{k}"[:40]: (None if v is None else max(0.0, float(v)))
            for k, v in rep["scorerMetrics"]["flat"].items()}
    m = _metrics_ts(rep["snapshot"])
    for k, v in m.items():
        if k == "tokenTotals":
            for x in ("inputTokens", "outputTokens", "cachedInputTokens", "cacheWriteInputTokens", "totalTokens"):
                flat.setdefault(f"token{x[0].upper()}{x[1:]}"[:40], None if v is None else float(v[x]))
        else:
            flat.setdefault(k[:40], None if v is None else float(v))
    cited = {i for x in [*rep["content"]["findings"], *rep["content"]["moments"], *rep["content"]["opportunities"]]
             for i in x["evidenceIds"]}
    sessions = rep["snapshot"][:200]
    budget = PROMPT_BUDGET - 8_000 - len(json.dumps(flat)) - len(rep["role"]) - 400 * len(sessions)
    per = max(2_000, budget // max(1, len(sessions)))
    inp = []
    for s in sessions:
        evs = [e for e in s["events"] if e["kind"] != "metric"]  # old service rejects kind 'metric'
        prio = [e for e in evs if e["kind"] == "user" or e["id"] in cited]
        rest = [e for e in evs if e not in prio]
        step = max(1, len(rest) // 40)
        chosen, size = [], 0
        for e in prio + rest[::step]:
            n = len(json.dumps(e)) + 10
            if size + n > per and e["id"] not in cited:
                continue
            chosen.append(e)
            size += n
            if len(chosen) >= 2000:
                break
        chosen.sort(key=lambda e: e["line"])
        inp.append({"id": s["id"], "agent": s["agent"], "title": s["title"][:300], "parentId": s["parentId"],
                    "source": s["source"], "startedAt": s["startedAt"], "updatedAt": s["updatedAt"],
                    "counts": {"candidateMessages": s["userCount"], "agentMessages": s["assistantCount"],
                               "toolCalls": s["toolCount"]}, "evidence": chosen})
    return {"requestId": hosted_id, "name": rep["name"][:100], "role": rep["role"][:1000],
            "metrics": flat, "input": inp}


def hosted_id(packed: dict) -> str:
    rep = packed["hosted"]
    body = json.dumps({k: rep[k] for k in ("sessionIds", "content", "scorerMetrics")}, sort_keys=True)
    return f"{packed['id']}-{hashlib.sha1(body.encode()).hexdigest()[:8]}"


def published_report(packed: dict, rid: str, job: dict, req: dict) -> dict:
    """Report row for /api/sync once Eve's job is complete: content byte-identical to the job's."""
    rep = packed["hosted"]
    content = job["content"]
    used = {i for x in [*content["findings"], *content["moments"], *content["opportunities"]] for i in x["evidenceIds"]}
    sent = {e["id"]: (e, s) for s in req["input"] for e in s["evidence"]}
    index = [{**e, "sessionTitle": s["title"], "agent": s["agent"]} for i, (e, s) in sent.items() if i in used]
    m = _metrics_ts(rep["snapshot"])
    activity = [{"id": s["id"], "agent": s["agent"], "title": s["title"][:300], "tasks": s["tasks"],
                 "userCount": s["userCount"], "toolCount": s["toolCount"]} for s in rep["snapshot"]]
    return {"id": rid, "status": "complete", "createdAt": rep["createdAt"], "name": rep["name"][:100],
            "role": rep["role"][:1000], "sessionIds": rep["sessionIds"], "content": content, "error": None,
            "coverage": rep["coverage"], "metrics": m, "timezone": rep["scorerMetrics"]["tz"],
            "activity": activity, "evidenceIndex": index, "scorerMetrics": rep["scorerMetrics"]}


def sync(bundle: dict, url: str, token: str | None = None, *, dry_run: str | None = None,
         poll_s: float = 5.0, timeout_s: float = 300.0, echo=print) -> list[dict]:
    """Per report: /api/sync (summaries) -> /api/analyze -> poll -> /api/sync (report)."""
    url = url.rstrip("/")
    results = []
    plans = []
    for packed in bundle["reports"]:
        rid = hosted_id(packed)
        req = analysis_request(packed, rid)
        plans.append((packed, rid, req))
    if dry_run:
        Path(dry_run).write_text(json.dumps([{"id": rid, "sync": {"sessions": [_summary(s) for s in p["hosted"]["snapshot"]],
                                                                    "reportName": p["hosted"]["name"][:100]},
                                              "analyze": req} for p, rid, req in plans], indent=1))
        return [{"id": rid, "dry_run": dry_run, "prompt_chars": len(json.dumps(req))} for _, rid, req in plans]
    if not token:
        try:
            token = json.loads(token_path().read_text())["deviceToken"]
        except (OSError, ValueError, KeyError):
            raise RuntimeError("no device token: run `scorer sync --activate invite_…` first")
    for packed, rid, req in plans:
        summaries = [_summary(s) for s in packed["hosted"]["snapshot"]]
        st, data = _http("POST", f"{url}/api/sync", {"sessions": summaries, "reportName": req["name"]}, token)
        if st != 200:
            raise RuntimeError(f"/api/sync failed ({st}): {data.get('error', '')}")
        st, data = _http("POST", f"{url}/api/analyze", req, token)
        if st not in (200, 202):
            raise RuntimeError(f"/api/analyze failed ({st}): {data.get('error', '')}")
        job_id = data.get("jobId") or data.get("id")
        job = data
        t_end = time.monotonic() + timeout_s
        while job.get("status") not in ("complete", "failed") and job_id:
            if time.monotonic() > t_end:
                raise RuntimeError(f"analysis {job_id} not complete after {timeout_s:.0f}s")
            time.sleep(poll_s)
            st, job = _http("GET", f"{url}/api/analyses/{job_id}", None, token)
            if st != 200:
                raise RuntimeError(f"/api/analyses failed ({st}): {job.get('error', '')}")
            echo(f"  {rid}: {job.get('status')}")
        if job.get("status") != "complete" or not job.get("content"):
            raise RuntimeError(f"analysis failed: {job.get('error', 'no content')}")
        pub = published_report(packed, rid, job, req)
        st, data = _http("POST", f"{url}/api/sync", {"sessions": summaries, "report": pub,
                                                     "reportName": req["name"]}, token)
        if st != 200:
            raise RuntimeError(f"/api/sync(report) failed ({st}): {data.get('error', '')}")
        results.append({"id": rid, "job": job_id, "cited": len(pub["evidenceIndex"])})
    return results
