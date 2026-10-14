"""check.run_checks on a DB built from goldens/fixtures, and on deliberately broken rows."""
from __future__ import annotations

import glob
import os

from scorer.check import run_checks
from scorer.db import connect
from scorer.ingest import write_bundle
from scorer.parse_claude import parse

ROOT = os.path.dirname(os.path.dirname(__file__))


def _db(tmp_path):
    conn = connect(tmp_path / "t.db")
    for p in sorted(glob.glob(os.path.join(ROOT, "tests", "golden", "g*.jsonl"))):
        b = parse(p)
        if b:
            write_bundle(conn, b)
    conn.commit()
    return conn


def test_clean_db_passes(tmp_path):
    conn = _db(tmp_path)
    assert run_checks(conn, warn=None) == []


def test_negative_gap_detected(tmp_path):
    conn = _db(tmp_path)
    sid = "11111111-0000-0000-0000-000000000001"
    conn.execute("UPDATE turn SET agent_finished_at='2026-10-01T09:00:00+00:00' WHERE session_id=? AND idx=0", (sid,))
    fails = run_checks(conn, warn=None)
    assert any(f.startswith("negative gaps: 1") for f in fails)


def test_missing_result_detected(tmp_path):
    conn = _db(tmp_path)
    sid = "22222222-0000-0000-0000-000000000002"
    # g2 has one turn -> make it non-last by adding a later turn, then drop the result flag
    conn.execute("INSERT INTO turn(session_id, idx, prompt_at, agent_finished_at) VALUES (?,?,?,?)",
                 (sid, 1, "2026-10-01T09:00:00+00:00", "2026-10-01T09:00:05+00:00"))
    conn.execute("UPDATE tool_call SET has_result=0 WHERE tool_use_id='toolu_g2'")
    fails = run_checks(conn, warn=None)
    assert any("without result: 1" in f for f in fails)


def test_api_twins_detected(tmp_path):
    conn = _db(tmp_path)
    r = conn.execute("SELECT * FROM api_call LIMIT 1").fetchone()
    conn.execute("INSERT INTO api_call(turn_id, request_id, model, input_tok, cached_tok, output_tok, at)"
                  " VALUES (?,?,?,?,?,?,?)", (r["turn_id"], "dup-x", r["model"], r["input_tok"], r["cached_tok"],
                                               r["output_tok"], r["at"]))
    fails = run_checks(conn, warn=None)
    assert any("usage not deduped" in f for f in fails)


def test_long_turn_without_agent_entries_detected(tmp_path):
    conn = _db(tmp_path)
    sid = "44444444-0000-0000-0000-000000000004"
    conn.execute("UPDATE turn SET agent_finished_at='2026-10-01T20:00:00+00:00' WHERE session_id=? AND idx=1", (sid,))
    fails = run_checks(conn, warn=None)
    assert any(f.startswith("turns > 6 h without agent entries spanning them: 1") for f in fails)
