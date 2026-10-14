"""Dashboard cache for LLM output (narrative, summaries) in the scorer DB itself.

    ui_cache(key TEXT PRIMARY KEY, value JSON, at TEXT)     # created on first use (extends db.DDL)

    make_key(kind, *parts) -> str       # e.g. make_key("narrative", day, run_id)
    get(conn, key) -> dict | None       # {"value": <decoded JSON>, "at": iso}
    put(conn, key, value) -> str        # upsert + commit; returns the ISO UTC timestamp
    lock(key) -> threading.Lock         # per-key, process-wide: one generation per key at a time

Callers never hold a write transaction while waiting on an API call: compute first, then put().
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

DDL = "CREATE TABLE IF NOT EXISTS ui_cache(key TEXT PRIMARY KEY, value JSON, at TEXT)"

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def make_key(kind: str, *parts) -> str:
    return json.dumps([kind, *[None if p is None else str(p) for p in parts]], separators=(",", ":"))


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)


def get(conn: sqlite3.Connection, key: str) -> dict | None:
    _ensure(conn)
    r = conn.execute("SELECT value, at FROM ui_cache WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    try:
        return {"value": json.loads(r[0]), "at": r[1]}
    except (TypeError, ValueError):
        return None


def put(conn: sqlite3.Connection, key: str, value) -> str:
    _ensure(conn)
    at = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT OR REPLACE INTO ui_cache(key, value, at) VALUES (?,?,?)",
                 (key, json.dumps(value, default=str), at))
    conn.commit()
    return at


def lock(key: str) -> threading.Lock:
    with _guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk
