"""SQLite schema (spec §4, verbatim) and connection helper."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .model import db_path

DDL = """
CREATE TABLE IF NOT EXISTS ingest_run(id INTEGER PRIMARY KEY, at TEXT, parser_version TEXT);
CREATE TABLE IF NOT EXISTS session(id TEXT PRIMARY KEY, agent TEXT, cwd TEXT, repo TEXT, branch TEXT,
  transcript_path TEXT, forked_from TEXT, started_at TEXT, ended_at TEXT, ended_by TEXT);
CREATE TABLE IF NOT EXISTS turn(id INTEGER PRIMARY KEY, session_id TEXT REFERENCES session, idx INT,
  prompt_at TEXT, prompt_text TEXT, prompt_kind TEXT, initiator TEXT, agent_started_at TEXT,
  agent_finished_at TEXT, end_source TEXT, first_uuid TEXT, last_uuid TEXT, UNIQUE(session_id, idx));
CREATE TABLE IF NOT EXISTS api_call(id INTEGER PRIMARY KEY, turn_id INT REFERENCES turn, request_id TEXT UNIQUE,
  model TEXT, effort TEXT, input_tok INT, cached_tok INT, output_tok INT, context_window INT,
  cost_usd REAL, duration_ms INT, at TEXT);
CREATE TABLE IF NOT EXISTS tool_call(id INTEGER PRIMARY KEY, turn_id INT REFERENCES turn, tool_use_id TEXT UNIQUE,
  name TEXT, kind TEXT, bash_intent TEXT, initiator TEXT, initiator_rule TEXT, args_summary TEXT,
  started_at TEXT, duration_ms INT, is_error INT, has_result INT, sidechain_id TEXT);
CREATE TABLE IF NOT EXISTS file_edit(id INTEGER PRIMARY KEY, tool_call_id INT REFERENCES tool_call, path TEXT,
  manifest_dir TEXT, lines_added INT, lines_removed INT, user_modified INT);
CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY, session_id TEXT REFERENCES session, turn_id INT,
  at TEXT, kind TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS git_change(id INTEGER PRIMARY KEY, repo TEXT, sha TEXT, at TEXT, turn_id INT,
  files TEXT, lines_added INT, lines_removed INT, author TEXT, UNIQUE(repo, sha));
CREATE TABLE IF NOT EXISTS metric_value(id INTEGER PRIMARY KEY, run_id INT REFERENCES ingest_run, metric TEXT,
  definition_version TEXT, scope_type TEXT, scope_id TEXT, value REAL, unit TEXT, n INT);
CREATE TABLE IF NOT EXISTS metric_evidence(metric_value_id INT REFERENCES metric_value,
  turn_id INT REFERENCES turn, tool_call_id INT, git_change_id INT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_turn_session ON turn(session_id);
CREATE INDEX IF NOT EXISTS ix_tool_turn ON tool_call(turn_id);
CREATE INDEX IF NOT EXISTS ix_api_turn ON api_call(turn_id);
CREATE INDEX IF NOT EXISTS ix_event_session ON event(session_id);
CREATE INDEX IF NOT EXISTS ix_mv_run ON metric_value(run_id, metric);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    return conn


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    r = conn.execute("SELECT max(id) FROM ingest_run").fetchone()
    return r[0] if r else None
