"""Discovery + idempotent write of parser Bundles into SQLite.

Parser contract:  parse(path: str, hook_events: list[dict] | None = None) -> Bundle | None
  hook_events = lines from events.jsonl whose payload session_id matches (used
  for Stop-hook turn ends and git snapshots). Return None to skip a file.

Idempotency: per session, delete all child rows then reinsert. api_call.request_id
and tool_call.tool_use_id are UNIQUE and forks copy ids -> INSERT OR IGNORE.
Metrics must be recomputed in the same run (turn ids change on reinsert).
"""
from __future__ import annotations

import fnmatch
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
            " context_window, cost_usd, duration_ms, at, sidechain_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid.get(a.turn_idx), a.request_id, a.model, a.effort, a.input_tok + a.cache_write_tok, a.cached_tok,
             a.output_tok, a.context_window, a.cost_usd, a.duration_ms, a.at, a.sidechain_id))
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


# Lab / scratch / temp sessions must not pollute the human's real day (`scorer ingest`
# excludes them by default; --include-lab opts back in; config.toml `exclude_cwd = [...]`
# replaces this list). Pattern forms:
#   contains "/"  -> path glob on the cwd (~ and $VARS expanded, symlinks resolved, so
#                    /tmp == /private/tmp); a trailing "/**" also matches the dir itself.
#                    A pattern with an unset $VAR is ignored.
#   no "/"        -> glob on the cwd's basename.
# The judge workdir (judge.judge_cwd) is always excluded on top of the list.
DEFAULT_EXCLUDE_CWD: tuple[str, ...] = (
    "/tmp/**",
    "/private/tmp/**",
    "/private/var/folders/**",
    "$TMPDIR/**",
    "~/scorer-lab/**",
    "~/.scorer-lab/**",
    "~/.scorer-*/**",
    "scorer-capture-repo.*",
    "scorer-e2e.*",
    "scorer-judge*",
    "lab-*",
)

_GLOB = set("*?[")


def _judge_dirs() -> list[str]:
    from .model import scorer_home
    h = scorer_home()
    return [str(h / "judge") + "/**", str(h / "scorer-judge") + "/**"]


def default_exclude_patterns() -> list[str]:
    """config.toml `exclude_cwd` if set, else DEFAULT_EXCLUDE_CWD; plus the judge workdir."""
    from .config import load
    cfg = load().exclude_cwd
    return list(cfg if cfg is not None else DEFAULT_EXCLUDE_CWD) + _judge_dirs()


def _resolve_pattern(pat: str) -> list[str] | None:
    """Path pattern -> [expanded, symlink-resolved] variants; None if a $VAR is unset."""
    e = os.path.expanduser(os.path.expandvars(pat))
    if "$" in e:
        return None
    e = os.path.normpath(e)
    parts = e.split(os.sep)
    i = next((k for k, c in enumerate(parts) if _GLOB & set(c)), len(parts))
    head, tail = os.sep.join(parts[:i]) or os.sep, parts[i:]
    real = os.path.join(os.path.realpath(head), *tail) if tail else os.path.realpath(head)
    return list(dict.fromkeys([e, real]))


def cwd_excluded(cwd: str | None, patterns: list[str]) -> bool:
    """True if cwd matches any exclusion pattern (see DEFAULT_EXCLUDE_CWD)."""
    if not cwd or not patterns:
        return False
    raw = os.path.normpath(os.path.expanduser(cwd))
    cands = list(dict.fromkeys([raw, _real(cwd)]))
    for pat in patterns:
        if "/" not in pat:
            if any(fnmatch.fnmatchcase(os.path.basename(c), pat) for c in cands):
                return True
            continue
        for p in _resolve_pattern(pat) or []:
            if p.endswith("/**"):
                base = p[:-3].rstrip(os.sep) or os.sep
                if any(fnmatch.fnmatchcase(c, base) or fnmatch.fnmatchcase(c, base.rstrip(os.sep) + "/*")
                       for c in cands):
                    return True
            elif any(fnmatch.fnmatchcase(c, p) for c in cands):
                return True
    return False


def _is_excluded(cwd: str | None, exclude: list[str], prefixes: list[str]) -> bool:
    """Excluded unless explicitly included by a --cwd-prefix / SCORER_CWD_PREFIX (lab mode)."""
    if not exclude or not cwd_excluded(cwd, exclude):
        return False
    return not (prefixes and cwd_matches(cwd, prefixes))


def purge_excluded(conn: sqlite3.Connection, exclude: list[str], prefixes: list[str]) -> int:
    """Delete stored sessions (and children) that the exclusion now covers. judge_label rows stay.
    With a --cwd-prefix (lab mode) this run only looked at sessions under the prefix: sessions outside it
    (earlier lab runs in a lab DB) are none of its business and are never purged."""
    ids = [r[0] for r in conn.execute("SELECT id, cwd FROM session").fetchall()
           if _is_excluded(r[1], exclude, prefixes) and not (prefixes and not cwd_matches(r[1], prefixes))]
    with conn:
        for sid in ids:
            delete_session(conn, sid)
    return len(ids)


def run_ingest(conn: sqlite3.Connection, *, since_days: int, claude_root: str, codex_root: str,
               events_file: str | None, cwd_prefixes: list[str] | None = None,
               exclude_cwd: list[str] | None = None) -> int:
    """cwd_prefixes=None -> env SCORER_CWD_PREFIX; empty -> no filter.
    exclude_cwd: cwd patterns to drop (and purge from the DB) unless under an explicit prefix;
    None/empty -> no exclusion. The CLI passes default_exclude_patterns() unless --include-lab."""
    prefixes = list(cwd_prefixes) if cwd_prefixes else cwd_prefixes_from_env()
    exclude = list(exclude_cwd or [])
    hooks = load_hook_events(events_file)
    run_id = conn.execute("INSERT INTO ingest_run(at, parser_version) VALUES (?,?)",
                          (datetime.now(timezone.utc).isoformat(), PARSER_VERSION)).lastrowid
    jobs: list[tuple[str, str]] = [("claude", p) for p in discover_claude(claude_root, since_days)]
    jobs += [("codex", p) for p in discover_codex(codex_root, since_days)]
    ok = fail = skipped = excluded = 0
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
            if _is_excluded(b.session.cwd, exclude, prefixes):
                excluded += 1
                continue
            with conn:
                write_bundle(conn, b)
            ok += 1
        except Exception as e:  # one bad file must not kill the run
            fail += 1
            log(f"ingest: skip {os.path.basename(path)}: {type(e).__name__}: {str(e)[:80]}")
    conn.commit()
    if exclude:
        purged = purge_excluded(conn, exclude, prefixes)
        log(f"ingest: excluded {excluded} lab/scratch session(s), purged {purged} from the DB")
    if prefixes:
        log(f"ingest: cwd filter kept {ok}, skipped {skipped} session(s) outside "
            f"{len(prefixes)} prefix(es)")
    if fail:
        log(f"ingest: {ok} ok, {fail} failed")
    return run_id
