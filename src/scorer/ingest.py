"""Discovery + idempotent write of parser Bundles into SQLite.

Parser contract:  parse(path: str, hook_events: list[dict] | None = None) -> Bundle | None
  hook_events = lines from events.jsonl whose payload session_id matches (used
  for Stop-hook turn ends and git snapshots). Return None to skip a file.

Idempotency: per session, delete all child rows then reinsert. api_call.request_id
and tool_call.tool_use_id are UNIQUE and forks copy ids -> INSERT OR IGNORE.
Metrics must be recomputed in the same run (turn ids change on reinsert).
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .model import PARSER_VERSION, Bundle

log = print


def load_hook_events(events_file: str | None) -> dict[str, list[dict]]:
    """events.jsonl -> {session_id: [line, ...]}. Lines: {at, agent, event, payload, git?}."""
    out: dict[str, list[dict]] = defaultdict(list)
    if not events_file or not os.path.exists(events_file):
        return out
    with open(events_file, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            p = e.get("payload") or {}
            sid = p.get("session_id") or p.get("sessionId") or p.get("thread_id")
            if sid:
                out[sid].append(e)
    return out


def _session_id_from_path(path: str) -> str:
    b = os.path.splitext(os.path.basename(path))[0]
    return b[-36:] if len(b) >= 36 else b


# The judge's `claude -p` fallback runs with cwd = SCORER_HOME/judge; its project dir
# slug contains this marker and must never be ingested as the human's own usage.
JUDGE_SLUG_MARKER = "-scorer-judge"


def discover_claude(root: str, since_days: int) -> list[str]:
    cutoff = time.time() - since_days * 86400
    return sorted(p for p in glob.glob(os.path.join(root, "*", "*.jsonl"))
                  if os.path.getmtime(p) >= cutoff and JUDGE_SLUG_MARKER not in os.path.basename(os.path.dirname(p)))


def discover_codex(root: str, since_days: int) -> list[str]:
    # Spec: only the current format under 2026/10 (older files are v0.98 format).
    cutoff = time.time() - since_days * 86400
    return sorted(p for p in glob.glob(os.path.join(root, "2026", "10", "**", "*.jsonl"), recursive=True)
                  if os.path.getmtime(p) >= cutoff)


def delete_session(conn: sqlite3.Connection, sid: str) -> None:
    turn_ids = "SELECT id FROM turn WHERE session_id=?"
    conn.execute(f"DELETE FROM file_edit WHERE tool_call_id IN (SELECT id FROM tool_call WHERE turn_id IN ({turn_ids}))", (sid,))
    conn.execute(f"DELETE FROM tool_call WHERE turn_id IN ({turn_ids})", (sid,))
    conn.execute(f"DELETE FROM api_call WHERE turn_id IN ({turn_ids})", (sid,))
    conn.execute("DELETE FROM event WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM turn WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM session WHERE id=?", (sid,))


def write_bundle(conn: sqlite3.Connection, b: Bundle) -> None:
    s = b.session
    delete_session(conn, s.id)
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (s.id, s.agent, s.cwd, s.repo, s.branch, s.transcript_path, s.forked_from,
                  s.started_at, s.ended_at, s.ended_by))
    tid: dict[int, int] = {}
    for t in b.turns:
        cur = conn.execute(
            "INSERT INTO turn(session_id, idx, prompt_at, prompt_text, prompt_kind, initiator, agent_started_at,"
            " agent_finished_at, end_source, first_uuid, last_uuid) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (s.id, t.idx, t.prompt_at, t.prompt_text, t.prompt_kind, t.initiator, t.agent_started_at,
             t.agent_finished_at, t.end_source, t.first_uuid, t.last_uuid))
        tid[t.idx] = cur.lastrowid
        if t.interrupted:
            conn.execute("INSERT INTO event(session_id, turn_id, at, kind, data) VALUES (?,?,?,?,?)",
                         (s.id, cur.lastrowid, t.agent_finished_at or t.prompt_at, "interrupt", "{}"))
        if t.enqueued_at:
            conn.execute("INSERT INTO event(session_id, turn_id, at, kind, data) VALUES (?,?,?,?,?)",
                         (s.id, cur.lastrowid, t.enqueued_at, "queue_enqueue", "{}"))
    for a in b.api_calls:
        conn.execute(
            "INSERT OR IGNORE INTO api_call(turn_id, request_id, model, effort, input_tok, cached_tok, output_tok,"
            " context_window, cost_usd, duration_ms, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid.get(a.turn_idx), a.request_id, a.model, a.effort, a.input_tok + a.cache_write_tok, a.cached_tok,
             a.output_tok, a.context_window, a.cost_usd, a.duration_ms, a.at))
    tcid: dict[str, int] = {}
    for c in b.tool_calls:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tool_call(turn_id, tool_use_id, name, kind, bash_intent, initiator, initiator_rule,"
            " args_summary, started_at, duration_ms, is_error, has_result, sidechain_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid.get(c.turn_idx), c.tool_use_id, c.name, c.kind, c.bash_intent, c.initiator, c.initiator_rule,
             c.args_summary, c.started_at, c.duration_ms, int(c.is_error), int(c.has_result), c.sidechain_id))
        if cur.rowcount:
            tcid[c.tool_use_id] = cur.lastrowid
    for fe in b.file_edits:
        if fe.tool_use_id in tcid:
            conn.execute("INSERT INTO file_edit(tool_call_id, path, manifest_dir, lines_added, lines_removed,"
                         " user_modified) VALUES (?,?,?,?,?,?)",
                         (tcid[fe.tool_use_id], fe.path, fe.manifest_dir, fe.lines_added, fe.lines_removed,
                          int(fe.user_modified)))
    for e in b.events:
        conn.execute("INSERT INTO event(session_id, turn_id, at, kind, data) VALUES (?,?,?,?,?)",
                     (s.id, tid.get(e.turn_idx) if e.turn_idx is not None else None, e.at, e.kind,
                      json.dumps(e.data, default=str)))


def cwd_prefixes_from_env() -> list[str]:
    """SCORER_CWD_PREFIX = os.pathsep-separated dirs (default for --cwd-prefix)."""
    return [p for p in os.environ.get("SCORER_CWD_PREFIX", "").split(os.pathsep) if p.strip()]


def _real(p: str) -> str:
    return os.path.realpath(os.path.expanduser(p))


def cwd_matches(cwd: str | None, prefixes: list[str]) -> bool:
    """True if cwd is one of the prefixes or below it (symlinks resolved: /tmp == /private/tmp)."""
    if not prefixes:
        return True
    if not cwd:
        return False
    c = _real(cwd)
    for p in prefixes:
        r = _real(p)
        if c == r or c.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def run_ingest(conn: sqlite3.Connection, *, since_days: int, claude_root: str, codex_root: str,
               events_file: str | None, cwd_prefixes: list[str] | None = None) -> int:
    """cwd_prefixes=None -> env SCORER_CWD_PREFIX; empty -> no filter."""
    prefixes = list(cwd_prefixes) if cwd_prefixes else cwd_prefixes_from_env()
    hooks = load_hook_events(events_file)
    run_id = conn.execute("INSERT INTO ingest_run(at, parser_version) VALUES (?,?)",
                          (datetime.now(timezone.utc).isoformat(), PARSER_VERSION)).lastrowid
    jobs: list[tuple[str, str]] = [("claude", p) for p in discover_claude(claude_root, since_days)]
    jobs += [("codex", p) for p in discover_codex(codex_root, since_days)]
    ok = fail = skipped = 0
    for agent, path in jobs:
        try:
            if agent == "claude":
                from .parse_claude import parse
            else:
                try:
                    from .parse_codex import parse
                except ImportError:
                    continue
            b = parse(path, hooks.get(_session_id_from_path(path)))
            if b is None:
                continue
            if prefixes and not cwd_matches(b.session.cwd, prefixes):
                skipped += 1
                continue
            with conn:
                write_bundle(conn, b)
            ok += 1
        except Exception as e:  # one bad file must not kill the run
            fail += 1
            log(f"ingest: skip {os.path.basename(path)}: {type(e).__name__}: {str(e)[:80]}")
    conn.commit()
    if prefixes:
        log(f"ingest: cwd filter kept {ok}, skipped {skipped} session(s) outside "
            f"{len(prefixes)} prefix(es)")
    if fail:
        log(f"ingest: {ok} ok, {fail} failed")
    return run_id
