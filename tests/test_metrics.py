from datetime import datetime, timedelta, timezone

import pytest

from scorer import classify, db, ingest, metrics
from scorer.model import (ApiCall, Bundle, Event, FileEdit, Session, ToolCall, Turn, local_day)

T0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)   # noon UTC: same local day almost everywhere
DAY = local_day(T0)


def ts(sec: float) -> str:
    return (T0 + timedelta(seconds=sec)).isoformat()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SCORER_HOME", str(tmp_path / "home"))
    c = db.connect(tmp_path / "x.db")
    yield c
    c.close()


def run(conn) -> int:
    rid = conn.execute("INSERT INTO ingest_run(at, parser_version) VALUES ('now','1')").lastrowid
    conn.commit()
    metrics.compute_all(conn, rid)
    return rid


def mv(conn, rid, metric, scope_type="day", scope_id=DAY):
    r = conn.execute("SELECT value, unit, n FROM metric_value WHERE run_id=? AND metric=? AND scope_type=?"
                     " AND scope_id=?", (rid, metric, scope_type, scope_id)).fetchone()
    return r


def val(conn, rid, metric, **kw):
    r = mv(conn, rid, metric, **kw)
    return r["value"] if r else None


def write(conn, *bundles):
    with conn:
        for b in bundles:
            ingest.write_bundle(conn, b)


def turn(idx, start, end, text="please do it now ok", **kw):
    return Turn(idx=idx, prompt_at=ts(start), prompt_text=text, agent_finished_at=ts(end) if end is not None else None,
                **kw)


# --- golden 1: gaps 10/90/200 s -----------------------------------------------------------------

def test_golden1_gap_buckets(conn):
    turns, t = [], 0.0
    ends = []
    for i, gap in enumerate([None, 10, 90, 200]):
        start = t if gap is None else ends[-1] + gap
        end = start + 60
        turns.append(turn(i, start, end))
        ends.append(end)
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=turns))
    rid = run(conn)
    assert val(conn, rid, "gap_bucket:lt30s") == 1
    assert val(conn, rid, "gap_bucket:30s-2m") == 1
    assert val(conn, rid, "gap_bucket:2-5m") == 1
    assert val(conn, rid, "gap_bucket:5-15m") == 0
    assert val(conn, rid, "gap_bucket:break") == 0
    assert val(conn, rid, "gap_median_s") == 90
    assert val(conn, rid, "switch_share") == 0
    assert val(conn, rid, "turns") == 4


def test_queued_prompt_gap_from_enqueue(conn):
    turns = [turn(0, 0, 60), turn(1, 60, 120, prompt_kind="queued", enqueued_at=ts(30))]
    write(conn, Bundle(Session("s1", "claude"), turns=turns))
    g = metrics.gaps(conn, DAY)
    assert [x.gap_s for x in g] == [0.0]


# --- golden 5: interrupt -> thrown 45 s ------------------------------------------------------------

def test_golden5_interrupt(conn):
    write(conn, Bundle(Session("s1", "claude"), turns=[turn(0, 0, 45, interrupted=True, end_source="interrupt")]))
    rid = run(conn)
    assert val(conn, rid, "interrupts") == 1
    assert val(conn, rid, "thrown_s") == 45


# --- golden 6: Edit then pytest -> verified; Edit then ls -> not -------------------------------------

def _g6(tmp_path, *, error=False, has_result=True, initiator="agent"):
    (tmp_path / "pyproject.toml").write_text("")
    classify.manifest_dir.cache_clear()
    classify._walk.cache_clear()
    classify._has_manifest.cache_clear()
    f = str(tmp_path / "a.py")
    calls = [
        ToolCall(0, "e1", "Edit", kind="edit", args_summary=f, started_at=ts(1), has_result=True),
        ToolCall(0, "r1", "Read", kind="read", args_summary=f, started_at=ts(2), has_result=True),
        ToolCall(0, "b1", "Bash", kind="bash", bash_intent="test", args_summary="uv run pytest -q", started_at=ts(3),
                 has_result=has_result, is_error=error, initiator=initiator),
        ToolCall(1, "e2", "Edit", kind="edit", args_summary=f, started_at=ts(101), has_result=True),
        ToolCall(1, "b2", "Bash", kind="bash", bash_intent="read", args_summary="ls", started_at=ts(102),
                 has_result=True),
    ]
    edits = [FileEdit("e1", f), FileEdit("e2", f)]
    return Bundle(Session("s1", "claude", cwd=str(tmp_path)), turns=[turn(0, 0, 50), turn(1, 100, 150)],
                  tool_calls=calls, file_edits=edits)


def test_golden6_verification(conn, tmp_path):
    write(conn, _g6(tmp_path, initiator="human"))
    rid = run(conn)
    r = mv(conn, rid, "V")
    assert r["value"] == 0.5 and r["n"] == 2
    bs = metrics.verification_batches(conn, DAY)
    assert [b.tier for b in bs] == ["verified_pass", "none"]
    assert val(conn, rid, "V_final") == 0.0          # last batch (turn 1) unverified
    assert val(conn, rid, "V_human") == 1.0
    assert val(conn, rid, "V_fail_share") == 0.0
    notes = [r[0] for r in conn.execute(
        "SELECT note FROM metric_evidence e JOIN metric_value v ON v.id = e.metric_value_id"
        " WHERE v.metric='V' AND v.scope_type='day'")]
    assert "verified_pass" in notes and any(n.startswith("unverified: ") for n in notes)


def test_golden6_fail_and_no_result(conn, tmp_path):
    write(conn, _g6(tmp_path, error=True))
    rid = run(conn)
    assert val(conn, rid, "V_fail_share") == 1.0
    assert val(conn, rid, "V_tier:verified_fail") == 1


def test_golden6_backgrounded_without_result_not_verified(conn, tmp_path):
    write(conn, _g6(tmp_path, has_result=False))
    bs = metrics.verification_batches(conn, DAY)
    assert bs[0].tier == "inspected"                  # Read of the edited file, but no test result


def test_bash_edit_batch_and_irrelevant_test(conn, tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "pyproject.toml").write_text("")
    classify.manifest_dir.cache_clear()
    classify._walk.cache_clear()
    classify._has_manifest.cache_clear()
    calls = [
        ToolCall(0, "e1", "Bash", kind="bash", bash_intent="edit", args_summary="echo x > web/a.ts",
                 started_at=ts(1), has_result=True),
        ToolCall(0, "b1", "Bash", kind="bash", bash_intent="test", args_summary="cd api && pytest",
                 started_at=ts(2), has_result=True),
    ]
    write(conn, Bundle(Session("s1", "claude", cwd=str(tmp_path)), turns=[turn(0, 0, 10)], tool_calls=calls))
    bs = metrics.verification_batches(conn, DAY)
    assert len(bs) == 1 and bs[0].tier == "none"


# --- golden 7: userModified -> hand edit ---------------------------------------------------------

def test_golden7_hand_edit(conn):
    calls = [ToolCall(0, "e1", "Edit", kind="edit", args_summary="/r/a.py", started_at=ts(1), has_result=True)]
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=[turn(0, 0, 10)], tool_calls=calls,
                       file_edits=[FileEdit("e1", "/r/a.py", user_modified=True)]))
    rid = run(conn)
    r = mv(conn, rid, "H")
    assert r["value"] == 1 and r["unit"] == "edits(partial)"


def test_hand_edit_git_snapshot(conn):
    calls = [ToolCall(0, "e1", "Edit", kind="edit", args_summary="/r/a.py", started_at=ts(1), has_result=True),
             ToolCall(1, "e2", "Edit", kind="edit", args_summary="/r/c.py", started_at=ts(101), has_result=True)]
    events = [Event("git_snapshot", ts(150), turn_idx=1, data={"git": {"changed": ["a.py", "b.py", "c.py"]}})]
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=[turn(0, 0, 10), turn(1, 100, 150)],
                       tool_calls=calls, file_edits=[FileEdit("e1", "/r/a.py"), FileEdit("e2", "/r/c.py")],
                       events=events))
    rid = run(conn)
    assert mv(conn, rid, "H")["unit"] == "edits"
    assert val(conn, rid, "H_repair") == 1   # a.py: agent-edited earlier in the session
    assert val(conn, rid, "H_own") == 1      # b.py: never agent-edited


# --- golden 8: two sessions overlapping 120 s -> overlap 120; B ----------------------------------

def test_golden8_overlap_and_blocked(conn):
    s1 = Bundle(Session("s1", "claude", cwd="/a"), turns=[turn(0, 0, 600), turn(1, 1200, 1260)])
    s2 = Bundle(Session("s2", "codex", cwd="/b"), turns=[turn(0, 480, 900)])
    write(conn, s1, s2)
    rid = run(conn)
    assert val(conn, rid, "overlap_s") == 120
    assert val(conn, rid, "parallel_s") == 120
    assert val(conn, rid, "active_s") == 900 + 60      # union, not sum (600+420+60)
    assert val(conn, rid, "sessions") == 2
    # s1 waits 600..1200 while s2 is active until 900 -> 300 s blocked
    assert val(conn, rid, "B_min") == pytest.approx(5.0)
    assert val(conn, rid, "active_s", scope_type="all", scope_id="all") == 960


def test_attended_gap(conn):
    write(conn, Bundle(Session("s1", "claude"), turns=[turn(0, 0, 60), turn(1, 90, 120, text="yes go")]))
    bl = metrics.blocked(conn, DAY)
    assert len(bl) == 1 and bl[0]["attended"] and bl[0]["blocked_s"] == 30


def test_switch_share(conn):
    s1 = Bundle(Session("s1", "claude"), turns=[turn(0, 0, 60), turn(1, 200, 260)])
    s2 = Bundle(Session("s2", "claude"), turns=[turn(0, 100, 150)])
    write(conn, s1, s2)
    rid = run(conn)
    assert val(conn, rid, "switch_share") == 1.0


# --- diagnostics -----------------------------------------------------------------------------------

def test_diagnostics(conn):
    api = [ApiCall(0, "r1", model="claude-opus-5-5", input_tok=100_000, cached_tok=100_000, output_tok=1000,
                   at=ts(5)),
           ApiCall(0, "r2", model="mystery-model", input_tok=10, output_tok=10, at=ts(6))]
    calls = [ToolCall(0, "e1", "Edit", kind="edit", started_at=ts(30), has_result=True, is_error=True),
             ToolCall(0, "b1", "Bash", kind="bash", args_summary="ls", started_at=ts(31), has_result=True)]
    write(conn, Bundle(Session("s1", "claude", started_at=ts(0)), turns=[turn(0, 10, 60)], api_calls=api,
                       tool_calls=calls, events=[Event("compaction", ts(20), 0), Event("subagent", ts(21), 0)]))
    rid = run(conn)
    assert val(conn, rid, "unpriced_calls") == 1
    assert val(conn, rid, "cost_usd") == pytest.approx((100_000 * 4 + 100_000 * 0.2 + 1000 * 20) / 1e6)
    assert val(conn, rid, "context_p50") is not None
    assert val(conn, rid, "model_mix:claude-opus-5-5") == 1
    assert val(conn, rid, "tokens_in") == 100_010
    assert val(conn, rid, "compactions") == 1 and val(conn, rid, "subagents") == 1
    assert val(conn, rid, "tool_calls") == 2 and val(conn, rid, "tool_error_share") == 0.5
    assert val(conn, rid, "orientation_s", scope_type="session", scope_id="s1") == 30


def test_null_tolerance(conn):
    write(conn, Bundle(Session("s1", "claude"), turns=[turn(0, 0, None), Turn(idx=1, prompt_at=None)],
                       tool_calls=[ToolCall(None, "x", "Bash", kind="bash")],
                       api_calls=[ApiCall(None, "r", model=None)]))
    run(conn)


# --- idempotency -----------------------------------------------------------------------------------

def test_compute_all_idempotent(conn, tmp_path):
    write(conn, _g6(tmp_path), Bundle(Session("s2", "codex"), turns=[turn(0, 20, 80, interrupted=True)]))
    rid = run(conn)
    counts = lambda: (conn.execute("SELECT count(*) FROM metric_value").fetchone()[0],
                      conn.execute("SELECT count(*) FROM metric_evidence").fetchone()[0])
    first = counts()
    metrics.compute_all(conn, rid)
    assert counts() == first
    assert conn.execute("SELECT DISTINCT definition_version FROM metric_value").fetchall()[0][0] == "v1"


# --- tooling use -----------------------------------------------------------------------------------

def test_tooling(conn):
    ev = [
        Event("tooling", ts(1), 0, {"category": "command", "name": "review", "initiator": "human", "rule": "r"}),
        Event("tooling", ts(2), 0, {"category": "skill", "name": "pdf", "initiator": "agent", "tool_use_id": "t1"}),
        Event("tooling", ts(3), 0, {"category": "skill", "name": "pdf", "initiator": "agent", "tool_use_id": "t2"}),
        Event("tooling", ts(4), 0, {"category": "mcp", "name": "linear", "initiator": "agent"}),
    ]
    write(conn, Bundle(Session("s1", "claude"), turns=[turn(0, 0, 60)], events=ev))
    rid = run(conn)
    assert val(conn, rid, "tooling:skill:agent") == 2
    assert val(conn, rid, "tooling:command:human") == 1
    assert val(conn, rid, "tooling:skill") == 2
    assert val(conn, rid, "tooling:mcp") == 1
    assert val(conn, rid, "tooling_human_share") == 0.25
    notes = {r[0] for r in conn.execute(
        "SELECT note FROM metric_evidence e JOIN metric_value v ON v.id = e.metric_value_id"
        " WHERE v.metric='tooling:skill' AND v.scope_type='day'")}
    assert notes == {"skill:pdf:agent x2"}
