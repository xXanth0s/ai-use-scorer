"""Positive episodes (judge-v3, spec §1 C+): validation, deterministic gates, metrics, report line 6."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from scorer import config, judge
from scorer.db import connect


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    config.load.cache_clear()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SCORER_NO_LLM", "1")
    yield
    config.load.cache_clear()


def label(**kw):
    base = {"event": "new_requirement", "cause": "unknown", "detection": "unknown", "evidence": "",
            "confidence": 0.8, "defect_caught": False, "resolved": None, "user_action": None,
            "preventable_by_user": False, "action_category": "other", "user_strength": None,
            "strength_category": "other", "positive_episode": None, "episode_reason": None}
    base.update(kw)
    return base


class FakeClient:
    """Label calls answered by prompt substring (first match wins); anything else abstains."""

    def __init__(self, by_prompt):
        self.by_prompt, self.calls = by_prompt, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"].split("<user_prompt>", 1)[1]
        out = next((v for k, v in self.by_prompt.items() if k in text), {"event": "abstain"})
        return SimpleNamespace(stop_reason="tool_use",
                               content=[SimpleNamespace(type="tool_use", name=kw["tools"][0]["name"], input=out)])


def make_db(tmp_path, turns, interrupt_after=()):
    """turns: list of dicts {prompt, edits: [(path, lines)], checks: [(intent, is_error, initiator)]}.
    One session s1 on 2026-10-13 from 14:00 UTC, one turn every 6 min; edits at +1 min, checks at +2 min."""
    conn = connect(tmp_path / "t.db")
    conn.execute("INSERT INTO ingest_run(id, at, parser_version) VALUES (1, '2026-10-13T10:00:00+00:00', '1')")
    conn.execute("INSERT INTO session(id, agent, cwd) VALUES ('s1', 'claude', '/tmp/x')")
    for i, t in enumerate(turns):
        tid, m = i + 1, 6 * i
        at = lambda dm, sec=0: f"2026-10-13T14:{m + dm:02d}:{sec:02d}+00:00"  # noqa: E731
        conn.execute("INSERT INTO turn(id, session_id, idx, prompt_at, prompt_text, prompt_kind, end_source,"
                     " agent_finished_at) VALUES (?,?,?,?,?,?,?,?)",
                     (tid, "s1", i, at(0), t["prompt"], "prompt", "stop_hook", at(3)))
        for j, (path, lines) in enumerate(t.get("edits", ())):
            cid = conn.execute("INSERT INTO tool_call(turn_id, tool_use_id, name, kind, args_summary, started_at,"
                               " has_result) VALUES (?,?,?,?,?,?,1)",
                               (tid, f"e{tid}-{j}", "Edit", "edit", path, at(1, j))).lastrowid
            conn.execute("INSERT INTO file_edit(tool_call_id, path, manifest_dir, lines_added, lines_removed)"
                         " VALUES (?,?,?,?,?)", (cid, path, "/tmp/x", lines, 0))
        for j, (intent, err, init) in enumerate(t.get("checks", ())):
            conn.execute("INSERT INTO tool_call(turn_id, tool_use_id, name, kind, bash_intent, initiator, is_error,"
                         " args_summary, started_at, has_result) VALUES (?,?,?,?,?,?,?,?,?,1)",
                         (tid, f"c{tid}-{j}", "Bash", "bash", intent, init, int(err), "pytest -q",
                          at(2, j)))
        if i in interrupt_after:
            conn.execute("INSERT INTO event(session_id, turn_id, at, kind, data) VALUES ('s1', ?, ?, 'interrupt', '{}')",
                         (tid, at(0, 30)))
    conn.commit()
    return conn


def run(conn, labels):
    st = judge.run_judge(conn, 1, client=FakeClient(labels), chain=["api"])
    day = judge.candidates(conn, first=True)[0]["day"]
    return st, day


SPEC = ("Add a --json flag to the export command. Keep the CSV output unchanged. "
        "Done when test_export_json passes and ruff is clean.")
GOOD_SPEC = label(evidence="Done when test_export_json passes", positive_episode="good_spec",
                  episode_reason="Goal, constraint and acceptance test in the first prompt")


# --- schema + validation -----------------------------------------------------------------------

def test_schema_version_and_fields():
    assert judge.PROMPT_VERSION == "judge-v3"
    p = judge.SCHEMA["properties"]
    assert set(judge.SCHEMA["required"]) == set(p) >= {"positive_episode", "episode_reason"}
    enum = next(x["enum"] for x in p["positive_episode"]["anyOf"] if x.get("type") == "string")
    assert set(enum) == set(judge.EPISODES) == {"good_spec", "verification_catch", "early_redirect",
                                               "scoped_turn", "knew_when_not_to_delegate"}
    a = judge.abstain("rule")
    assert a["positive_episode"] is None and a["episode_reason"] is None


def test_validate_episode_unknown_no_evidence_abstain_and_quote():
    lab = judge.validate(GOOD_SPEC, SPEC, "api")
    assert lab["positive_episode"] == "good_spec" and lab["evidence"] in SPEC
    assert len(lab["episode_reason"]) <= judge.REASON_CHARS
    assert judge.validate(dict(GOOD_SPEC, positive_episode="great_vibes"), SPEC, "api")["positive_episode"] is None
    # no evidence -> the whole label abstains, and the episode with it
    lab = judge.validate(dict(GOOD_SPEC, evidence=""), SPEC, "api")
    assert lab["event"] == "abstain" and lab["positive_episode"] is None
    # evidence not a substring of the prompt -> abstain, episode null
    lab = judge.validate(dict(GOOD_SPEC, evidence="Done when all tests pass"), SPEC, "api")
    assert lab["event"] == "abstain" and lab["positive_episode"] is None
    # abstain event never carries an episode
    lab = judge.validate(dict(GOOD_SPEC, event="abstain"), SPEC, "api")
    assert lab["positive_episode"] is None and lab["episode_reason"] is None
    # no reason -> null; reason quoting > 80 prompt chars -> null; long reason clipped
    assert judge.validate(dict(GOOD_SPEC, episode_reason=None), SPEC, "api")["positive_episode"] is None
    assert judge.validate(dict(GOOD_SPEC, episode_reason=SPEC[:100]), SPEC, "api")["positive_episode"] is None
    lab = judge.validate(dict(GOOD_SPEC, episode_reason="x " * 200), SPEC, "api")
    assert len(lab["episode_reason"]) <= judge.REASON_CHARS
    # regex stand-in never proposes episodes
    assert judge.validate(judge.call_regex("", "no, wrong"), "no, wrong", "regex")["positive_episode"] is None


# --- gates -------------------------------------------------------------------------------------

def test_good_spec_gate_first_prompt_and_no_later_correction(tmp_path):
    conn = make_db(tmp_path, [{"prompt": SPEC}, {"prompt": "now add docs"}])
    st, day = run(conn, {"Done when": GOOD_SPEC,
                         "now add docs": label(evidence="now add docs")})
    eps = judge.episodes(conn, day)
    assert [(e["episode"], e["turn_id"], e["source"], e["uncalibrated"]) for e in eps] == \
        [("good_spec", 1, "judge", True)]
    assert eps[0]["evidence_quote"] == "Done when test_export_json passes"
    assert st["episodes"] == {"good_spec": 1} and st["n"] == 1   # first prompt not in C coverage
    # the first prompt never shows up in C rows
    rows = dict(conn.execute("SELECT metric, value FROM metric_value WHERE metric LIKE 'C%'").fetchall())
    assert rows["C:event:new_requirement"] == 1 and rows["C_labelled"] == 1
    assert rows["C+:good_spec"] == 1


def test_good_spec_blocked_by_later_correction_and_not_on_later_turns(tmp_path):
    conn = make_db(tmp_path, [{"prompt": SPEC}, {"prompt": "nein, das ist falsch"}, {"prompt": SPEC + " again"}])
    _, day = run(conn, {"falsch": label(event="correction", evidence="das ist falsch"),
                        "again": GOOD_SPEC, "Done when": GOOD_SPEC})
    assert judge.episodes(conn, day) == []


def test_verification_catch_gate(tmp_path):
    ask = "run the tests before we go on"
    vc = label(evidence="run the tests", positive_episode="verification_catch",
               episode_reason="Asked for a test run, which surfaced a failure")
    # human-initiated failing check -> counts
    conn = make_db(tmp_path, [{"prompt": "build it"}, {"prompt": ask, "checks": [("test", 1, "human")]}])
    _, day = run(conn, {"run the tests": vc})
    assert [(e["episode"], e["turn_id"]) for e in judge.episodes(conn, day)] == [("verification_catch", 2)]
    # passing check -> nothing caught
    conn2 = make_db(tmp_path / "b", [{"prompt": "build it"}, {"prompt": ask, "checks": [("test", 0, "human")]}])
    _, day = run(conn2, {"run the tests": vc})
    assert judge.episodes(conn2, day) == []
    # agent-initiated failure, fixed by a passing check in the next turn -> counts
    conn3 = make_db(tmp_path / "c", [{"prompt": "build it"}, {"prompt": ask, "checks": [("test", 1, "agent")]},
                                     {"prompt": "fix it", "edits": [("/tmp/x/a.py", 4)],
                                      "checks": [("test", 0, "agent")]}])
    _, day = run(conn3, {"run the tests": vc})
    assert [e["turn_id"] for e in judge.episodes(conn3, day) if e["episode"] == "verification_catch"] == [2]
    # no judge label -> no episode, even with a failing human check
    conn4 = make_db(tmp_path / "d", [{"prompt": "build it"}, {"prompt": ask, "checks": [("test", 1, "human")]}])
    _, day = run(conn4, {"run the tests": label(evidence="run the tests")})
    assert judge.episodes(conn4, day) == []


def test_early_redirect_gate(tmp_path):
    er = label(event="correction", evidence="stop, use the v2 API", positive_episode="early_redirect",
               episode_reason="Stopped a wrong path early and named the right API")
    p = "stop, use the v2 API because v1 is deprecated"
    conn = make_db(tmp_path, [{"prompt": "port the client"}, {"prompt": p}])
    _, day = run(conn, {"v2 API": er})
    assert [(e["episode"], e["turn_id"]) for e in judge.episodes(conn, day)] == [("early_redirect", 2)]
    # too late in the session (position 3)
    conn2 = make_db(tmp_path / "b", [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}, {"prompt": p}])
    _, day = run(conn2, {"v2 API": er})
    assert judge.episodes(conn2, day) == []
    # not a correction, but the previous agent turn was interrupted -> counts
    conn3 = make_db(tmp_path / "c", [{"prompt": "port the client"}, {"prompt": p}], interrupt_after=(0,))
    _, day = run(conn3, {"v2 API": dict(er, event="clarification")})
    assert [e["turn_id"] for e in judge.episodes(conn3, day)] == [2]
    # neither correction nor interrupt -> no
    conn4 = make_db(tmp_path / "d", [{"prompt": "port the client"}, {"prompt": p}])
    _, day = run(conn4, {"v2 API": dict(er, event="clarification")})
    assert judge.episodes(conn4, day) == []


def test_scoped_turn_gate_metrics_only_and_judge_upgrade(tmp_path):
    small = {"prompt": "fix the off-by-one in pager.py, done when test_pager passes",
             "edits": [("/tmp/x/pager.py", 6), ("/tmp/x/test_pager.py", 10)], "checks": [("test", 0, "agent")]}
    big = {"prompt": "refactor everything", "edits": [(f"/tmp/x/m{i}.py", 30) for i in range(4)],
           "checks": [("test", 0, "agent")]}
    failing = {"prompt": "tweak it", "edits": [("/tmp/x/a.py", 3)], "checks": [("test", 1, "agent")]}
    unverified = {"prompt": "tweak b", "edits": [("/tmp/x/b.py", 3)]}
    unmeasured = {"prompt": "rm c", "edits": [("/tmp/x/c.py", 0)], "checks": [("test", 0, "agent")]}  # Bash edit: 0/0
    conn = make_db(tmp_path, [{"prompt": "start"}, small, big, failing, unverified, unmeasured])
    _, day = run(conn, {})
    eps = judge.episodes(conn, day)
    assert [(e["episode"], e["turn_id"], e["source"], e["evidence_quote"], e["uncalibrated"]) for e in eps] == \
        [("scoped_turn", 2, "metrics", None, False)]
    assert "2 file(s), 16 line(s)" in eps[0]["reason"]
    # the judge agreeing upgrades it to a grounded, uncalibrated judge episode
    conn2 = make_db(tmp_path / "b", [{"prompt": "start"}, small])
    _, day = run(conn2, {"off-by-one": label(evidence="done when test_pager passes", positive_episode="scoped_turn",
                                             episode_reason="One bug, one file, explicit done criterion")})
    e = judge.episodes(conn2, day)[0]
    assert (e["source"], e["evidence_quote"], e["uncalibrated"]) == ("judge", "done when test_pager passes", True)


def test_knew_when_not_to_delegate_gate(tmp_path, monkeypatch):
    from scorer import metrics
    fail = lambda p: {"prompt": "try again", "edits": [(p, 5)], "checks": [("test", 1, "agent")]}  # noqa: E731
    conn = make_db(tmp_path, [{"prompt": "fix parser"}, fail("/tmp/x/src/parser.py"), fail("/tmp/x/src/parser.py"),
                              {"prompt": "I fixed parser.py by hand, carry on"}])
    hand = {"evidence": [{"turn_id": 4, "tool_call_id": None, "note": "repair: src/parser.py"}]}
    monkeypatch.setattr(metrics, "hand_edits", lambda conn, day=None: hand)
    _, day = run(conn, {})
    eps = [e for e in judge.episodes(conn, day) if e["episode"] == "knew_when_not_to_delegate"]
    assert [(e["turn_id"], e["source"], e["evidence_quote"]) for e in eps] == [(4, "metrics", None)]
    assert "parser.py" in eps[0]["reason"] and "2 failed check(s)" in eps[0]["reason"]
    # judge label on the hand-edit turn supplies the quote (fresh DB: the first run cached an abstain)
    conn = make_db(tmp_path / "j", [{"prompt": "fix parser"}, fail("/tmp/x/src/parser.py"),
                                    fail("/tmp/x/src/parser.py"), {"prompt": "I fixed parser.py by hand, carry on"}])
    _, day = run(conn, {"by hand": label(evidence="I fixed parser.py by hand",
                                         positive_episode="knew_when_not_to_delegate",
                                         episode_reason="Took over the file the agent kept breaking")})
    e = next(e for e in judge.episodes(conn, day) if e["episode"] == "knew_when_not_to_delegate")
    assert e["source"] == "judge" and e["evidence_quote"] == "I fixed parser.py by hand"
    # a hand edit on a file the agent was NOT struggling with does not count
    hand["evidence"] = [{"turn_id": 4, "tool_call_id": None, "note": "own: README.md"}]
    assert not [e for e in judge.episodes(conn, day) if e["episode"] == "knew_when_not_to_delegate"]
    # one failure is not a struggle
    conn2 = make_db(tmp_path / "b", [{"prompt": "fix parser"}, fail("/tmp/x/src/parser.py"), {"prompt": "done"}])
    hand["evidence"] = [{"turn_id": 3, "tool_call_id": None, "note": "repair: src/parser.py"}]
    _, day = run(conn2, {})
    assert not [e for e in judge.episodes(conn2, day) if e["episode"] == "knew_when_not_to_delegate"]


def test_same_file_matching():
    assert judge._same_file("src/parser.py", "/tmp/x/src/parser.py")
    assert judge._same_file("src/very/long/pa…", "/repo/src/very/long/path/file.py")
    assert not judge._same_file("parser.py", "/tmp/x/myparser.py")
    assert not judge._same_file("", "/a")


# --- metrics, strengths, report ----------------------------------------------------------------

def test_c_plus_rows_replaced_not_accumulated(tmp_path):
    conn = make_db(tmp_path, [{"prompt": SPEC}, {"prompt": "now add docs"}])
    labels = {"Done when": GOOD_SPEC}
    run(conn, labels)
    run(conn, labels)
    assert conn.execute("SELECT count(*) FROM metric_value WHERE metric='C+:good_spec'").fetchone()[0] == 1
    note, tid = conn.execute("SELECT e.note, e.turn_id FROM metric_evidence e JOIN metric_value v"
                             " ON v.id = e.metric_value_id WHERE v.metric='C+:good_spec'").fetchone()
    assert tid == 1 and note.startswith("good_spec: ") and len(note) <= len("good_spec: ") + 60


def test_strengths_list_episodes_first(tmp_path):
    conn = make_db(tmp_path, [{"prompt": SPEC}, {"prompt": "run the tests please", "checks": [("test", 1, "human")]}])
    _, day = run(conn, {"Done when": dict(GOOD_SPEC, user_strength="Gave acceptance criteria up front",
                                          strength_category="prompt_specificity"),
                        "run the tests": label(evidence="run the tests", positive_episode="verification_catch",
                                               episode_reason="Asked for a test run that caught a failure")})
    st = judge.strengths(conn, day)
    eps = [i["episode"] for i in st]
    assert eps[:2] == ["verification_catch", "good_spec"] and all(e is None for e in eps[2:])
    assert st[0]["text"].startswith("Verification catch: ") and st[0]["uncalibrated"] is True
    assert any(i["episode"] is None and i["source"] == "judge" for i in st)   # generic strengths stay


def _items_eps(*specs):
    return [{"episode": ep, "turn_id": t, "session_id": "s1", "reason": f"why {ep}", "evidence_quote": q,
             "source": src, "uncalibrated": src == "judge"} for ep, t, q, src in specs]


def test_pick_episode_priority():
    eps = _items_eps(("knew_when_not_to_delegate", 9, None, "metrics"), ("early_redirect", 3, "q", "judge"),
                     ("scoped_turn", 5, None, "metrics"), ("good_spec", 1, "spec", "judge"),
                     ("verification_catch", 7, "run tests", "judge"), ("verification_catch", 8, "x", "judge"))
    p = judge.pick_episode(eps)
    assert p["episode"] == "verification_catch" and p["turn_ids"] == [7, 8] and p["count"] == 2
    order = []
    while eps:
        p = judge.pick_episode(eps)
        order.append(p["episode"])
        eps = [e for e in eps if e["episode"] != p["episode"]]
    assert order == ["verification_catch", "good_spec", "scoped_turn", "early_redirect",
                     "knew_when_not_to_delegate"]
    assert judge.pick_episode([]) is None


def _metric(conn, metric, day, value, n):
    conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id, value, unit, n)"
                 " VALUES (1,?,?,?,?,?,?,?)", (metric, "v1", "day", day, value, "x", n))


LONGSPEC = SPEC + " Also never touch the legacy exporter, it is used by the nightly billing job in production."


def test_report_line6_picks_episode_by_priority_and_falls_back(tmp_path):
    from scorer import report
    conn = make_db(tmp_path, [{"prompt": LONGSPEC},
                              {"prompt": "run the tests please", "checks": [("test", 1, "human")]}])
    _, day = run(conn, {"Done when": dict(GOOD_SPEC, evidence=LONGSPEC[:190]),
                        "run the tests": label(evidence="run the tests", positive_episode="verification_catch",
                                               episode_reason="Asked for a test run that caught a failure")})
    with conn:
        _metric(conn, "turns", day, 2, 2)
    out = report.render(conn, day=day, days=None)
    line6 = next(l for l in out.splitlines() if l.startswith("6. **Repeat:**"))
    assert "Verification catch" in line6 and "turn:2" in line6 and "uncalibrated" in line6
    assert re.findall(r"^(\d)\. \*\*", out, re.M) == [str(i) for i in range(1, 9)]
    well = out.split("## What you did well", 1)[1].split("## Action items", 1)[0]
    assert well.index("Verification catch") < well.index("Good spec")
    # no prompt text longer than 80 chars anywhere in the report
    for line in out.splitlines():
        assert LONGSPEC[:81] not in line
        for m in re.findall(r'"([^"]+)"', line):
            assert len(m) <= 80
    # no episodes -> the metric-based pattern
    conn2 = make_db(tmp_path / "b", [{"prompt": "a"}, {"prompt": "b"}])
    _, day2 = run(conn2, {})
    with conn2:
        _metric(conn2, "turns", day2, 2, 2)
    f = report.facts(conn2, day2, run_id=1)
    assert f["episode"] is None
    out2 = report.render(conn2, day=day2, days=None)
    assert "6. **Repeat:** no verified edit batch or human-initiated tooling" in out2


def test_episode_fields_are_clipped(tmp_path):
    conn = make_db(tmp_path, [{"prompt": LONGSPEC}, {"prompt": "next"}])
    _, day = run(conn, {"Done when": dict(GOOD_SPEC, evidence=LONGSPEC[:200])})
    (e,) = judge.episodes(conn, day)
    assert set(e) == {"episode", "turn_id", "session_id", "reason", "evidence_quote", "source", "uncalibrated"}
    assert len(e["evidence_quote"]) <= 80 and len(e["reason"]) <= judge.REASON_CHARS
    stored = conn.execute("SELECT label FROM judge_label").fetchall()
    assert all(len(__import__("json").loads(r[0])["evidence"]) <= judge.EVIDENCE_CHARS for r in stored)
