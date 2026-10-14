"""Deterministic metrics (spec §1/§3). `compute_all(conn, run_id)` writes metric_value +
metric_evidence for one run (idempotent: deletes that run's rows first) and commits.

Scopes: scope_type "day" (scope_id = local YYYY-MM-DD), "session" (session id), "all" (scope_id "all").
All interval math uses model.parse_ts; every input column may be NULL.

Pure helpers for report / dashboard (all read-only, `day` = local "YYYY-MM-DD" or None = all days):

  day_bounds(day) -> (start: datetime, end: datetime)   # report day = cutoff..cutoff in model.local_tz()
  turn_intervals(conn, day=None) -> list[(session_id, turn_id, start: datetime, end: datetime)]
      start = turn.prompt_at, end = turn.agent_finished_at; turns without both are skipped.
      With `day`, intervals are clipped to day_bounds(day) (turns spanning the cutoff are split).
  union_seconds(intervals) -> float               # length of the union of (start, end) pairs
  overlap_seconds(intervals) -> float             # time covered by >= 2 distinct sessions
  active_overlap(conn, day=None) -> (active_s, overlap_s)
  gaps(conn, day=None) -> list[Gap]               # consecutive turns per session, see Gap
  gap_buckets(gap_list) -> dict[bucket, count]    # GAP_BUCKETS order; "break" = > 15 min
  interruptions(conn, day=None) -> list[dict(turn_id, session_id, at, agent_s)]
  verification_batches(conn, day=None) -> list[Batch]   # V: edit batches with tier + verifier
  hand_edits(conn, day=None) -> dict(count, user_modified, repair, own, partial, evidence=[...])
  blocked(conn, day=None) -> list[dict(session_id, turn_id, blocked_s, attended, gap_s)]
  api_rows(conn, day=None) -> list[dict]          # api_call rows + derived window/cost/occupancy
  days(conn) -> list[str]                         # local days that have turns

Metric ids written (day scope unless noted; also "all" scope for the headline set):
  active_s, overlap_s, parallel_s, turns, sessions,
  gap_bucket:<lt30s|30s-2m|2-5m|5-15m|break>, gap_median_s, switch_share,
  interrupts, thrown_s, queued_prompts,
  V, V_fail_share, V_final, V_human, V_tier:<verified_pass|verified_fail|inspected|none>,
  H (unit "edits" or "edits(partial)"), H_repair, H_own,
  B_min,
  context_p50, context_p90 (percent of window), tokens_in, tokens_cached, tokens_out,
  cost_usd (api_call.cost_usd as stored; recomputed via prices only when NULL), unpriced_calls,
  model_mix:<model> (calls), tokens:<model> (in+cached+out), cost_usd:<model>, subagents, compactions, tool_calls, tool_error_share,
  orientation_s (session scope + day median),
  tooling:<category>:<initiator>, tooling:<category> (evidence per distinct name), tooling_human_share.
  tooling(conn, day=None) -> list[dict(category, name, initiator, rule, tool_use_id, turn_id, session_id, day)]
"""
from __future__ import annotations

import json
import os
from bisect import bisect_left, bisect_right
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from . import classify, prices
from .model import DEFINITION_VERSION, clip, local_day, local_tz, parse_ts

GAP_BUCKETS = ("lt30s", "30s-2m", "2-5m", "5-15m", "break")
BREAK_S = 15 * 60
ATTENDED_GAP_S = 120
ATTENDED_WORDS = 5


# --- loading -----------------------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


_CACHE: dict = {}


def _cached(fn):
    """Memoize loaders while compute_all runs (cache is cleared on exit)."""
    def wrap(conn):
        key = (id(conn), fn.__name__)
        if _CACHE.get("on") and key in _CACHE:
            return _CACHE[key]
        v = fn(conn)
        if _CACHE.get("on"):
            _CACHE[key] = v
        return v
    wrap.__name__ = fn.__name__
    return wrap


def _human_lane(t: dict) -> bool:
    """False for agent-driven turns: initiator="agent", or any turn of a Codex subagent/review thread
    (session.forked_from set on a codex session). Claude forks/resumes (forked_from set by the Claude
    parser) are human-driven and stay in."""
    if t.get("initiator") == "agent":
        return False
    return not (t.get("s_agent") == "codex" and t.get("s_forked"))


@_cached
def _turns(conn) -> list[dict]:
    rows = _rows(conn, "SELECT t.*, s.cwd AS s_cwd, s.repo AS s_repo, s.agent AS s_agent, s.started_at AS s_started,"
                       " s.forked_from AS s_forked FROM turn t LEFT JOIN session s ON s.id = t.session_id")
    for r in rows:
        r["human"] = _human_lane(r)
        r["start"] = parse_ts(r["prompt_at"])
        r["end"] = parse_ts(r["agent_finished_at"])
        r["day"] = local_day(r["start"])
    rows.sort(key=lambda r: (r["session_id"] or "", r["start"].timestamp() if r["start"] else 0, r["idx"] or 0))
    return rows


def days(conn) -> list[str]:
    return sorted({t["day"] for t in _turns(conn) if t["day"]})


def day_bounds(day: str) -> tuple[datetime, datetime]:
    """Report day -> [start, end): from day_cutoff to the next day's cutoff in model.local_tz()
    (consistent with model.local_day)."""
    from .config import load
    c = load().day_cutoff
    tz = local_tz()
    d = datetime.strptime(day, "%Y-%m-%d").date()
    lo = datetime.combine(d, c).replace(tzinfo=tz)
    hi = datetime.combine(d + timedelta(days=1), c).replace(tzinfo=tz)
    return lo, hi


_day_bounds = day_bounds


# --- intervals ---------------------------------------------------------------------------------

def turn_intervals(conn, day: str | None = None) -> list[tuple[str, int, datetime, datetime]]:
    out = []
    lo, hi = _day_bounds(day) if day else (None, None)
    for t in _turns(conn):
        if not t["human"]:
            continue
        s, e = t["start"], t["end"]
        if not s or not e or e <= s:
            continue
        if day:
            s, e = max(s, lo), min(e, hi)
            if e <= s:
                continue
        out.append((t["session_id"], t["id"], s, e))
    return out


def union_seconds(iv) -> float:
    pts = sorted((x[-2], x[-1]) for x in iv)
    total, cur_s, cur_e = 0.0, None, None
    for s, e in pts:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return total


def _session_unions(iv) -> dict[str, list[tuple[datetime, datetime]]]:
    by: dict[str, list] = {}
    for x in iv:
        by.setdefault(x[0], []).append((x[-2], x[-1]))
    out = {}
    for sid, lst in by.items():
        lst.sort()
        merged: list[list] = []
        for s, e in lst:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out[sid] = [(a, b) for a, b in merged]
    return out


def overlap_seconds(iv) -> float:
    """Time covered by >= 2 distinct sessions (sweep over per-session unions)."""
    ev = []
    for lst in _session_unions(iv).values():
        for s, e in lst:
            ev.append((s, 1))
            ev.append((e, -1))
    ev.sort(key=lambda x: (x[0], x[1]))
    total, n, prev = 0.0, 0, None
    for t, d in ev:
        if prev is not None and n >= 2:
            total += (t - prev).total_seconds()
        n += d
        prev = t
    return total


def active_overlap(conn, day: str | None = None) -> tuple[float, float]:
    iv = turn_intervals(conn, day)
    return union_seconds(iv), overlap_seconds(iv)


def _intersect_s(a: datetime, b: datetime, ivs: list[tuple[datetime, datetime]]) -> float:
    """Length of [a,b] ∩ union(ivs); ivs must be merged (non-overlapping) and sorted."""
    tot = 0.0
    i = max(0, bisect_right(ivs, (a, a)) - 1)
    for s, e in ivs[i:]:
        if s >= b:
            break
        lo, hi = max(a, s), min(b, e)
        if hi > lo:
            tot += (hi - lo).total_seconds()
    return tot


def _merge(lst):
    lst = sorted(lst)
    out: list[list] = []
    for s, e in lst:
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


# --- gaps --------------------------------------------------------------------------------------

@dataclass(eq=False)
class Gap:
    session_id: str
    prev_turn_id: int
    turn_id: int
    day: str | None
    gap_s: float
    bucket: str
    switched: bool
    prev_end: datetime
    next_start: datetime
    words: int


def _bucket(s: float) -> str:
    if s < 30:
        return "lt30s"
    if s < 120:
        return "30s-2m"
    if s < 300:
        return "2-5m"
    if s <= BREAK_S:
        return "5-15m"
    return "break"


def _enqueue_map(conn) -> dict[int, datetime]:
    out = {}
    for r in _rows(conn, "SELECT turn_id, at FROM event WHERE kind='queue_enqueue' AND turn_id IS NOT NULL"):
        ts = parse_ts(r["at"])
        if ts and (r["turn_id"] not in out or ts < out[r["turn_id"]]):
            out[r["turn_id"]] = ts
    return out


def _words(text: str | None) -> int:
    return len((text or "").split())


def _command_only(t: dict) -> bool:
    """A slash command the agent never worked on (/exit, /clear, ...): no human wait before or after it."""
    return t["prompt_kind"] == "command" and not t["agent_started_at"]


def _abandoned(t: dict) -> bool:
    """Prompt interrupted before any agent output (Esc, then resubmitted): no agent end to wait after."""
    return t["end_source"] == "interrupt" and not t["agent_started_at"]


def gaps(conn, day: str | None = None) -> list[Gap]:
    """Per session, consecutive human-lane turns. Command-only turns are skipped (the gap bridges them);
    an abandoned turn starts no gap (its resubmission is not a new human response), but the gap that
    ends at it counts (that is when the human responded)."""
    turns = [t for t in _turns(conn) if t["human"] and not _command_only(t)]
    enq = _enqueue_map(conn)
    prompts = sorted((t["start"], t["session_id"] or "") for t in turns if t["start"])
    pts = [p[0] for p in prompts]
    out = []
    by: dict[str, list] = {}
    for t in turns:
        by.setdefault(t["session_id"], []).append(t)
    for sid, lst in by.items():
        for prev, nxt in zip(lst, lst[1:]):
            if not prev["end"] or not nxt["start"]:
                continue  # crash/unknown end -> undefined
            if _abandoned(prev):
                continue
            if day and nxt["day"] != day:
                continue
            start = enq.get(nxt["id"]) if (nxt["prompt_kind"] == "queued" or nxt["id"] in enq) else None
            start = start or nxt["start"]
            g = max(0.0, (start - prev["end"]).total_seconds())
            if nxt["prompt_kind"] == "queued" and nxt["id"] not in enq:
                g = 0.0
            lo_i, hi_i = bisect_right(pts, prev["end"]), bisect_left(pts, nxt["start"])
            sw = any(prompts[k][1] != (sid or "") for k in range(lo_i, hi_i))
            out.append(Gap(sid, prev["id"], nxt["id"], nxt["day"], g, _bucket(g), sw, prev["end"], nxt["start"],
                           _words(nxt["prompt_text"])))
    return out


def gap_buckets(gap_list: list[Gap]) -> dict[str, int]:
    c = {b: 0 for b in GAP_BUCKETS}
    for g in gap_list:
        c[g.bucket] += 1
    return c


# --- interruptions -----------------------------------------------------------------------------

def interruptions(conn, day: str | None = None) -> list[dict]:
    tmap = {t["id"]: t for t in _turns(conn)}
    out = []
    for e in _rows(conn, "SELECT session_id, turn_id, at FROM event WHERE kind='interrupt'"):
        t = tmap.get(e["turn_id"])
        if t and not t["human"]:
            continue  # agent/subagent thread interrupt, not a human interruption
        at = parse_ts(e["at"])
        start = t["start"] if t else None
        d = (t["day"] if t else None) or local_day(at)
        if day and d != day:
            continue
        agent_s = max(0.0, (at - start).total_seconds()) if (at and start) else None
        out.append({"turn_id": e["turn_id"], "session_id": e["session_id"], "at": e["at"], "agent_s": agent_s,
                    "day": d})
    return out


# --- queued prompts ----------------------------------------------------------------------------

def queued_prompts(conn, day: str | None = None) -> list[dict]:
    """Prompts typed while the agent was busy: turns with prompt_kind="queued", plus queued prompts Claude
    absorbed into the running turn (queue remove reason=absorbed_mid_turn; no turn of their own)."""
    tmap = {t["id"]: t for t in _turns(conn)}
    out = [{"turn_id": t["id"], "session_id": t["session_id"], "day": t["day"], "absorbed": False}
           for t in tmap.values() if t["human"] and t["prompt_kind"] == "queued" and (not day or t["day"] == day)]
    for e in _rows(conn, "SELECT session_id, turn_id, at, data FROM event WHERE kind='queue_enqueue'"
                         " AND data LIKE '%absorbed_mid_turn%'"):
        t = tmap.get(e["turn_id"])
        if t is not None and not t["human"]:
            continue
        d = (t["day"] if t else None) or local_day(e["at"])
        if not day or d == day:
            out.append({"turn_id": e["turn_id"], "session_id": e["session_id"], "day": d, "absorbed": True})
    return out


# --- verification ------------------------------------------------------------------------------

@dataclass(eq=False)
class Batch:
    session_id: str
    turn_id: int
    day: str | None
    edit_call_ids: list[int]
    paths: list[str]
    last_edit_call_id: int
    tier: str = "none"                     # verified_pass | verified_fail | inspected | none
    verifier_call_id: int | None = None
    verifier_initiator: str | None = None
    relevance: str | None = None
    followed_by_edit: bool = False         # another batch later in the same turn
    final: bool = False                    # last batch of its session (in scope) and verified
    order: tuple = field(default=(), repr=False)


@_cached
def _tool_calls(conn) -> list[dict]:
    rows = _rows(conn, "SELECT * FROM tool_call WHERE turn_id IS NOT NULL")
    for r in rows:
        if r["kind"] is None:
            r["kind"] = classify.tool_kind(r["name"] or "")
        if r["kind"] == "bash" and not r["bash_intent"]:
            r["bash_intent"] = classify.bash_intent(r["args_summary"] or "")
        r["ts"] = parse_ts(r["started_at"])
    return rows


@_cached
def _file_edits(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in _rows(conn, "SELECT * FROM file_edit"):
        out.setdefault(r["tool_call_id"], []).append(r)
    return out


def _is_edit(c: dict) -> bool:
    return c["kind"] == "edit" or (c["kind"] == "bash" and c["bash_intent"] == "edit")


def _roots(t: dict | None) -> tuple:
    return tuple(r for r in ((t or {}).get("s_cwd"), (t or {}).get("s_repo")) if r)


def _v_paths(c: dict, fe: dict[int, list[dict]], t: dict | None) -> list[str]:
    """Edited paths that count for V: inside the session's cwd/repo tree, not "." or scratch space."""
    rs = _roots(t)
    return [p for p in _edit_paths(c, fe) if classify.v_edit_path(p, rs)]


def _is_v_edit(c: dict, fe: dict[int, list[dict]], t: dict | None) -> bool:
    """An edit call for V batches: an edit whose paths are unknown, or with at least one V path
    (a cp/rm in /tmp or the agent's scratchpad is not a code edit)."""
    if not _is_edit(c):
        return False
    ps = _edit_paths(c, fe)
    return not ps or bool(_v_paths(c, fe, t))


def _is_check(c: dict) -> bool:
    return c["kind"] == "bash" and c["bash_intent"] in ("test", "build")


def _edit_paths(c: dict, fe: dict[int, list[dict]]) -> list[str]:
    ps = [r["path"] for r in fe.get(c["id"], []) if r["path"]]
    if not ps and c["kind"] == "bash":
        ps = classify.bash_edit_paths(c["args_summary"] or "")
    if not ps and c["kind"] == "edit" and c["args_summary"]:
        ps = [c["args_summary"]]
    return ps


@_cached
def _calls_by_turn_c(conn) -> dict[int, list[dict]]:
    return _calls_by_turn(_tool_calls(conn))


def _calls_by_turn(calls: list[dict]) -> dict[int, list[dict]]:
    by: dict[int, list[dict]] = {}
    for c in calls:
        by.setdefault(c["turn_id"], []).append(c)
    for lst in by.values():
        lst.sort(key=lambda c: (c["ts"].timestamp() if c["ts"] else float("inf"), c["id"]))
    return by


def _mentions(arg: str | None, paths: list[str]) -> bool:
    a = arg or ""
    return any(p and (p in a or os.path.basename(p) in a.split()) for p in paths)


def verification_batches(conn, day: str | None = None) -> list[Batch]:
    turns = _turns(conn)
    calls = _calls_by_turn_c(conn)
    fe = _file_edits(conn)
    out: list[Batch] = []
    for ti, t in enumerate(turns):
        if day and t["day"] != day:
            continue
        lst = calls.get(t["id"], [])
        cwd = t["s_cwd"]
        batches: list[tuple[Batch, int]] = []   # (batch, index where the verifier scan starts)
        cur: Batch | None = None
        for i, c in enumerate(lst):
            if _is_v_edit(c, fe, t):
                if cur is None:
                    cur = Batch(t["session_id"], t["id"], t["day"], [], [], c["id"], order=(ti, i))
                    batches.append((cur, i + 1))
                cur.edit_call_ids.append(c["id"])
                cur.last_edit_call_id = c["id"]
                for p in _v_paths(c, fe, t) or _edit_paths(c, fe):
                    if p not in cur.paths:
                        cur.paths.append(p)
                batches[-1] = (cur, i + 1)
            elif _is_check(c):
                cur = None
                # `cat > f <<EOF … && pytest`: one call edits, then checks -> its own batch, verified by itself
                own = [r["path"] for r in fe.get(c["id"], []) if r["path"]
                       and classify.v_edit_path(r["path"], _roots(t))]
                if own and classify.edit_precedes_check(c["args_summary"] or ""):
                    b = Batch(t["session_id"], t["id"], t["day"], [c["id"]], list(dict.fromkeys(own)), c["id"],
                              order=(ti, i))
                    batches.append((b, i))
        for bi, (b, scan_from) in enumerate(batches):
            b.followed_by_edit = bi + 1 < len(batches)
            inspected = None
            for c in lst[scan_from:]:
                if _is_check(c):
                    if not c["has_result"]:
                        continue
                    rel = classify.is_relevant(c["args_summary"] or "", cwd, b.paths)
                    if rel:
                        b.tier = "verified_fail" if c["is_error"] else "verified_pass"
                        b.verifier_call_id, b.verifier_initiator, b.relevance = c["id"], c["initiator"], rel
                        break
                elif inspected is None and not _is_v_edit(c, fe, t):
                    if c["kind"] == "read" and _mentions(c["args_summary"], b.paths):
                        inspected = c
                    elif c["kind"] == "bash" and c["bash_intent"] == "git" and \
                            classify.bash_git_subkind(c["args_summary"] or "") == "read" and \
                            "diff" in (c["args_summary"] or ""):
                        inspected = c
            if b.tier == "none" and inspected is not None:
                b.tier, b.verifier_call_id = "inspected", inspected["id"]
            out.append(b)
    # final: last batch per session (within scope) verified with no later edit
    last: dict[str, Batch] = {}
    for b in out:
        if b.session_id not in last or b.order > last[b.session_id].order:
            last[b.session_id] = b
    for b in last.values():
        b.final = b.tier in ("verified_pass", "verified_fail")
    return out


# --- hand edits --------------------------------------------------------------------------------

def _snapshot_paths(data) -> list[str] | None:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    g = data.get("git") if isinstance(data.get("git"), dict) else data
    for k in ("changed", "changed_paths", "changed_files", "files", "paths", "dirty"):
        v = g.get(k)
        if isinstance(v, list):
            return [x if isinstance(x, str) else (x.get("path") if isinstance(x, dict) else None) for x in v
                    if x]
    return None


def _snapshot_meta(data) -> tuple[bool, set]:
    """(baseline?, untracked paths) of a git_snapshot event's data."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return False, set()
    if not isinstance(data, dict):
        return False, set()
    g = data.get("git") if isinstance(data.get("git"), dict) else data
    u = g.get("untracked_paths")
    return bool(data.get("baseline")), set(x for x in u if isinstance(x, str)) if isinstance(u, list) else set()


def _snapshot_states(data) -> dict:
    """Optional per-file state {path: hash} in a snapshot (data.git.path_states / file_hashes)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    if not isinstance(data, dict):
        return {}
    g = data.get("git") if isinstance(data.get("git"), dict) else data
    for k in ("path_states", "file_hashes", "states"):
        v = g.get(k)
        if isinstance(v, dict):
            return v
    return {}


def hand_edits(conn, day: str | None = None) -> dict:
    """H: file_edit.user_modified rows + git-snapshot deltas. Snapshots (kind=git_snapshot, data.git.changed_paths
    = everything differing from HEAD at that Stop) are diffed per session against the previous snapshot: a path
    counts in the snapshot where it first appears (or its per-file state changes, if provided), unless an agent
    edit touched it in that session since the previous snapshot. Repair = path agent-edited earlier in the session."""
    tmap = {t["id"]: t for t in _turns(conn)}
    um = _rows(conn, "SELECT fe.path, fe.tool_call_id, tc.turn_id FROM file_edit fe"
                     " JOIN tool_call tc ON tc.id = fe.tool_call_id WHERE fe.user_modified = 1")
    um = [r for r in um if not day or (tmap.get(r["turn_id"]) or {}).get("day") == day]
    ev = []
    for r in um:
        ev.append({"turn_id": r["turn_id"], "tool_call_id": r["tool_call_id"],
                   "note": "user_modified: " + clip(r["path"], 60)})
    snaps = _rows(conn, "SELECT session_id, turn_id, at, data FROM event WHERE kind = 'git_snapshot'")
    repair = own = 0
    have_snap = False
    if snaps:
        fe = _file_edits(conn)
        # per session: [(ts, paths)] of agent edits (tool ts, else turn start)
        agent: dict[str, list] = {}
        for c in _tool_calls(conn):
            t = tmap.get(c["turn_id"])
            if not t or not _is_edit(c):
                continue
            at = c["ts"] or t["start"]
            if at:
                agent.setdefault(t["session_id"], []).append((at, set(_edit_paths(c, fe))))
        by_sess: dict[str, list] = {}
        for sn in snaps:
            at = parse_ts(sn["at"])
            ps = _snapshot_paths(sn["data"])
            if at is None or ps is None:
                continue
            have_snap = True
            base, untracked = _snapshot_meta(sn["data"])
            by_sess.setdefault(sn["session_id"], []).append((at, sn["turn_id"], ps, _snapshot_states(sn["data"]),
                                                             base, untracked))
        for sid, lst in by_sess.items():
            lst.sort(key=lambda x: x[0])
            prev_at, prev_paths, prev_states = None, set(), {}
            for at, turn_id, ps, states, base, untracked in lst:
                cur = set(ps)
                if base:
                    if prev_at is None:   # SessionStart state: what was dirty before the agent ran
                        prev_at, prev_paths, prev_states = at, cur, states
                    continue
                # new = first appearance vs the previous snapshot, or per-file state changed (if provided).
                # Without a baseline, untracked files in the session's first snapshot may predate it: skip them.
                new = [p for p in ps if (p not in prev_paths and not (prev_at is None and p in untracked))
                       or (states.get(p) is not None and prev_states.get(p) is not None
                           and states[p] != prev_states[p])]
                t = tmap.get(turn_id)
                d = (t["day"] if t else None) or local_day(at)
                window = set().union(*[pp for ts_, pp in agent.get(sid, [])
                                       if (prev_at is None or ts_ > prev_at) and ts_ <= at])
                before = set().union(*[pp for ts_, pp in agent.get(sid, [])
                                       if prev_at is not None and ts_ <= prev_at])
                if not day or d == day:
                    for p in new:
                        if _path_in(p, window):
                            continue  # the agent changed it since the last snapshot
                        if _path_in(p, before):
                            repair += 1
                            kind = "repair"
                        else:
                            own += 1
                            kind = "own"
                        ev.append({"turn_id": turn_id, "tool_call_id": None, "note": f"{kind}: " + clip(p, 60)})
                prev_at, prev_paths, prev_states = at, cur, states
    return {"count": len(um) + repair + own, "user_modified": len(um), "repair": repair, "own": own,
            "partial": not have_snap, "evidence": ev}


def _path_in(p: str, paths: set) -> bool:
    if p.endswith("/"):   # collapsed untracked dir (`?? newdir/`): any agent edit below it
        d = p.rstrip("/")
        return any(q == d or q.startswith(d + "/") or ("/" + d + "/") in q for q in paths)
    return any(p == q or q.endswith("/" + p) or p.endswith("/" + q) for q in paths)


# --- blocked -----------------------------------------------------------------------------------

@_cached
def _others(conn):
    """session_id -> merged intervals of all *other* sessions (memoized per session)."""
    per_sess = _session_unions(turn_intervals(conn))
    memo: dict[str, list] = {}

    def get(sid):
        if sid not in memo:
            memo[sid] = _merge([iv for s, lst in per_sess.items() if s != sid for iv in lst])
        return memo[sid]
    return get


def blocked(conn, day: str | None = None) -> list[dict]:
    """Per gap: seconds the session waited on the human while another session was active,
    or (attended gap) the whole gap when < 2 min and the next prompt is <= 5 words. Each gap counted once.
    Gaps > 15 min (breaks) are excluded."""
    others_of = _others(conn)
    out = []
    for g in gaps(conn, day):
        if g.bucket == "break":
            continue  # > 15 min = break, not waiting (same exclusion as gap buckets)
        others = others_of(g.session_id)
        b = _intersect_s(g.prev_end, g.next_start, others) if g.next_start > g.prev_end else 0.0
        attended = g.gap_s < ATTENDED_GAP_S and g.words <= ATTENDED_WORDS
        if attended:
            b = max(b, g.gap_s)
        if b > 0:
            out.append({"session_id": g.session_id, "turn_id": g.turn_id, "blocked_s": b, "attended": attended,
                        "gap_s": g.gap_s, "day": g.day})
    return out


# --- api / diagnostics -------------------------------------------------------------------------

def api_rows(conn, day: str | None = None) -> list[dict]:
    tmap = {t["id"]: t for t in _turns(conn)}
    out = []
    for r in _rows(conn, "SELECT * FROM api_call"):
        t = tmap.get(r["turn_id"])
        d = (t["day"] if t else None) or local_day(r["at"])
        if day and d != day:
            continue
        ctx = (r["input_tok"] or 0) + (r["cached_tok"] or 0)
        w = r["context_window"] or prices.context_window(r["model"], ctx)
        if w and ctx > w and (r["model"] or "").startswith("claude") and w < 1_000_000:
            w = 1_000_000
        cost = r["cost_usd"]
        if cost is None:
            cost = prices.cost_usd(r["model"], r["input_tok"], r["cached_tok"], 0, r["output_tok"])
        r.update(day=d, ctx=ctx, window=w, occupancy=(100.0 * ctx / w) if w else None, cost=cost,
                 session_id=t["session_id"] if t else None)
        out.append(r)
    return out


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * q
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


@_cached
def orientation(conn) -> dict[str, float]:
    """session_id -> seconds from session start (or first prompt) to first file-edit tool call."""
    turns = _turns(conn)
    first: dict[str, datetime] = {}
    for t in turns:
        s0 = parse_ts(t["s_started"]) or t["start"]
        if s0 and (t["session_id"] not in first or s0 < first[t["session_id"]]):
            first[t["session_id"]] = s0
    tsess = {t["id"]: t["session_id"] for t in turns}
    tmap = {t["id"]: t for t in turns}
    fe = _file_edits(conn)
    edit_at: dict[str, datetime] = {}
    for c in _tool_calls(conn):
        if c["ts"] and _is_v_edit(c, fe, tmap.get(c["turn_id"])):
            sid = tsess.get(c["turn_id"])
            if sid and (sid not in edit_at or c["ts"] < edit_at[sid]):
                edit_at[sid] = c["ts"]
    return {sid: max(0.0, (edit_at[sid] - first[sid]).total_seconds()) for sid in edit_at if sid in first}


# --- tooling -----------------------------------------------------------------------------------

TOOLING_CATEGORIES = ("skill", "command", "mcp", "plugin")


def tooling(conn, day: str | None = None) -> list[dict]:
    """event(kind=tooling) rows -> [{category, name, initiator, rule, tool_use_id, turn_id, session_id, day}]."""
    tday = {t["id"]: t["day"] for t in _turns(conn)}
    out = []
    for e in _rows(conn, "SELECT session_id, turn_id, at, data FROM event WHERE kind='tooling'"):
        try:
            d = json.loads(e["data"]) if e["data"] else {}
        except ValueError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        dd = tday.get(e["turn_id"]) or local_day(e["at"])
        if day and dd != day:
            continue
        out.append({"category": d.get("category") or "unknown", "name": d.get("name") or "?",
                    "initiator": d.get("initiator") or "unknown", "rule": d.get("rule"),
                    "tool_use_id": d.get("tool_use_id"), "turn_id": e["turn_id"], "session_id": e["session_id"],
                    "day": dd})
    return out


def _subagent_keys(evs: list[dict], scope_turns: list[dict], in_scope) -> set:
    """Distinct subagents: subagent events keyed by agent_id / agent_thread_id / tool_use_id (Codex emits
    started+completed per child), plus Codex subagent thread sessions (forked_from set) in scope."""
    keys: set = set()
    for e in evs:
        if e["kind"] != "subagent" or not in_scope(e):
            continue
        try:
            d = json.loads(e["data"]) if e["data"] else {}
        except ValueError:
            d = {}
        d = d if isinstance(d, dict) else {}
        keys.add(d.get("agent_id") or d.get("agent_thread_id") or d.get("tool_use_id") or f"event:{e['id']}")
    for t in scope_turns:
        if t.get("s_agent") == "codex" and t.get("s_forked"):
            keys.add(t["session_id"])
    return keys


# --- writer ------------------------------------------------------------------------------------

class _W:
    def __init__(self, conn, run_id):
        self.conn, self.run_id = conn, run_id

    def put(self, metric, scope_type, scope_id, value, unit=None, n=None, evidence=()):
        if value is None:
            return None
        cur = self.conn.execute(
            "INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id, value, unit, n)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (self.run_id, metric, DEFINITION_VERSION, scope_type, scope_id, float(value), unit, n))
        mid = cur.lastrowid
        for e in evidence:
            self.conn.execute("INSERT INTO metric_evidence(metric_value_id, turn_id, tool_call_id, git_change_id, note)"
                              " VALUES (?,?,?,?,?)",
                              (mid, e.get("turn_id"), e.get("tool_call_id"), e.get("git_change_id"),
                               clip(e.get("note"), 80)))
        return mid


def _share(num, den):
    return (num / den) if den else None


def _scope(day):
    return ("day", day) if day else ("all", "all")


def _emit_scope(w: _W, conn, day: str | None) -> None:
    st, sid = _scope(day)
    all_turns = [t for t in _turns(conn) if not day or t["day"] == day]
    turns = [t for t in all_turns if t["human"]]
    w.put("turns", st, sid, len(turns), "turns")
    w.put("sessions", st, sid, len({t["session_id"] for t in turns}), "sessions")

    iv = turn_intervals(conn, day)
    act, ov = union_seconds(iv), overlap_seconds(iv)
    w.put("active_s", st, sid, act, "s", len(iv))
    w.put("overlap_s", st, sid, ov, "s", len(iv))
    w.put("parallel_s", st, sid, ov, "s", len(iv))

    gl = gaps(conn, day)
    for b, c in gap_buckets(gl).items():
        w.put(f"gap_bucket:{b}", st, sid, c, "gaps", len(gl))
    nb = [g for g in gl if g.bucket != "break"]
    if nb:
        w.put("gap_median_s", st, sid, statistics.median(g.gap_s for g in nb), "s", len(nb))
        w.put("switch_share", st, sid, _share(sum(g.switched for g in nb), len(nb)), "share", len(nb))

    w.put("queued_prompts", st, sid, len(queued_prompts(conn, day)), "prompts", len(turns))

    ints = interruptions(conn, day)
    w.put("interrupts", st, sid, len(ints), "count", len(ints),
          evidence=[{"turn_id": i["turn_id"], "note": f"interrupt after {i['agent_s'] or 0:.0f}s"} for i in ints])
    w.put("thrown_s", st, sid, sum(i["agent_s"] or 0 for i in ints), "s", len(ints))

    bs = verification_batches(conn, day)
    if bs:
        ver = [b for b in bs if b.tier in ("verified_pass", "verified_fail")]
        ev = []
        for b in bs:
            note = b.tier if b.tier in ("verified_pass", "verified_fail") else \
                "unverified: " + clip(b.paths[0] if b.paths else "?", 60)
            is_ver = b.tier in ("verified_pass", "verified_fail")
            ev.append({"turn_id": b.turn_id, "tool_call_id": b.verifier_call_id if is_ver else b.last_edit_call_id,
                       "note": note if b.tier != "inspected" else "inspected; " + note})
        w.put("V", st, sid, _share(len(ver), len(bs)), "share", len(bs), evidence=ev)
        for tier in ("verified_pass", "verified_fail", "inspected", "none"):
            w.put(f"V_tier:{tier}", st, sid, sum(b.tier == tier for b in bs), "batches", len(bs))
        if ver:
            fails = [b for b in ver if b.tier == "verified_fail" or b.followed_by_edit]
            w.put("V_fail_share", st, sid, _share(len(fails), len(ver)), "share", len(ver))
            w.put("V_human", st, sid, _share(sum(b.verifier_initiator == "human" for b in ver), len(ver)),
                  "share", len(ver))
        lasts: dict[str, Batch] = {}
        for b in bs:
            if b.session_id not in lasts or b.order > lasts[b.session_id].order:
                lasts[b.session_id] = b
        w.put("V_final", st, sid, _share(sum(b.final for b in lasts.values()), len(lasts)), "share", len(lasts))

    h = hand_edits(conn, day)
    w.put("H", st, sid, h["count"], "edits(partial)" if h["partial"] else "edits", h["count"], evidence=h["evidence"])
    if not h["partial"]:
        w.put("H_repair", st, sid, h["repair"], "edits")
        w.put("H_own", st, sid, h["own"], "edits")

    bl = blocked(conn, day)
    top = sorted(bl, key=lambda x: -x["blocked_s"])[:5]
    w.put("B_min", st, sid, sum(x["blocked_s"] for x in bl) / 60.0, "min", len(bl),
          evidence=[{"turn_id": x["turn_id"], "note": f"blocked {x['blocked_s']:.0f}s"
                     + (" (attended)" if x["attended"] else "")} for x in top])

    api = api_rows(conn, day)
    occ = [r["occupancy"] for r in api if r["occupancy"] is not None]
    if occ:
        w.put("context_p50", st, sid, _pct(occ, 0.5), "pct", len(occ))
        w.put("context_p90", st, sid, _pct(occ, 0.9), "pct", len(occ))
    w.put("tokens_in", st, sid, sum(r["input_tok"] or 0 for r in api), "tokens", len(api))
    w.put("tokens_cached", st, sid, sum(r["cached_tok"] or 0 for r in api), "tokens", len(api))
    w.put("tokens_out", st, sid, sum(r["output_tok"] or 0 for r in api), "tokens", len(api))
    priced = [r["cost"] for r in api if r["cost"] is not None]
    w.put("cost_usd", st, sid, sum(priced), "usd", len(priced))
    w.put("unpriced_calls", st, sid, len(api) - len(priced), "calls", len(api))
    mix: dict[str, list] = {}
    for r in api:
        mix.setdefault(r["model"] or "unknown", []).append(r)
    for m, rs in sorted(mix.items()):
        w.put(f"model_mix:{m}", st, sid, len(rs), "calls", len(api))
        w.put(f"tokens:{m}", st, sid, sum((r["input_tok"] or 0) + (r["cached_tok"] or 0) + (r["output_tok"] or 0)
                                         for r in rs), "tokens", len(rs))
        pc = [r["cost"] for r in rs if r["cost"] is not None]
        if pc:
            w.put(f"cost_usd:{m}", st, sid, sum(pc), "usd", len(pc))

    tids = {t["id"] for t in all_turns}
    sids = {t["session_id"] for t in all_turns}
    evs = _rows(conn, "SELECT id, session_id, turn_id, at, kind, data FROM event WHERE kind IN ('subagent','compaction')")
    tday = {t["id"]: t["day"] for t in _turns(conn)}

    def in_scope(e):
        if not day:
            return True
        if e["turn_id"] is not None:
            return tday.get(e["turn_id"]) == day
        return e["session_id"] in sids and local_day(e["at"]) == day

    w.put("subagents", st, sid, len(_subagent_keys(evs, all_turns, in_scope)), "count")
    w.put("compactions", st, sid, sum(1 for e in evs if e["kind"] == "compaction" and in_scope(e)), "count")
    calls = [c for c in _tool_calls(conn) if c["turn_id"] in tids]
    w.put("tool_calls", st, sid, len(calls), "calls")
    done = [c for c in calls if c["has_result"]]
    w.put("tool_error_share", st, sid, _share(sum(1 for c in done if c["is_error"]), len(done)), "share", len(done))

    tl = tooling(conn, day)
    if tl:
        by_ci: dict[tuple, int] = {}
        by_c: dict[str, list] = {}
        for x in tl:
            by_ci[(x["category"], x["initiator"])] = by_ci.get((x["category"], x["initiator"]), 0) + 1
            by_c.setdefault(x["category"], []).append(x)
        for (cat, ini), n in sorted(by_ci.items()):
            w.put(f"tooling:{cat}:{ini}", st, sid, n, "uses", n)
        for cat, xs in sorted(by_c.items()):
            names: dict[tuple, dict] = {}
            for x in xs:
                k = (x["name"], x["initiator"])
                if k not in names:
                    names[k] = {"n": 0, "turn_id": x["turn_id"]}
                names[k]["n"] += 1
            ev = [{"turn_id": v["turn_id"], "note": f"{cat}:{clip(nm, 40)}:{ini} x{v['n']}"}
                  for (nm, ini), v in sorted(names.items(), key=lambda kv: -kv[1]["n"])]
            w.put(f"tooling:{cat}", st, sid, len(xs), "uses", len(xs), evidence=ev)
        w.put("tooling_human_share", st, sid, _share(sum(x["initiator"] == "human" for x in tl), len(tl)),
              "share", len(tl))

    ori = orientation(conn)
    vals = [v for s, v in ori.items() if s in sids]
    if vals:
        w.put("orientation_s", st, sid, statistics.median(vals), "s", len(vals))


def compute_all(conn: sqlite3.Connection, run_id: int) -> None:
    _CACHE.clear()
    _CACHE["on"] = True
    try:
        _compute_all(conn, run_id)
    finally:
        _CACHE.clear()


def _compute_all(conn: sqlite3.Connection, run_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM metric_evidence WHERE metric_value_id IN"
                     " (SELECT id FROM metric_value WHERE run_id = ?)", (run_id,))
        conn.execute("DELETE FROM metric_value WHERE run_id = ?", (run_id,))
        w = _W(conn, run_id)
        for d in days(conn):
            _emit_scope(w, conn, d)
        _emit_scope(w, conn, None)
        for s, v in orientation(conn).items():
            w.put("orientation_s", "session", s, v, "s", 1)
        per_sess: dict[str, list] = {}
        for b in verification_batches(conn):
            per_sess.setdefault(b.session_id, []).append(b)
        for s, bs in per_sess.items():
            ver = sum(b.tier in ("verified_pass", "verified_fail") for b in bs)
            w.put("V", "session", s, ver / len(bs), "share", len(bs))
        for t_sid, lst in _session_unions(turn_intervals(conn)).items():
            w.put("active_s", "session", t_sid, sum((e - s).total_seconds() for s, e in lst), "s", len(lst))
