"""Replay invariants on an ingested DB (spec §6e). Returns failure lines; empty = ok.

Soft checks print a WARN line (returned only when they cross the hard threshold).
Never surfaces prompt text: only counts and turn ids.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from .model import parse_ts

TOL = timedelta(seconds=1)


def _ex(ids, n=5) -> str:
    return ",".join(str(i) for i in list(ids)[:n])


def _negative_gaps(conn) -> str | None:
    bad = []
    prev = None
    for r in conn.execute("SELECT id, session_id, prompt_at, agent_finished_at FROM turn ORDER BY session_id, idx"):
        if prev is not None and prev[1] == r[1]:
            end, nxt = parse_ts(prev[3]), parse_ts(r[2])
            if end and nxt and end - nxt > TOL:
                bad.append(prev[0])
        prev = tuple(r)
    return f"negative gaps: {len(bad)} turns (e.g. turn {_ex(bad)})" if bad else None


def _usage_dedup(conn) -> str | None:
    dup = conn.execute("SELECT count(*) FROM (SELECT request_id FROM api_call GROUP BY request_id HAVING count(*)>1)"
                       ).fetchone()[0]
    twins = conn.execute(
        "SELECT turn_id FROM api_call GROUP BY turn_id, model, input_tok, cached_tok, output_tok, at"
        " HAVING count(*)>1").fetchall()
    out = []
    if dup:
        out.append(f"{dup} duplicate request_id")
    if twins:
        out.append(f"{len(twins)} identical api_call twins (e.g. turn {_ex(r[0] for r in twins)})")
    return "usage not deduped: " + "; ".join(out) if out else None


def _tool_results(conn) -> str | None:
    rows = conn.execute("""
        SELECT tc.id, t.id FROM tool_call tc JOIN turn t ON t.id = tc.turn_id
        WHERE tc.has_result = 0
          AND NOT EXISTS (SELECT 1 FROM event e WHERE e.turn_id = t.id AND e.kind = 'interrupt')
          AND t.idx < (SELECT max(idx) FROM turn t2 WHERE t2.session_id = t.session_id)
    """).fetchall()
    if not rows:
        return None
    turns = sorted({r[1] for r in rows})
    return f"tool calls without result: {len(rows)} in {len(turns)} turns (e.g. turn {_ex(turns)})"


def _long_turns(conn) -> str | None:
    """No turn > 6 h unless its own agent entries reach (within 15 min) its end."""
    bad = []
    for r in conn.execute("""
        SELECT t.id, t.prompt_at, t.agent_finished_at,
          max(coalesce((SELECT max(at) FROM api_call a WHERE a.turn_id = t.id), ''),
              coalesce((SELECT max(started_at) FROM tool_call c WHERE c.turn_id = t.id), '')) AS last_agent
        FROM turn t WHERE (julianday(t.agent_finished_at) - julianday(t.prompt_at)) * 24 > 6"""):
        end, last = parse_ts(r[2]), parse_ts(r[3] or None)
        if last is None or end - last > timedelta(minutes=15):
            bad.append(r[0])
    return f"turns > 6 h without agent entries spanning them: {len(bad)} (e.g. turn {_ex(bad)})" if bad else None


def _turn_duration_ratio(conn) -> tuple[str | None, str | None]:
    """Turns with agent work vs turns ended by turn_duration. Warn >10%, fail >25%."""
    active = conn.execute("SELECT count(*) FROM turn t JOIN session s ON s.id=t.session_id"
                          " WHERE s.agent='claude' AND t.agent_started_at IS NOT NULL").fetchone()[0]
    td = conn.execute("SELECT count(*) FROM turn t JOIN session s ON s.id=t.session_id"
                      " WHERE s.agent='claude' AND t.end_source IN ('turn_duration','stop_hook')").fetchone()[0]
    if not active:
        return None, None
    dev = abs(active - td) / active
    msg = f"claude turns with agent work={active} vs ended by turn_duration/Stop={td} ({dev:.0%} apart)"
    if dev > 0.25:
        return None, msg
    if dev > 0.10:
        return "WARN " + msg, None
    return None, None


def run_checks(conn: sqlite3.Connection, warn=print) -> list[str]:
    fails = []
    # overlaps within a session == negative gaps; overlaps across sessions are allowed (parallel work)
    for fn in (_negative_gaps, _usage_dedup, _tool_results, _long_turns):
        r = fn(conn)
        if r:
            fails.append(r)
    w, f = _turn_duration_ratio(conn)
    if w and warn:
        warn(w)
    if f:
        fails.append(f)
    return fails
