import re
from datetime import datetime, timedelta, timezone

import pytest

from scorer import config, db, ingest, metrics, report
from scorer.model import ApiCall, Bundle, FileEdit, Session, ToolCall, Turn, local_day

T0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
DAY = local_day(T0)
PREV = local_day(T0 - timedelta(days=1))
MARK = "SECRETPROMPTMARKER "


def ts(sec: float, base=T0) -> str:
    return (base + timedelta(seconds=sec)).isoformat()


def long_prompt(i: int) -> str:
    return (MARK + f"turn {i} " + "please refactor the module and keep the public api stable " * 4).strip()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SCORER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SCORER_NO_LLM", "1")
    config.load.cache_clear()
    c = db.connect(tmp_path / "x.db")
    yield c
    c.close()
    config.load.cache_clear()


def write(conn, *bundles):
    with conn:
        for b in bundles:
            ingest.write_bundle(conn, b)


def run(conn) -> int:
    rid = conn.execute("INSERT INTO ingest_run(at, parser_version) VALUES ('now','1')").lastrowid
    conn.commit()
    metrics.compute_all(conn, rid)
    return rid


def val(conn, rid, metric, day=DAY):
    r = conn.execute("SELECT value FROM metric_value WHERE run_id=? AND metric=? AND scope_type='day'"
                     " AND scope_id=?", (rid, metric, day)).fetchone()
    return r[0] if r else None


def edit(idx, tuid, path, at):
    return (ToolCall(idx, tuid, "Edit", kind="edit", args_summary=path, started_at=at, has_result=True),
            FileEdit(tuid, path))


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "pyproject.toml").write_text("[project]\nname='x'\n")
    (r / "uv.lock").write_text("")
    return r


def fixture_bundles(repo):
    a, b = str(repo / "src" / "alpha.py"), str(repo / "src" / "beta.py")
    # session s1: turn0 edit+pytest (verified), turn1 edit only (unverified), turn2 interrupted after 300s
    t1 = [Turn(0, ts(0), long_prompt(0), agent_finished_at=ts(100)),
          Turn(1, ts(130), long_prompt(1), agent_finished_at=ts(400)),
          Turn(2, ts(420), long_prompt(2), agent_finished_at=ts(720), interrupted=True)]
    e0, f0 = edit(0, "e0", a, ts(10))
    e1, f1 = edit(1, "e1", b, ts(150))
    tc1 = [e0, ToolCall(0, "p0", "Bash", kind="bash", bash_intent="test", args_summary="uv run pytest -q",
                        started_at=ts(20), has_result=True, initiator="human"), e1]
    s1 = Bundle(Session("s1aaaaaaaaaa", "claude", cwd=str(repo)), turns=t1, tool_calls=tc1, file_edits=[f0, f1],
                api_calls=[ApiCall(0, "r1", model="claude-opus-5-5", input_tok=1000, cached_tok=5000,
                                   output_tok=200, at=ts(30))])
    # session s2 runs while s1 idles in its gap (blocked time for s1)
    t2 = [Turn(0, ts(100), "go", agent_finished_at=ts(125)), Turn(1, ts(140), "ok", agent_finished_at=ts(500))]
    s2 = Bundle(Session("s2bbbbbbbbbb", "codex", cwd=str(repo)), turns=t2)
    # previous day: one session, one verified batch -> baseline
    base = T0 - timedelta(days=1)
    t3 = [Turn(0, ts(0, base), long_prompt(9), agent_finished_at=ts(200, base))]
    e3, f3 = edit(0, "e3", a, ts(10, base))
    tc3 = [e3, ToolCall(0, "p3", "Bash", kind="bash", bash_intent="test", args_summary="uv run pytest -q",
                        started_at=ts(20, base), has_result=True)]
    s3 = Bundle(Session("s3cccccccccc", "claude", cwd=str(repo)), turns=t3, tool_calls=tc3, file_edits=[f3])
    return s1, s2, s3


@pytest.fixture()
def filled(conn, repo):
    write(conn, *fixture_bundles(repo))
    rid = run(conn)
    return conn, rid


def sections(text: str) -> list[str]:
    return re.findall(r"^(\d)\. \*\*", text, re.M)


def test_all_eight_sections(filled):
    conn, _ = filled
    out = report.render(conn, day=DAY, days=None)
    assert sections(out) == [str(i) for i in range(1, 9)]
    for label in ("Verdict", "Tomorrow", "correction", "Unverified", "attention window", "Repeat",
                  "Scored metrics", "Diagnostics"):
        assert label in out
    assert "## Narrative" not in out


def test_numbers_match_metric_rows(filled):
    conn, rid = filled
    out = report.render(conn, day=DAY, days=None)
    f = report.facts(conn, DAY)
    # verdict candidates are derived from stored rows
    vn = conn.execute("SELECT n FROM metric_value WHERE run_id=? AND metric='V' AND scope_id=?",
                      (rid, DAY)).fetchone()[0]
    verified = val(conn, rid, "V_tier:verified_pass") + val(conn, rid, "V_tier:verified_fail")
    c = f["verdict"]["candidates"]
    assert c["unverified"] == (vn - verified) * report.UNVERIFIED_FIX_MIN
    assert c["blocked"] == round(val(conn, rid, "B_min"), 1)
    assert c["interrupted"] == round(val(conn, rid, "thrown_s") / 60, 1)
    assert val(conn, rid, "thrown_s") == 300
    assert f["verdict"]["category"] == max(c, key=c.get)
    # scored table: V today
    row = next(s for s in f["scored"] if s["metric"] == "V")
    assert row["today"] == val(conn, rid, "V")
    assert f"| V verified share | {100 * val(conn, rid, 'V'):.0f}% |" in out
    assert row["baseline"] == val(conn, rid, "V", PREV) == 1.0
    assert "▼ worse" in out  # V 50% today vs 100% baseline
    # V counter-metric split: "verified, then edited again" vs failed checks
    assert f"|   V checks that failed | {val(conn, rid, 'V_tier:verified_fail'):.0f} |" in out
    assert "V verified, then edited again" in out
    # H without git snapshots: not measured, not 0
    assert "| H hand-edits | not measured yet (no git snapshots) | n/a | n/a |" in out
    assert "hand-edits: not measured yet (no git snapshots)" in out
    # diagnostics
    assert f"interrupts {val(conn, rid, 'interrupts'):.0f}" in out
    assert f"${val(conn, rid, 'cost_usd'):.2f}" in out
    assert "opus-5-5" in out
    # unverified-now: beta.py in s1 with the uv command for the pyproject dir
    assert "beta.py" in out and "`uv run pytest -q`" in out
    assert "alpha.py" not in out.split("4. **")[1].split("5. **")[0]
    # attention window exists (s1 waited while s2 ran)
    assert f["worst_window"] and f["worst_window"]["minutes"] > 0
    assert "turn:" in out
    # judge not run
    assert "judge pending" in out


def test_no_prompt_text_leaks(filled):
    conn, _ = filled
    outs = [report.render(conn, day=DAY, days=None), report.render(conn, day=DAY, days=7)]
    prompts = [r[0] for r in conn.execute("SELECT prompt_text FROM turn") if r[0] and len(r[0]) > 80]
    assert prompts
    for out in outs:
        for line in out.splitlines():
            for p in prompts:
                assert p[:81] not in line
        assert MARK.strip() not in out


def test_empty_db(conn):
    out = report.render(conn, day=DAY, days=None)
    assert f"no data for {DAY}" in out
    out = report.render(conn, day="today", days=None)
    assert "no data for" in out
    out = report.render(conn, day=DAY, days=7)
    assert "no data for" in out


def test_days_table(filled):
    conn, rid = filled
    out = report.render(conn, day=DAY, days=7)
    assert "| day | active h |" in out
    rows = [l for l in out.splitlines() if re.match(r"^\| \d{4}-\d{2}-\d{2} \|", l)]
    assert len(rows) == 7 and rows[-1].startswith(f"| {DAY} ") and rows[-2].startswith(f"| {PREV} ")
    assert f"| {val(conn, rid, 'turns'):.0f} |" in rows[-1]
    assert "| n/m |" in rows[-1]   # H not measured without git snapshots
    assert sections(out) == [str(i) for i in range(1, 9)]
    assert f"## {DAY}" in out


def test_days_focus_is_latest_day_with_data(filled):
    conn, _ = filled
    later = (datetime.strptime(DAY, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
    out = report.render(conn, day=later, days=7)
    assert f"## {DAY} (latest day with data)" in out


def test_judge_corrections_rendered(filled):
    conn, rid = filled
    tid = conn.execute("SELECT id FROM turn WHERE session_id='s1aaaaaaaaaa' AND idx=1").fetchone()[0]
    with conn:
        mid = conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id,"
                           " value, unit, n) VALUES (?,?,?,?,?,?,?,?)",
                           (rid, "C:cause:info_missing", "v1", "day", DAY, 2, "turns", 3)).lastrowid
        conn.execute("INSERT INTO metric_evidence(metric_value_id, turn_id, note) VALUES (?,?,?)",
                     (mid, tid, "correction/info_missing/human_review: say which db file to use"))
        for metric, v in (("C:event:correction", 2), ("C:detection:human_review", 2), ("C:event:uncertain", 1),
                          ("C_source:regex", 3)):
            conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id,"
                         " value, unit, n) VALUES (?,?,?,?,?,?,?,?)", (rid, metric, "v1", "day", DAY, v, "turns", 3))
        conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id,"
                     " value, unit, n) VALUES (?,?,?,?,?,?,?,?)", (rid, "C_abstain", "v1", "day", DAY, 1, "count", 1))
    out = report.render(conn, day=DAY, days=None)
    assert f'info_missing: "say which db file to use" (turn:{tid})' in out
    assert "would have prevented it: state the missing fact" in out
    assert "| C corrections | 2 |" in out          # by cause only; event/detection rows not summed
    assert "C cause: info_missing | 2 |" in out
    assert "abstained on 1 turn(s)" in out and "regex stand-in" in out and "1 turn(s) labelled uncertain" in out


def test_judge_ran_no_corrections(filled):
    conn, rid = filled
    with conn:
        conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id,"
                     " value, unit, n) VALUES (?,?,?,?,?,?,?,?)", (rid, "C:event:new_requirement", "v1", "day",
                                                                    DAY, 2, "turns", 2))
    out = report.render(conn, day=DAY, days=None)
    assert "the judge found no corrections today" in out
    assert "| C corrections | 0 |" in out and "Judge unsure: nothing flagged" in out


def test_narrative_used_and_failures_swallowed(filled, monkeypatch):
    conn, _ = filled
    from scorer import agent
    monkeypatch.delenv("SCORER_NO_LLM")
    monkeypatch.setattr(agent, "has_key", lambda: True)
    seen = {}

    def fake(c, day, facts):
        seen["facts"] = facts
        return "- **Verdict:** test [turn:1]"
    monkeypatch.setattr(agent, "narrative", fake)
    out = report.render(conn, day=DAY, days=None)
    assert out.index("## Narrative") < out.index("1. **Verdict")
    assert seen["facts"]["day"] == DAY

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(agent, "narrative", boom)
    out = report.render(conn, day=DAY, days=None)
    assert "## Narrative" not in out and "RuntimeError" not in out
    monkeypatch.setenv("SCORER_NO_LLM", "1")
    monkeypatch.setattr(agent, "narrative", fake)
    assert "## Narrative" not in report.render(conn, day=DAY, days=None)
