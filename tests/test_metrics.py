from datetime import datetime, timedelta, timezone

import pytest

from scorer import classify, config, db, ingest, metrics, model
from scorer.model import (ApiCall, Bundle, Event, FileEdit, Session, ToolCall, Turn, local_day)

T0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)   # noon UTC: same local day almost everywhere
DAY = local_day(T0)


def ts(sec: float) -> str:
    return (T0 + timedelta(seconds=sec)).isoformat()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SCORER_HOME", str(tmp_path / "home"))
    config.load.cache_clear()
    c = db.connect(tmp_path / "x.db")
    yield c
    c.close()
    config.load.cache_clear()


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
    classify._is_relevant.cache_clear()
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
    classify._is_relevant.cache_clear()
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


def test_hand_edit_git_snapshots_three_stops(conn):
    """changed_paths accumulates across Stops; only first appearances not touched by the agent count."""
    calls = [ToolCall(0, "e1", "Edit", kind="edit", args_summary="/r/a.py", started_at=ts(10), has_result=True),
             ToolCall(1, "e2", "Edit", kind="edit", args_summary="/r/c.py", started_at=ts(110), has_result=True)]
    snap = lambda sec, idx, paths: Event("git_snapshot", ts(sec), idx, {"git": {"head": "h", "changed_paths": paths}})
    events = [snap(50, 0, []),                                   # agent's a.py edit got committed
              snap(150, 1, ["b.py", "c.py"]),                    # b.py: own hand edit; c.py: agent (this window)
              snap(250, 2, ["a.py", "b.py", "c.py", "d.py"])]    # a.py: repair; d.py: own; b/c not re-counted
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=[turn(0, 0, 50), turn(1, 100, 150), turn(2, 200, 250)],
                       tool_calls=calls, file_edits=[FileEdit("e1", "/r/a.py"), FileEdit("e2", "/r/c.py")],
                       events=events))
    rid = run(conn)
    h = mv(conn, rid, "H")
    assert h["unit"] == "edits" and h["value"] == 3
    assert val(conn, rid, "H_repair") == 1
    assert val(conn, rid, "H_own") == 2
    notes = sorted(r[0] for r in conn.execute(
        "SELECT note FROM metric_evidence e JOIN metric_value v ON v.id = e.metric_value_id"
        " WHERE v.metric='H' AND v.scope_type='day'"))
    assert notes == ["own: b.py", "own: d.py", "repair: a.py"]


def test_hand_edit_snapshot_state_change(conn):
    snap = lambda sec, idx, h: Event("git_snapshot", ts(sec), idx,
                                     {"git": {"changed_paths": ["x.py"], "path_states": {"x.py": h}}})
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=[turn(0, 0, 50), turn(1, 100, 150)],
                       events=[snap(50, 0, "h1"), snap(150, 1, "h2")]))
    rid = run(conn)
    assert val(conn, rid, "H_own") == 2   # appears, then changes again without an agent edit


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
    assert conn.execute("SELECT DISTINCT definition_version FROM metric_value").fetchall()[0][0] == model.DEFINITION_VERSION == "v2"


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


# --- day cutoff / timezone ---------------------------------------------------------------------------

def test_day_cutoff_and_bounds(conn, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text('timezone = "Europe/Berlin"\nday_cutoff = "04:00"\n')
    config.load.cache_clear()
    # 2026-10-08 02:30 Berlin (CEST, UTC+2) = 00:30 UTC -> belongs to 2026-10-07
    t230 = datetime(2026, 10, 8, 0, 30, tzinfo=timezone.utc)
    assert local_day(t230) == "2026-10-07"
    lo, hi = metrics.day_bounds("2026-10-07")
    assert lo == datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)   # 04:00 Berlin
    assert hi == datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)
    turns = [Turn(0, t230.isoformat(), "x", agent_finished_at=(t230 + timedelta(minutes=10)).isoformat()),
             # 03:30 -> 04:30 Berlin: split 30 min / 30 min across the cutoff
             Turn(1, datetime(2026, 10, 8, 1, 30, tzinfo=timezone.utc).isoformat(), "y",
                  agent_finished_at=datetime(2026, 10, 8, 2, 30, tzinfo=timezone.utc).isoformat())]
    write(conn, Bundle(Session("s1", "claude"), turns=turns))
    d7 = metrics.turn_intervals(conn, "2026-10-07")
    d8 = metrics.turn_intervals(conn, "2026-10-08")
    assert metrics.union_seconds(d7) == 600 + 1800
    assert metrics.union_seconds(d8) == 1800
    rid = run(conn)
    assert val(conn, rid, "turns", scope_id="2026-10-07") == 2
    assert val(conn, rid, "active_s", scope_id="2026-10-07") == 2400
    assert val(conn, rid, "active_s", scope_id="2026-10-08") is None   # no turn starts on the 8th


# --- stored cost wins; per-model cost/tokens ------------------------------------------------------

def test_stored_cost_and_per_model(conn):
    api = [ApiCall(0, "r1", model="claude-opus-5-5", input_tok=1000, cached_tok=2000, output_tok=300,
                   cache_write_tok=500, cost_usd=1.23, at=ts(5)),
           ApiCall(0, "r2", model="claude-opus-5-5", input_tok=1_000_000, output_tok=0, at=ts(6)),   # NULL cost
           ApiCall(0, "r3", model="gpt-6-astra", input_tok=10, cached_tok=0, output_tok=5, cost_usd=0.5, at=ts(7))]
    write(conn, Bundle(Session("s1", "claude"), turns=[turn(0, 0, 60)], api_calls=api))
    rid = run(conn)
    assert val(conn, rid, "cost_usd:claude-opus-5-5") == pytest.approx(1.23 + 4.0)   # stored + recomputed
    assert val(conn, rid, "cost_usd:gpt-6-astra") == pytest.approx(0.5)
    assert val(conn, rid, "cost_usd") == pytest.approx(5.73)
    # write_bundle folds cache_write into input_tok: 1500 + 2000 + 300 + 1_000_000
    assert val(conn, rid, "tokens:claude-opus-5-5") == 1_003_800
    assert val(conn, rid, "tokens:gpt-6-astra") == 15


# --- agent-driven lanes (Codex subagent threads, initiator=agent) ----------------------------------

def test_subagent_threads_excluded_from_human_lanes(conn):
    parent = Bundle(Session("p", "codex", cwd="/a"),
                    turns=[turn(0, 0, 600), turn(1, 700, 760, text="now please add the missing tests too")],
                    api_calls=[ApiCall(0, "rp", model="gpt-6-astra", input_tok=100, output_tok=10, at=ts(5))],
                    events=[Event("subagent", ts(10), 0, {"kind": "started", "agent_thread_id": "child"}),
                            Event("subagent", ts(500), 0, {"kind": "completed", "agent_thread_id": "child"})])
    # child thread overlaps the parent completely and has a gap + interrupt of its own
    child = Bundle(Session("child", "codex", cwd="/a", forked_from="p"),
                   turns=[turn(0, 10, 200, initiator="agent", interrupted=True),
                          turn(1, 210, 400, initiator="agent", text="ok")],
                   api_calls=[ApiCall(0, "rc", model="gpt-6-astra", input_tok=1000, output_tok=100, at=ts(20))])
    # a Claude fork/resume IS human-driven and stays in
    fork = Bundle(Session("f", "claude", cwd="/b", forked_from="old"), turns=[turn(0, 2000, 2060)])
    write(conn, parent, child, fork)
    rid = run(conn)
    assert val(conn, rid, "overlap_s") == 0
    assert val(conn, rid, "parallel_s") == 0
    assert val(conn, rid, "active_s") == 600 + 60 + 60
    assert val(conn, rid, "B_min") == 0
    assert val(conn, rid, "interrupts") == 0
    assert val(conn, rid, "turns") == 3 and val(conn, rid, "sessions") == 2
    assert val(conn, rid, "gap_bucket:lt30s") == 0 and val(conn, rid, "gap_bucket:30s-2m") == 1
    # tokens/cost/model mix keep the child's calls; the child counts once as a subagent
    assert val(conn, rid, "tokens_in") == 1100
    assert val(conn, rid, "model_mix:gpt-6-astra") == 2
    assert val(conn, rid, "subagents") == 1


# --- one call that edits and then checks verifies itself -------------------------------------------

def _self_verify(tmp_path, cmd, error=False):
    (tmp_path / "pyproject.toml").write_text("")
    for f in (classify.manifest_dir, classify._walk, classify._has_manifest, classify._is_relevant):
        f.cache_clear()
    f = str(tmp_path / "a.py")
    calls = [ToolCall(0, "b1", "Bash", kind="bash", bash_intent=classify.bash_intent(cmd), args_summary=cmd,
                      started_at=ts(1), has_result=True, is_error=error)]
    return Bundle(Session("s1", "claude", cwd=str(tmp_path)), turns=[turn(0, 0, 50)], tool_calls=calls,
                  file_edits=[FileEdit("b1", f)])


def test_edit_then_test_in_one_call_is_verified(conn, tmp_path):
    write(conn, _self_verify(tmp_path, "cat > a.py ; pytest -q"))
    bs = metrics.verification_batches(conn, DAY)
    assert len(bs) == 1 and bs[0].tier == "verified_pass" and bs[0].verifier_call_id == bs[0].last_edit_call_id


def test_edit_then_failing_test_in_one_call(conn, tmp_path):
    write(conn, _self_verify(tmp_path, "sed -i s/a/b/ a.py && pytest -q", error=True))
    assert [b.tier for b in metrics.verification_batches(conn, DAY)] == ["verified_fail"]


def test_test_with_output_redirect_is_not_an_edit_batch(conn, tmp_path):
    write(conn, _self_verify(tmp_path, "pytest -q > out.log"))
    assert metrics.verification_batches(conn, DAY) == []


# --- herdr lab regressions ------------------------------------------------------------------------

def test_command_only_and_abandoned_turns_make_no_gaps(conn):
    turns = [turn(0, 0, 10),
             turn(1, 11, 11, end_source="interrupt", interrupted=True),           # Esc before any output
             turn(2, 24, 40, agent_started_at=ts(25)),                            # resubmitted
             turn(3, 100, 100, text="<command-name>/exit</command-name>", prompt_kind="command")]
    turns[0].agent_started_at = ts(1)
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=turns),
          Bundle(Session("s2", "claude", cwd="/q"), turns=[turn(0, 0, 200, agent_started_at=ts(1))]))
    g = metrics.gaps(conn, DAY)
    ids = {r[0]: r[1] for r in conn.execute("SELECT idx, id FROM turn WHERE session_id='s1'")}
    assert [(x.prev_turn_id, x.turn_id) for x in g] == [(ids[0], ids[1])]   # real -> abandoned only
    assert g[0].gap_s == 1
    assert [b["turn_id"] for b in metrics.blocked(conn, DAY)] == [ids[1]]   # no /exit or resubmit artifacts
    rid = run(conn)
    assert val(conn, rid, "interrupts") == 1


def test_scratchpad_edits_not_a_v_batch(conn):
    sp = "/private/tmp/claude-501/-r/abc/scratchpad"
    calls = [ToolCall(0, "e1", "Bash", kind="bash", bash_intent="edit",
                      args_summary=f"cd {sp} && cp /r/todo.py . && rm -f todo.json", started_at=ts(1), has_result=True),
             ToolCall(0, "e2", "Edit", kind="edit", args_summary="/r/todo.py", started_at=ts(2), has_result=True)]
    edits = [FileEdit("e1", sp + "/."), FileEdit("e1", sp + "/todo.json"), FileEdit("e2", "/r/todo.py")]
    write(conn, Bundle(Session("s1", "claude", cwd="/r", repo="/r"), turns=[turn(0, 0, 10)], tool_calls=calls,
                       file_edits=edits))
    bs = metrics.verification_batches(conn, DAY)
    assert len(bs) == 1 and bs[0].paths == ["/r/todo.py"]
    e2 = conn.execute("SELECT id FROM tool_call WHERE tool_use_id='e2'").fetchone()[0]
    assert bs[0].edit_call_ids == [e2]


def test_scratchpad_only_turn_has_no_batch(conn):
    calls = [ToolCall(0, "e1", "Bash", kind="bash", bash_intent="edit", args_summary="rm -rf /tmp/x/build",
                      started_at=ts(1), has_result=True)]
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=[turn(0, 0, 10)], tool_calls=calls,
                       file_edits=[FileEdit("e1", "/tmp/x/build")]))
    assert metrics.verification_batches(conn, DAY) == []


def _snap(sec, idx, changed, untracked=(), baseline=False):
    d = {"git": {"changed_paths": list(changed), "untracked_paths": list(untracked)}}
    if baseline:
        d["baseline"] = True
    return Event("git_snapshot", ts(sec), idx, d)


def test_hand_edit_new_untracked_file(conn):
    """A hand edit that creates a file shows up as untracked in the next Stop snapshot."""
    events = [_snap(-5, None, ["junk.log"], ["junk.log"], baseline=True),
              _snap(50, 0, ["junk.log"], ["junk.log"]),
              _snap(150, 1, ["junk.log", "notes.md", "newdir/"], ["junk.log", "notes.md", "newdir/"])]
    calls = [ToolCall(1, "e1", "Write", kind="edit", args_summary="/r/newdir/a.py", started_at=ts(110),
                      has_result=True)]
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=[turn(0, 0, 50), turn(1, 100, 150)], events=events,
                       tool_calls=calls, file_edits=[FileEdit("e1", "/r/newdir/a.py")]))
    h = metrics.hand_edits(conn, DAY)
    assert h["own"] == 1 and [e["note"] for e in h["evidence"]] == ["own: notes.md"]   # newdir/ = agent's


def test_untracked_in_first_snapshot_without_baseline_not_counted(conn):
    events = [_snap(50, 0, ["a.py", "old.log"], ["old.log"])]
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=[turn(0, 0, 50)], events=events))
    h = metrics.hand_edits(conn, DAY)
    assert [e["note"] for e in h["evidence"]] == ["own: a.py"]


def test_queued_prompts_metric(conn):
    turns = [turn(0, 0, 60), turn(1, 60, 120, prompt_kind="queued", enqueued_at=ts(30)), turn(2, 200, 260)]
    events = [Event("queue_enqueue", ts(210), 2, {"reason": "absorbed_mid_turn"}),
              Event("queue_enqueue", ts(220), 2, {"reason": "removed"})]
    write(conn, Bundle(Session("s1", "claude"), turns=turns, events=events))
    rid = run(conn)
    assert val(conn, rid, "queued_prompts") == 2
    assert val(conn, rid, "queued_prompts", scope_type="all", scope_id="all") == 2


# --- hand edits vs background subagents / parallel sessions --------------------------------------
# Real case (redacted): a session's first Stop snapshot listed ~13 files written by *another* session's
# background subagents in the same repo (and by subagents still running after their parent turn ended);
# all were counted as the human's own hand edits.

def _hsnap(sec, idx, paths):
    return Event("git_snapshot", ts(sec), idx, {"git": {"head": "h", "changed_paths": paths}})


def test_background_subagent_edit_after_turn_end_is_not_a_hand_edit(conn):
    # parent turn 0 ends at 50 s; its background subagent writes b.py at 70 s; the next Stop at 150 s
    calls = [ToolCall(0, "e1", "Edit", kind="edit", args_summary="/r/a.py", started_at=ts(10), has_result=True),
             ToolCall(0, "s1", "Write", kind="edit", args_summary="/r/b.py", started_at=ts(70), has_result=True,
                      sidechain_id="bg1")]
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=[turn(0, 0, 50), turn(1, 100, 150)],
                       tool_calls=calls, file_edits=[FileEdit("e1", "/r/a.py"), FileEdit("s1", "/r/b.py")],
                       events=[_hsnap(50, 0, ["a.py"]), _hsnap(150, 1, ["a.py", "b.py"])]))
    h = metrics.hand_edits(conn, DAY)
    assert h["count"] == 0 and not h["partial"], h["evidence"]


def test_parallel_session_edit_in_same_repo_is_not_a_hand_edit(conn):
    # session A only reads; session B (same repo) edits x.py and y.py; A's snapshots see them dirty
    a = Bundle(Session("sA", "claude", cwd="/r", repo="/r"), turns=[turn(0, 0, 50), turn(1, 100, 150)],
               events=[_hsnap(50, 0, []), _hsnap(150, 1, ["x.py", "y.py", "z.py"])])
    b = Bundle(Session("sB", "claude", cwd="/r/sub", repo="/r"), turns=[turn(0, 60, 140)],
               tool_calls=[ToolCall(0, "bx", "Edit", kind="edit", args_summary="/r/x.py", started_at=ts(70),
                                    has_result=True),
                           ToolCall(0, "by", "Bash", kind="bash", bash_intent="edit", args_summary="cp t y.py",
                                    started_at=ts(80), has_result=True, sidechain_id="sub1")],
               file_edits=[FileEdit("bx", "/r/x.py"), FileEdit("by", "/r/y.py")])
    # another repo editing z.py does not excuse it
    c = Bundle(Session("sC", "claude", cwd="/other", repo="/other"), turns=[turn(0, 60, 140)],
               tool_calls=[ToolCall(0, "cz", "Edit", kind="edit", args_summary="/other/z.py", started_at=ts(70),
                                    has_result=True)],
               file_edits=[FileEdit("cz", "/other/z.py")])
    write(conn, a, b, c)
    h = metrics.hand_edits(conn, DAY)
    assert [e["note"] for e in h["evidence"]] == ["own: z.py"]


def test_parallel_session_edit_before_window_still_counts(conn):
    # B edited x.py before A's previous snapshot: x.py first appears later -> not excused by B
    a = Bundle(Session("sA", "claude", cwd="/r", repo="/r"), turns=[turn(0, 0, 50), turn(1, 100, 150)],
               events=[_hsnap(50, 0, []), _hsnap(150, 1, ["x.py"])])
    b = Bundle(Session("sB", "claude", cwd="/r", repo="/r"), turns=[turn(0, 10, 40)],
               tool_calls=[ToolCall(0, "bx", "Edit", kind="edit", args_summary="/r/x.py", started_at=ts(20),
                                    has_result=True)],
               file_edits=[FileEdit("bx", "/r/x.py")])
    write(conn, a, b)
    assert metrics.hand_edits(conn, DAY)["own"] == 1


def test_inline_python_edit_is_not_a_hand_edit(conn):
    # the call's intent is "run", but the parser recorded the file it wrote: not the human's edit
    calls = [ToolCall(0, "p1", "Bash", kind="bash", bash_intent="run", args_summary="python3 - <<'EOF' …",
                      started_at=ts(20), has_result=True)]
    write(conn, Bundle(Session("s1", "claude", cwd="/r"), turns=[turn(0, 0, 50)], tool_calls=calls,
                       file_edits=[FileEdit("p1", "/r/m.py")],
                       events=[_hsnap(1, 0, []), _hsnap(50, 0, ["m.py"])]))
    assert metrics.hand_edits(conn, DAY)["count"] == 0
    assert [b.tier for b in metrics.verification_batches(conn, DAY)] == []   # V unchanged: not an edit batch


# --- V tier v2: last relevant check per window, lanes per sidechain -----------------------------
# Real case (redacted): one turn, 9 test runs (5 in subagents); a TDD red run (`cat > test ; pytest`) and
# one other non-zero exit, then green -> the dashboard showed "Check failed".

def _tier_bundle(tmp_path, calls, edits):
    (tmp_path / "pyproject.toml").write_text("")
    for f in (classify.manifest_dir, classify._walk, classify._has_manifest, classify._is_relevant):
        f.cache_clear()
    return Bundle(Session("s1", "claude", cwd=str(tmp_path)), turns=[turn(0, 0, 500)], tool_calls=calls,
                  file_edits=edits)


def test_red_then_green_is_pass_and_recovered(conn, tmp_path):
    f = str(tmp_path / "a.py")
    calls = [ToolCall(0, "e1", "Edit", kind="edit", args_summary=f, started_at=ts(1), has_result=True),
             ToolCall(0, "t1", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(2),
                      has_result=True, is_error=True),
             ToolCall(0, "t2", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(3),
                      has_result=True)]
    write(conn, _tier_bundle(tmp_path, calls, [FileEdit("e1", f)]))
    [b] = metrics.verification_batches(conn, DAY)
    assert (b.tier, b.recovered) == ("verified_pass", True)
    rid = run(conn)
    assert val(conn, rid, "V_recovered") == 1 and val(conn, rid, "V_tier:verified_fail") == 0


def test_last_check_failing_is_fail(conn, tmp_path):
    f = str(tmp_path / "a.py")
    calls = [ToolCall(0, "e1", "Edit", kind="edit", args_summary=f, started_at=ts(1), has_result=True),
             ToolCall(0, "t1", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(2),
                      has_result=True),
             ToolCall(0, "t2", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(3),
                      has_result=True, is_error=True)]
    write(conn, _tier_bundle(tmp_path, calls, [FileEdit("e1", f)]))
    [b] = metrics.verification_batches(conn, DAY)
    assert (b.tier, b.recovered) == ("verified_fail", False)


def test_check_after_next_edit_does_not_decide_earlier_batch(conn, tmp_path):
    f = str(tmp_path / "a.py")
    calls = [ToolCall(0, "e1", "Edit", kind="edit", args_summary=f, started_at=ts(1), has_result=True),
             ToolCall(0, "t1", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(2),
                      has_result=True, is_error=True),
             ToolCall(0, "e2", "Edit", kind="edit", args_summary=f, started_at=ts(3), has_result=True),
             ToolCall(0, "t2", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(4),
                      has_result=True)]
    write(conn, _tier_bundle(tmp_path, calls, [FileEdit("e1", f), FileEdit("e2", f)]))
    b1, b2 = metrics.verification_batches(conn, DAY)
    assert (b1.tier, b1.followed_by_edit, b2.tier, b2.recovered) == ("verified_fail", True, "verified_pass", False)


def test_tdd_red_edit_check_call_then_green(conn, tmp_path):
    t_ = str(tmp_path / "tests" / "test_a.py")
    calls = [ToolCall(0, "r1", "Bash", kind="bash", bash_intent="test", started_at=ts(1), has_result=True,
                      is_error=True, args_summary="cat > tests/test_a.py ; pytest -q"),
             ToolCall(0, "t2", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(2),
                      has_result=True)]
    write(conn, _tier_bundle(tmp_path, calls, [FileEdit("r1", t_)]))
    [b] = metrics.verification_batches(conn, DAY)
    assert (b.tier, b.recovered) == ("verified_pass", True)


def test_subagent_lane_not_interleaved_with_parent(conn, tmp_path):
    f, g = str(tmp_path / "a.py"), str(tmp_path / "b.py")
    calls = [ToolCall(0, "e1", "Edit", kind="edit", args_summary=f, started_at=ts(1), has_result=True),
             ToolCall(0, "se", "Edit", kind="edit", args_summary=g, started_at=ts(2), has_result=True,
                      sidechain_id="sub1"),
             ToolCall(0, "t1", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(3),
                      has_result=True),
             # the subagent's own run fails after the parent's passed: it must not flip the parent's batch,
             # and the parent's pass must not count for the subagent's batch
             ToolCall(0, "st", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(4),
                      has_result=True, is_error=True, sidechain_id="sub1")]
    write(conn, _tier_bundle(tmp_path, calls, [FileEdit("e1", f), FileEdit("se", g)]))
    bs = {b.edit_call_ids[0]: b for b in metrics.verification_batches(conn, DAY)}
    ids = {r["tool_use_id"]: r["id"] for r in conn.execute("SELECT id, tool_use_id FROM tool_call")}
    assert bs[ids["e1"]].tier == "verified_pass" and bs[ids["e1"]].verifier_call_id == ids["t1"]
    assert bs[ids["se"]].tier == "verified_fail" and bs[ids["se"]].verifier_call_id == ids["st"]


def test_parent_check_after_subagent_finished_verifies_its_batch(conn, tmp_path):
    g = str(tmp_path / "b.py")
    calls = [ToolCall(0, "se", "Edit", kind="edit", args_summary=g, started_at=ts(2), has_result=True,
                      sidechain_id="sub1"),
             ToolCall(0, "sr", "Read", kind="read", args_summary=g, started_at=ts(3), has_result=True,
                      sidechain_id="sub1"),
             ToolCall(0, "t1", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(10),
                      has_result=True, initiator="human")]
    write(conn, _tier_bundle(tmp_path, calls, [FileEdit("se", g)]))
    [b] = metrics.verification_batches(conn, DAY)
    assert (b.tier, b.verifier_initiator) == ("verified_pass", "human")


def test_subagent_lane_spans_turns(conn, tmp_path):
    # background subagent edits in turn 0's window and runs its tests during turn 1's window
    (tmp_path / "pyproject.toml").write_text("")
    for f in (classify.manifest_dir, classify._walk, classify._has_manifest, classify._is_relevant):
        f.cache_clear()
    g = str(tmp_path / "b.py")
    calls = [ToolCall(0, "se", "Edit", kind="edit", args_summary=g, started_at=ts(20), has_result=True,
                      sidechain_id="sub1"),
             ToolCall(1, "st", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(110),
                      has_result=True, sidechain_id="sub1")]
    write(conn, Bundle(Session("s1", "claude", cwd=str(tmp_path)), turns=[turn(0, 0, 50), turn(1, 100, 150)],
                       tool_calls=calls, file_edits=[FileEdit("se", g)]))
    [b] = metrics.verification_batches(conn, DAY)
    tid0 = conn.execute("SELECT id FROM turn WHERE idx=0").fetchone()[0]
    assert b.tier == "verified_pass" and b.turn_id == tid0


def test_tdd_red_step_then_green_fix_is_recovered(conn, tmp_path):
    # `cat > test ; pytest` red (own batch), then the fix edit and a green run: no "Check failed"
    t_, f = str(tmp_path / "tests" / "test_a.py"), str(tmp_path / "a.py")
    calls = [ToolCall(0, "r1", "Bash", kind="bash", bash_intent="test", started_at=ts(1), has_result=True,
                      is_error=True, args_summary="cat > tests/test_a.py ; pytest -q", sidechain_id="sub1"),
             ToolCall(0, "e1", "Edit", kind="edit", args_summary=f, started_at=ts(2), has_result=True,
                      sidechain_id="sub1"),
             ToolCall(0, "t2", "Bash", kind="bash", bash_intent="test", args_summary="pytest -q", started_at=ts(3),
                      has_result=True, sidechain_id="sub1")]
    write(conn, _tier_bundle(tmp_path, calls, [FileEdit("r1", t_), FileEdit("e1", f)]))
    assert [(b.tier, b.recovered) for b in metrics.verification_batches(conn, DAY)] == \
        [("verified_pass", True), ("verified_pass", False)]
