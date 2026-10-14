"""Action items: per-turn user_action fields, daily aggregation (judge + metrics), report section."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from scorer import config, judge
from scorer.db import connect

DAY = None  # set from the candidates (local day of the fixture timestamps)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    config.load.cache_clear()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCORER_NO_LLM", raising=False)
    yield
    config.load.cache_clear()


def label(**kw):
    base = {"event": "correction", "cause": "info_missing", "detection": "human_review", "evidence": "",
            "confidence": 0.8, "defect_caught": True, "resolved": None, "user_action": None,
            "preventable_by_user": True, "action_category": "prompt_specificity"}
    base.update(kw)
    return base


class FakeClient:
    """Answers per tool name: label calls by prompt substring, cluster calls with `cluster`."""

    def __init__(self, by_prompt=None, cluster=None):
        self.by_prompt, self.cluster, self.calls = by_prompt or {}, cluster, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        name = kw["tools"][0]["name"]
        text = kw["messages"][0]["content"]
        if name == judge.CLUSTER_TOOL:
            out = self.cluster
        else:
            out = next((v for k, v in self.by_prompt.items() if k in text), {"event": "abstain"})
        return SimpleNamespace(stop_reason="tool_use",
                               content=[SimpleNamespace(type="tool_use", name=name, input=out)])


LONG = "Please always use the click library for the CLI and put every subcommand in its own module file here"


def make_db(tmp_path, prompts):
    conn = connect(tmp_path / "t.db")
    conn.execute("INSERT INTO ingest_run(id, at, parser_version) VALUES (1, '2026-10-13T10:00:00+00:00', '1')")
    conn.execute("INSERT INTO session(id, agent, cwd) VALUES ('s1', 'claude', '/tmp/x')")
    for i, p in enumerate(prompts):
        ts = f"2026-10-13T1{i}:00:00+00:00"
        conn.execute("INSERT INTO turn(id, session_id, idx, prompt_at, prompt_text, prompt_kind, end_source,"
                     " agent_finished_at) VALUES (?,?,?,?,?,?,?,?)",
                     (i + 1, "s1", i, ts, p, "prompt", "stop_hook", ts))
    conn.commit()
    return conn


# --- per-turn schema ---------------------------------------------------------------------------

def test_schema_has_action_fields_and_version_bumped():
    p = judge.SCHEMA["properties"]
    assert p["user_action"]["type"] == ["string", "null"]
    assert p["preventable_by_user"]["type"] == "boolean"
    assert set(p["action_category"]["enum"]) == set(judge.ACTION_CATEGORIES) == {
        "prompt_specificity", "verification", "context", "task_split", "tooling", "review", "other"}
    assert set(judge.SCHEMA["required"]) == set(p)
    assert judge.PROMPT_VERSION != "judge-v1"


def test_mocked_label_carries_action():
    prompt = "Nein, du hast die Tests nicht laufen lassen, bevor du fertig warst"
    client = FakeClient({"Nein": label(evidence="Tests nicht laufen lassen", action_category="verification",
                                       user_action="Ask for a test run before accepting edits")})
    lab, _ = judge.label_one({"prompt": prompt, "summary": "s"}, "claude-haiku-5-5", ["api"], client)
    assert lab["user_action"] == "Ask for a test run before accepting edits"
    assert lab["preventable_by_user"] is True and lab["action_category"] == "verification"
    assert client.calls[0]["tools"][0]["strict"] is True


def test_action_null_on_abstain_and_not_preventable():
    p = "continue"
    lab = judge.validate(label(event="abstain", user_action="Do something"), p, "api")
    assert lab["user_action"] is None and lab["preventable_by_user"] is False
    lab = judge.validate(label(evidence="x", preventable_by_user=False, user_action="Ask for tests"), "x y", "api")
    assert lab["user_action"] is None and lab["action_category"] == "other"
    assert judge.abstain("rule")["user_action"] is None
    # missing fields (old backends / regex) default, never abstain
    lab = judge.validate({"event": "correction", "evidence": "x"}, "x", "api")
    assert lab["event"] == "correction" and lab["user_action"] is None and lab["action_category"] == "other"


def test_action_quoting_more_than_80_prompt_chars_is_dropped():
    lab = judge.validate(label(evidence="click library", user_action=LONG[:110]), LONG, "api")
    assert lab["user_action"] is None
    lab = judge.validate(label(evidence="click library", user_action="x" * 300), LONG, "api")
    assert lab["user_action"] is None or len(lab["user_action"]) <= judge.ACTION_CHARS
    lab = judge.validate(label(evidence="click library", user_action="Name the CLI library in the first prompt"),
                         LONG, "api")
    assert lab["user_action"] == "Name the CLI library in the first prompt"


# --- daily aggregation -------------------------------------------------------------------------

PROMPTS = ["build the cli", "nein, du solltest click nehmen", "you never ran the tests, run them",
           "and again you did not test it", "now add docs"]
LABELS = {
    "click": label(evidence="click nehmen", user_action="Name the CLI library in the first prompt"),
    "never ran": label(evidence="never ran the tests", action_category="verification",
                       user_action="Ask for a test run before accepting edits"),
    "again": label(evidence="did not test it", action_category="verification",
                   user_action="Ask for tests before accepting edits"),
    "docs": label(event="new_requirement", evidence="now add docs", preventable_by_user=False,
                  user_action="Plan docs earlier", action_category="other"),
}


def _labelled(tmp_path):
    conn = make_db(tmp_path, PROMPTS)
    judge.run_judge(conn, 1, client=FakeClient(LABELS), chain=["api"])
    day = judge.candidates(conn)[0]["day"]
    return conn, day


def test_fallback_groups_by_category_and_ranks_verification_first(tmp_path):
    conn, day = _labelled(tmp_path)
    items = [i for i in judge.action_items(conn, day) if i["source"] == "judge"]
    assert [i["category"] for i in items] == ["verification", "prompt_specificity"]
    v = items[0]
    assert v["count"] == 2 and sorted(v["turn_ids"]) == [3, 4] and v["uncalibrated"] is True
    assert v["text"] in ("Ask for a test run before accepting edits", "Ask for tests before accepting edits")
    assert items[1]["turn_ids"] == [2] and items[1]["example_quote"] == "click nehmen"
    assert all(set(i) >= {"text", "category", "count", "turn_ids", "example_quote", "claude_md_line",
                          "uncalibrated", "source"} for i in items)
    assert conn.execute("SELECT count(*) FROM judge_action").fetchone()[0] == 0  # fallback is not cached


def test_api_clustering_merges_and_caches(tmp_path, monkeypatch):
    conn, day = _labelled(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cluster = {"items": [
        {"text": "Ask for a test run before accepting edits", "category": "verification", "turn_ids": [3, 4, 99],
         "claude_md_line": "Run `uv run pytest -q` after every edit and show the result."},
        {"text": "Name the CLI library in the first prompt", "category": "prompt_specificity", "turn_ids": [2],
         "claude_md_line": None}]}
    fake = FakeClient(cluster=cluster)
    monkeypatch.setattr(judge, "_api_client", lambda: fake)
    items = [i for i in judge.action_items(conn, day) if i["source"] == "judge"]
    assert len(fake.calls) == 1 and fake.calls[0]["tools"][0]["strict"] is True
    assert fake.calls[0]["tool_choice"] == {"type": "tool", "name": judge.CLUSTER_TOOL}
    assert items[0]["turn_ids"] == [3, 4] and items[0]["count"] == 2  # unknown id 99 dropped
    assert items[0]["claude_md_line"].startswith("Run ")
    assert conn.execute("SELECT count(*) FROM judge_action").fetchone()[0] == 1
    assert judge.action_items(conn, day) and len(fake.calls) == 1  # cached by run_hash


def test_api_cluster_failure_falls_back(tmp_path, monkeypatch):
    conn, day = _labelled(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(judge, "_api_client", lambda: FakeClient(cluster={"items": []}))
    items = [i for i in judge.action_items(conn, day) if i["source"] == "judge"]
    assert {i["category"] for i in items} == {"verification", "prompt_specificity"}


def test_no_item_quotes_more_than_80_prompt_chars(tmp_path, monkeypatch):
    conn = make_db(tmp_path, ["a", LONG])
    judge.run_judge(conn, 1, client=FakeClient({"click": label(evidence=LONG, user_action="Say it once")}),
                    chain=["api"])
    day = judge.candidates(conn)[0]["day"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    for it in judge.action_items(conn, day):
        for k in ("text", "example_quote", "claude_md_line"):
            assert not judge.quotes_prompt(it.get(k), [LONG])
        assert len(it["example_quote"]) <= 80


def _metric(conn, metric, day, value, n, evidence=()):
    mid = conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id,"
                       " value, unit, n) VALUES (1,?,?,?,?,?,?,?)",
                       (metric, "v1", "day", day, value, "x", n)).lastrowid
    for tid, note in evidence:
        conn.execute("INSERT INTO metric_evidence(metric_value_id, turn_id, note) VALUES (?,?,?)", (mid, tid, note))


def test_deterministic_metric_items(tmp_path):
    conn = make_db(tmp_path, ["a", "b", "c"])
    day = judge.candidates(conn)[0]["day"]
    with conn:
        _metric(conn, "V", day, 0.25, 4, [(1, "none: x"), (2, "verified_pass: y")])
        _metric(conn, "V_tier:verified_pass", day, 1, 4)
        _metric(conn, "B_min", day, 12.4, 2, [(2, "blocked 400s"), (3, "blocked 344s")])
        _metric(conn, "interrupts", day, 3, 3, [(1, "interrupt after 10s"), (2, "i"), (3, "i")])
        _metric(conn, "thrown_s", day, 240, 3)
    items = judge.action_items(conn, day, run_id=1)
    assert all(i["source"] == "metrics" and i["uncalibrated"] is False for i in items)
    by = {i["category"]: i for i in items}
    assert by["verification"]["count"] == 3 and "before accepting edits" in by["verification"]["text"]
    assert by["tooling"]["turn_ids"] == [2, 3] and "12 min blocked" in by["tooling"]["text"]
    assert by["prompt_specificity"]["count"] == 3 and "3×" in by["prompt_specificity"]["text"]
    assert items[0]["category"] == "verification"  # 3 × severity 3 ranks first
    # one interruption is not "repeated"
    with conn:
        conn.execute("UPDATE metric_value SET value=1 WHERE metric='interrupts'")
    assert "prompt_specificity" not in {i["category"] for i in judge.action_items(conn, day, run_id=1)}


def test_resolve_today_and_empty(tmp_path):
    conn = make_db(tmp_path, ["a"])
    assert judge.action_items(conn, "today") == []


# --- report ------------------------------------------------------------------------------------

def test_report_section_and_facts(tmp_path, monkeypatch):
    from scorer import report
    monkeypatch.setenv("SCORER_NO_LLM", "1")
    conn, day = _labelled(tmp_path)
    with conn:
        _metric(conn, "turns", day, 5, 5)
    f = report.facts(conn, day, run_id=1)
    assert f["action_items"] and f["action_items"][0]["category"] == "verification"
    out = report.render(conn, day=day, days=None)
    assert "## Action items" in out
    tail = out.split("## Action items", 1)[1]
    assert out.index("8. **Diagnostics") < out.index("## Action items")
    assert re.search(r"^1\. Ask for .*verification, 2× \(judge\) — turn:3, turn:4", tail, re.M)
    assert "uncalibrated" in tail
    assert re.findall(r"^(\d)\. \*\*", out, re.M) == [str(i) for i in range(1, 9)]
    for line in out.splitlines():
        for p in PROMPTS + [LONG]:
            assert len(p) <= 80 or p[:81] not in line


def test_report_claude_md_line_in_backticks(tmp_path):
    from scorer import report
    lines = report._render_actions([{"text": "Ask for tests", "category": "verification", "count": 1,
                                     "turn_ids": [7], "example_quote": "q", "claude_md_line": "Run `pytest`",
                                     "uncalibrated": True, "source": "judge"}])
    assert "1. Ask for tests — verification, 1× (judge) — turn:7" in lines
    assert "   - CLAUDE.md/AGENTS.md: `Run 'pytest'`" in lines
    assert report._render_actions([])[-1].startswith("none today")


# --- strengths -----------------------------------------------------------------------------------

def test_schema_has_strength_fields_and_validate():
    p = judge.SCHEMA["properties"]
    assert p["user_strength"]["type"] == ["string", "null"]
    assert set(p["strength_category"]["enum"]) == set(judge.ACTION_CATEGORIES)
    prompt = "Nein, der Test test_export schlägt fehl, schau dir den an"
    lab = judge.validate(label(evidence="test_export schlägt fehl", user_strength="Pointed the agent at the failing test",
                               strength_category="context"), prompt, "api")
    assert lab["user_strength"] == "Pointed the agent at the failing test" and lab["strength_category"] == "context"
    lab = judge.validate(label(event="abstain", user_strength="x"), prompt, "api")
    assert lab["user_strength"] is None and lab["strength_category"] == "other"
    lab = judge.validate(label(evidence="click library", user_strength=LONG[:100]), LONG, "api")
    assert lab["user_strength"] is None
    assert judge.abstain("rule")["user_strength"] is None


STRONG = dict(LABELS)
STRONG["click"] = label(evidence="click nehmen", user_strength="Named the library to use",
                        strength_category="prompt_specificity")
STRONG["never ran"] = label(evidence="run them", action_category="verification", cause="instruction_violated",
                            preventable_by_user=False, user_strength="Asked for a test run before accepting",
                            strength_category="verification")
STRONG["again"] = label(evidence="did not test it", cause="env_failure", preventable_by_user=False,
                        detection="automated_check", user_strength="Asked for tests again",
                        strength_category="verification")


def test_strengths_judge_and_metric_items(tmp_path):
    conn = make_db(tmp_path, PROMPTS)
    judge.run_judge(conn, 1, client=FakeClient(STRONG), chain=["api"])
    day = judge.candidates(conn)[0]["day"]
    st = judge.strengths(conn, day)
    j = [i for i in st if i["source"] == "judge"]
    assert j[0]["category"] == "verification" and j[0]["count"] == 2 and j[0]["uncalibrated"] is True
    assert {"text", "category", "count", "turn_ids", "example_quote", "uncalibrated", "source"} <= set(j[0])
    assert "claude_md_line" not in j[0]
    m = {i["text"]: i for i in st if i["source"] == "metrics"}
    caught = next(i for t, i in m.items() if t.startswith("Caught"))
    assert caught["turn_ids"] == [2, 3] and caught["category"] == "review"   # human_review corrections
    quick = next(i for t, i in m.items() if t.startswith("Corrected the agent within one turn"))
    assert quick["turn_ids"] == [4]   # turn 4's next turn is a new_requirement; 2 and 3 are followed by corrections


def test_quick_corrections_rule():
    rows = [({"session_id": "s", "turn_id": 1}, {"event": "correction"}),
            ({"session_id": "s", "turn_id": 2}, {"event": "correction"}),
            ({"session_id": "s", "turn_id": 3}, {"event": "new_requirement"}),
            ({"session_id": "s", "turn_id": 4}, {"event": "correction"})]
    assert judge._quick_corrections(rows) == [2]


# --- attribution ---------------------------------------------------------------------------------

def test_attribution_splits_corrections(tmp_path):
    conn = make_db(tmp_path, PROMPTS)
    judge.run_judge(conn, 1, client=FakeClient(STRONG), chain=["api"])
    day = judge.candidates(conn)[0]["day"]
    a = judge.attribution(conn, day)
    c = a["corrections"]
    assert c["user_preventable"] == {"count": 1, "turn_ids": [2]}   # info_missing
    assert c["agent_error"] == {"count": 1, "turn_ids": [3]}        # instruction_violated, not preventable
    assert c["environment"] == {"count": 1, "turn_ids": [4]}        # env_failure
    assert c["unclear"]["count"] == 0
    assert set(a["verification"]) == {"asked_by_you", "agent_initiative", "none"}
    assert set(a["tooling"]) == {"you", "agent", "default"}


def test_attribution_verification_and_tooling(tmp_path):
    conn = make_db(tmp_path, ["edit a", "edit b"])
    with conn:
        for tid, (init, intent) in enumerate((("human", "test"), ("agent", "test")), 1):
            ts = f"2026-10-13T1{tid - 1}:00:10+00:00"
            conn.execute("INSERT INTO tool_call(turn_id, tool_use_id, name, kind, args_summary, started_at,"
                         " has_result) VALUES (?,?,?,?,?,?,1)", (tid, f"e{tid}", "Edit", "edit", "/tmp/x/a.py", ts))
            cid = conn.execute("SELECT id FROM tool_call WHERE tool_use_id=?", (f"e{tid}",)).fetchone()[0]
            conn.execute("INSERT INTO file_edit(tool_call_id, path) VALUES (?, '/tmp/x/a.py')", (cid,))
            conn.execute("INSERT INTO tool_call(turn_id, tool_use_id, name, kind, bash_intent, initiator,"
                         " args_summary, started_at, has_result) VALUES (?,?,?,?,?,?,?,?,1)",
                         (tid, f"t{tid}", "Bash", "bash", intent, init, "pytest -q",
                          f"2026-10-13T1{tid - 1}:00:20+00:00"))
        conn.execute("INSERT INTO event(session_id, turn_id, at, kind, data) VALUES ('s1', 1, ?, 'tooling', ?)",
                     ("2026-10-13T10:00:05+00:00", json.dumps({"category": "skill", "name": "review",
                                                                "initiator": "human"})))
    from scorer import metrics
    day = metrics.tooling(conn)[0]["day"]   # turn 1's report day
    a = judge.attribution(conn, day)
    v = a["verification"]
    assert v["asked_by_you"]["count"] + v["agent_initiative"]["count"] + v["none"]["count"] >= 1
    assert a["tooling"]["you"] == {"count": 1, "turn_ids": [1]}


def test_report_strengths_and_attribution(tmp_path, monkeypatch):
    from scorer import report
    monkeypatch.setenv("SCORER_NO_LLM", "1")
    conn = make_db(tmp_path, PROMPTS)
    judge.run_judge(conn, 1, client=FakeClient(STRONG), chain=["api"])
    day = judge.candidates(conn)[0]["day"]
    with conn:
        _metric(conn, "turns", day, 5, 5)
    f = report.facts(conn, day, run_id=1)
    assert f["strengths"] and f["attribution"]["corrections"]["agent_error"]["count"] == 1
    out = report.render(conn, day=day, days=None)
    assert out.index("8. **Diagnostics") < out.index("## What you did well") < out.index("## Action items")
    sec7 = out.split("7. **", 1)[1].split("8. **", 1)[0]
    assert "you vs agent: V asked by you" in sec7 and "C you-preventable 1 / agent error 1 / environment 1" in sec7
    assert re.findall(r"^(\d)\. \*\*", out, re.M) == [str(i) for i in range(1, 9)]
