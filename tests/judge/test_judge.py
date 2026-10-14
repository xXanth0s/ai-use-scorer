"""Judge tests (spec §6d). No network: the Anthropic client and `claude -p` are mocked."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scorer import config, judge
from scorer.db import connect
from scorer.ingest import JUDGE_SLUG_MARKER

CASES = sorted(Path(__file__).parent.joinpath("cases").glob("*.json"))
DAY_TS = "2026-10-13T10:00:00+00:00"


def load(p: Path) -> dict:
    return json.loads(p.read_text())


@pytest.fixture(autouse=True)
def _fresh_config():
    config.load.cache_clear()
    yield
    config.load.cache_clear()


class FakeClient:
    """Stands in for anthropic.Anthropic: returns the queued tool inputs in order."""

    def __init__(self, outputs, stop_reason="tool_use"):
        self.outputs = list(outputs)
        self.calls = []
        self.stop_reason = stop_reason
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        out = self.outputs.pop(0) if self.outputs else {}
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="tool_use", name=judge.TOOL_NAME, input=out)])


def assert_schema(lab: dict):
    assert set(judge.SCHEMA["required"]) <= set(lab)
    assert lab["event"] in judge.EVENTS
    assert lab["cause"] in judge.CAUSES
    assert lab["detection"] in judge.DETECTIONS
    assert 0.0 <= lab["confidence"] <= 1.0
    assert isinstance(lab["defect_caught"], bool)
    assert lab["resolved"] in (True, False, None)
    assert len(lab["evidence"]) <= judge.EVIDENCE_CHARS
    assert lab["source"] in judge.SOURCES


# --- schema --------------------------------------------------------------------------------------

def test_schema_is_strict_and_includes_abstain():
    s = judge.SCHEMA
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    assert "abstain" in s["properties"]["event"]["enum"]
    assert set(s["properties"]["cause"]["enum"]) == set(judge.CAUSES)
    assert set(s["properties"]["detection"]["enum"]) == set(judge.DETECTIONS)


def test_at_least_six_cases_with_required_kinds():
    names = {load(p)["name"] for p in CASES}
    assert len(names) >= 6
    assert {"german_correction", "new_requirement", "clarification", "empty_turn",
            "evidence_not_substring"} <= names


# --- per-case: mocked API --------------------------------------------------------------------------

@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_case_api(path):
    c = load(path)
    cand = {"prompt": judge.build_prompt_text(c["prompt"]), "summary": c["summary"], "prompt_kind": "prompt"}
    client = FakeClient([c["model_output"]] if c["model_output"] else [])
    lab, _ = judge.label_one(cand, "claude-haiku-5-5", ["api"], client)
    assert_schema(lab)
    for k, v in c["expect"].items():
        assert lab[k] == v, (k, lab)
    if lab["event"] != "abstain" or lab["evidence"]:
        assert lab["evidence"] in cand["prompt"]
    assert len(client.calls) == (0 if c.get("expect_no_call") else 1)
    if client.calls:
        kw = client.calls[0]
        assert "temperature" not in kw  # Haiku 5.5 rejects non-default sampling
        assert kw["tool_choice"] == {"type": "tool", "name": judge.TOOL_NAME}
        assert kw["tools"][0]["strict"] is True


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_case_regex_standin(path):
    c = load(path)
    cand = {"prompt": judge.build_prompt_text(c["prompt"]), "summary": c["summary"], "prompt_kind": "prompt"}
    lab, used = judge.label_one(cand, "claude-haiku-5-5", ["regex"])
    assert_schema(lab)
    assert lab["event"] == c["expect_regex"]
    if lab["event"] != "abstain":
        assert lab["source"] == "regex" and lab["confidence"] == pytest.approx(0.3)
        assert used == "regex"
        assert lab["evidence"] in cand["prompt"]


def test_validate_downgrades_and_clamps():
    p = "Nein, falsch"
    assert judge.validate({"event": "correction", "evidence": "nope"}, p, "api")["event"] == "abstain"
    assert judge.validate("garbage", p, "api")["event"] == "abstain"
    lab = judge.validate({"event": "correction", "cause": "x", "detection": "y", "evidence": "Nein",
                          "confidence": 7, "defect_caught": 1, "resolved": "yes"}, p, "api")
    assert lab["event"] == "correction" and lab["cause"] == "unknown" and lab["detection"] == "unknown"
    assert lab["confidence"] == 1.0 and lab["resolved"] is None and lab["defect_caught"] is True


def test_stop_reason_max_tokens_abstains():
    client = FakeClient([{}], stop_reason="max_tokens")
    lab, _ = judge.label_one({"prompt": "no, wrong", "summary": "s"}, "claude-haiku-5-5", ["api"], client)
    assert lab["event"] == "abstain"


def test_opus_55_uses_auto_tool_choice():
    client = FakeClient([{"event": "abstain"}])
    judge.call_api("s", "p", "claude-opus-5-5", client)
    assert client.calls[0]["tool_choice"] == {"type": "auto"}
    assert "temperature" not in client.calls[0]


def test_api_failure_falls_back_to_regex():
    class Boom:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("down")

    cand = {"prompt": "Nein, das ist falsch", "summary": "s"}
    lab, used = judge.label_one(cand, "claude-haiku-5-5", ["api", "regex"], Boom())
    assert lab["source"] == "regex" and used == "regex" and lab["event"] == "correction"
    assert cand["errors"] == ["api:RuntimeError"]


# --- claude -p fallback ------------------------------------------------------------------------------

def test_claude_p_fallback(monkeypatch, scorer_home):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        out = {"type": "result", "result": "", "structured_output": {
            "event": "correction", "cause": "instruction_violated", "detection": "human_review",
            "evidence": "das ist falsch", "confidence": 0.8, "defect_caught": True, "resolved": None}}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

    monkeypatch.setattr(judge.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    lab, used = judge.label_one({"prompt": "Nein, das ist falsch", "summary": "s"}, "claude-haiku-5-5", ["claude-p"])
    assert lab["event"] == "correction" and lab["source"] == "claude-p" and used == "claude-haiku-5-5"
    assert "--no-session-persistence" in seen["cmd"] and "--json-schema" in seen["cmd"]
    assert seen["kw"]["timeout"] == 60
    cwd = seen["kw"]["cwd"]
    assert JUDGE_SLUG_MARKER in judge._slug(cwd)
    assert cwd.startswith(str(scorer_home))


def test_claude_p_result_text_and_garbage(monkeypatch):
    outs = iter([json.dumps({"result": 'here: {"event": "clarification", "cause": "unknown", "detection": '
                                       '"unknown", "evidence": "I mean", "confidence": 0.6, '
                                       '"defect_caught": false, "resolved": null}'}),
                 "not json at all"])
    monkeypatch.setattr(judge.shutil, "which", lambda name: "/x/claude")
    monkeypatch.setattr(judge.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=next(outs), stderr=""))
    lab, _ = judge.label_one({"prompt": "I mean prod", "summary": "s"}, "m", ["claude-p"])
    assert lab["event"] == "clarification"
    lab, _ = judge.label_one({"prompt": "I mean prod", "summary": "s"}, "m", ["claude-p"])
    assert lab["event"] == "abstain"  # parse failure, no further backend


def test_backend_chain(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(judge.shutil, "which", lambda name: None)
    assert judge.backend_chain() == ["regex"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(judge.shutil, "which", lambda name: "/x/claude")
    assert judge.backend_chain() == ["api", "claude-p", "regex"]


# --- DB: candidates, cache, metrics ------------------------------------------------------------------

def make_db(tmp_path, prompts):
    conn = connect(tmp_path / "t.db")
    conn.execute("INSERT INTO ingest_run(id, at, parser_version) VALUES (1, ?, '1')", (DAY_TS,))
    conn.execute("INSERT INTO session(id, agent, cwd) VALUES ('s1', 'claude', '/tmp/x')")
    for i, p in enumerate(prompts):
        ts = f"2026-10-13T1{i}:00:00+00:00"
        conn.execute("INSERT INTO turn(id, session_id, idx, prompt_at, prompt_text, prompt_kind, end_source,"
                     " agent_finished_at) VALUES (?,?,?,?,?,?,?,?)",
                     (i + 1, "s1", i, ts, p, "prompt", "stop_hook", ts))
        conn.execute("INSERT INTO tool_call(turn_id, tool_use_id, name, kind, bash_intent, is_error, has_result,"
                     " args_summary) VALUES (?,?,?,?,?,?,?,?)",
                     (i + 1, f"tu{i}", "Bash", "bash", "test", int(i == 0), 1, "pytest -q"))
    conn.commit()
    return conn


def test_candidates_need_previous_turn(tmp_path):
    conn = make_db(tmp_path, ["fix the bug", "Nein, das ist falsch", "now add docs"])
    cands = judge.candidates(conn)
    assert sorted(c["turn_id"] for c in cands) == [2, 3]
    c2 = next(c for c in cands if c["turn_id"] == 2)
    assert "ERROR" in c2["summary"] and "intent=test" in c2["summary"]
    assert len(c2["summary"]) <= judge.SUMMARY_CHARS
    assert judge.candidates(conn, limit=1)[0]["turn_id"] == 3
    assert judge.candidates(conn, day="2026-10-01") == []


def test_run_judge_cache_and_metrics(tmp_path):
    conn = make_db(tmp_path, ["fix the bug", "Nein, das ist falsch, warum hast du das gemacht?", "now add docs"])
    outs = [{"event": "correction", "cause": "instruction_violated", "detection": "human_review",
             "evidence": "das ist falsch", "confidence": 0.9, "defect_caught": True, "resolved": None},
            {"event": "new_requirement", "cause": "requirement_changed", "detection": "unknown",
             "evidence": "now add docs", "confidence": 0.8, "defect_caught": False, "resolved": None}]

    class OrderedClient(FakeClient):
        def create(self, **kw):  # thread pool: answer by prompt, not call order
            self.calls.append(kw)
            text = kw["messages"][0]["content"]
            out = outs[0] if "falsch" in text else outs[1]
            return SimpleNamespace(stop_reason="tool_use",
                                   content=[SimpleNamespace(type="tool_use", name=judge.TOOL_NAME, input=out)])

    client = OrderedClient([])
    st = judge.run_judge(conn, 1, client=client, chain=["api"])
    # 3 calls: the session's first prompt is labelled too (positive episodes), but stays out of C
    assert len(client.calls) == 3 and st["called"] == 3 and st["cached"] == 0
    assert st["n"] == 2 and st["first_turns"] == 1 and st["first_labelled"] == 1
    assert st["dist"] == {"correction": 1, "new_requirement": 1}
    assert conn.execute("SELECT count(*) FROM judge_label").fetchone()[0] == 3
    assert "FIRST prompt" in client.calls[0]["messages"][0]["content"] or any(
        "FIRST prompt" in c["messages"][0]["content"] for c in client.calls)

    st2 = judge.run_judge(conn, 1, client=client, chain=["api"])
    assert len(client.calls) == 3, "cache hit must not call the API again"
    assert st2["cached"] == 3

    rows = dict(conn.execute("SELECT metric, value FROM metric_value WHERE run_id=1 AND scope_type='day'"
                             " AND (metric LIKE 'C:%' OR metric LIKE 'C^_%' ESCAPE '^')").fetchall())
    day = judge.candidates(conn)[0]["day"]
    assert rows["C:event:correction"] == 1 and rows["C:event:new_requirement"] == 1
    assert rows["C:cause:instruction_violated"] == 1 and rows["C:detection:human_review"] == 1
    assert rows["C_source:api"] == 2 and "C_abstain" not in rows
    # idempotent: second run replaced, not duplicated
    n = conn.execute("SELECT count(*) FROM metric_value WHERE metric='C:event:correction' AND scope_id=?",
                     (day,)).fetchone()[0]
    assert n == 1
    note = conn.execute("SELECT e.note, e.turn_id FROM metric_evidence e JOIN metric_value v ON v.id=e.metric_value_id"
                        " WHERE v.metric='C:event:correction'").fetchone()
    assert note[1] == 2 and note[0].startswith("correction/instruction_violated/human_review: ")
    assert len(note[0]) <= 120


def test_run_judge_regex_when_no_key_no_cli(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(judge.shutil, "which", lambda name: None)
    conn = make_db(tmp_path, ["fix it", "no, that's wrong", ""])
    st = judge.run_judge(conn, 1)
    assert st["chain"] == ["regex"]
    assert st["sources"] == {"regex": 1, "rule": 1}
    assert st["dist"] == {"correction": 1, "abstain": 1}
    m = dict(conn.execute("SELECT metric, value FROM metric_value WHERE metric GLOB 'C*'").fetchall())
    assert m["C_source:regex"] == 1 and m["C_abstain"] == 1


def test_regex_cache_does_not_block_api(tmp_path):
    conn = make_db(tmp_path, ["fix it", "no, that's wrong"])
    judge.run_judge(conn, 1, chain=["regex"])
    client = FakeClient([{"event": "correction", "cause": "unknown", "detection": "human_review",
                          "evidence": "that's wrong", "confidence": 0.7, "defect_caught": True, "resolved": None}])
    st = judge.run_judge(conn, 1, client=client, chain=["api"])
    assert len(client.calls) == 2 and st["sources"] == {"api": 1}   # + the first prompt


def test_hash_depends_on_model_prompt_summary_version():
    h = judge.turn_hash("m", "p", "s")
    assert h != judge.turn_hash("m2", "p", "s") != judge.turn_hash("m", "p2", "s")
    assert h != judge.turn_hash("m", "p", "s2")
    assert len(h) == 40


def test_cli_judge_prints_counts_only(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from scorer.cli import cli
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(judge.shutil, "which", lambda name: None)
    secret = "Nein, das ist falsch SECRET_SOURCE_CODE_" + "x" * 200
    conn = make_db(tmp_path, ["fix it", secret])
    conn.close()
    r = CliRunner().invoke(cli, ["judge", "--day", "all", "--db", str(tmp_path / "t.db")])
    assert r.exit_code == 0, r.output
    assert "SECRET" not in r.output and "correction=1" in r.output and "regex" in r.output


def test_cli_report_survives_judge_failure(tmp_path, monkeypatch):
    import sys
    import types
    from click.testing import CliRunner
    from scorer.cli import cli
    make_db(tmp_path, ["a", "b"]).close()

    def boom(*a, **k):
        raise RuntimeError("judge down")

    monkeypatch.setattr(judge, "run_judge", boom)
    fake = types.ModuleType("scorer.report")
    fake.render = lambda conn, day, days: "# report ok"
    monkeypatch.setitem(sys.modules, "scorer.report", fake)
    r = CliRunner().invoke(cli, ["report", "--day", "2026-10-13", "--db", str(tmp_path / "t.db")])
    assert r.exit_code == 0 and "# report ok" in r.output


# --- report-path bounds ------------------------------------------------------------------------------

def _ok_client():
    return FakeClient([{"event": "new_requirement", "cause": "unknown", "detection": "unknown",
                        "evidence": "p", "confidence": 0.5, "defect_caught": False, "resolved": None}] * 50)


def test_max_calls_caps_uncached_and_reports_coverage(tmp_path):
    conn = make_db(tmp_path, ["p0", "p1", "p2", "p3", "p4"])
    client = _ok_client()
    st = judge.run_judge(conn, 1, client=client, chain=["api"], max_calls=2)
    assert len(client.calls) == 2 and st["labelled"] == 2 and st["unlabelled"] == 2 and st["n"] == 4
    # most recent first
    labelled = {c["turn_id"] for c in judge.candidates(conn)
                if judge._cache_get(conn, judge.turn_hash("claude-haiku-5-5", c["prompt"], c["summary"]))}
    assert labelled == {4, 5}
    v, n = conn.execute("SELECT value, n FROM metric_value WHERE metric='C_labelled'").fetchone()
    assert (v, n) == (2, 4)
    st = judge.run_judge(conn, 1, client=client, chain=["api"], max_calls=2)  # next run fills the rest
    assert st["labelled"] == 4 and st["cached"] == 2 and len(client.calls) == 4


def test_timeout_leaves_turns_unlabelled_and_returns_fast(tmp_path):
    import threading
    import time
    release = threading.Event()

    class Slow(FakeClient):
        def create(self, **kw):
            release.wait(5)
            return super().create(**kw)

    conn = make_db(tmp_path, ["p0", "p1", "p2"])
    t0 = time.monotonic()
    st = judge.run_judge(conn, 1, client=Slow([]), chain=["api"], timeout=0.2, workers=8)
    assert time.monotonic() - t0 < 2
    release.set()
    assert st["labelled"] == 0 and st["timed_out"] == 3   # incl. the first prompt
    assert conn.execute("SELECT count(*) FROM judge_label").fetchone()[0] == 0
    assert tuple(conn.execute("SELECT value, n FROM metric_value WHERE metric='C_labelled'").fetchone()) == (0, 2)


def test_cli_report_prints_judge_coverage(tmp_path, monkeypatch):
    import sys
    import types
    from click.testing import CliRunner
    from scorer.cli import cli
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(judge.shutil, "which", lambda name: None)
    make_db(tmp_path, ["a", "no, wrong", "c"]).close()
    fake = types.ModuleType("scorer.report")
    fake.render = lambda conn, day, days: "# report"
    monkeypatch.setitem(sys.modules, "scorer.report", fake)
    r = CliRunner().invoke(cli, ["report", "--day", "2026-10-13", "--db", str(tmp_path / "t.db")])
    assert r.exit_code == 0 and "judge: 2 of 2 turns labelled" in r.output
    r = CliRunner().invoke(cli, ["report", "--day", "2026-10-13", "--no-judge", "--db", str(tmp_path / "t.db")])
    assert r.exit_code == 0 and "judge:" not in r.output


def test_cli_report_days_budget(tmp_path, monkeypatch):
    import sys
    import types
    from click.testing import CliRunner
    from scorer.cli import cli
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(judge.shutil, "which", lambda name: None)
    monkeypatch.setattr(judge, "REPORT_MAX_CALLS", 1)
    make_db(tmp_path, ["a", "no, wrong", "c"]).close()
    seen = []
    real = judge.run_judge

    def spy(conn, run_id, **kw):
        seen.append(kw)
        return real(conn, run_id, **kw)

    monkeypatch.setattr(judge, "run_judge", spy)
    fake = types.ModuleType("scorer.report")
    fake.render = lambda conn, day, days: "# report"
    monkeypatch.setitem(sys.modules, "scorer.report", fake)
    r = CliRunner().invoke(cli, ["report", "--days", "2", "--db", str(tmp_path / "t.db")])
    assert r.exit_code == 0 and "turns labelled" in r.output
    assert len(seen) == 2 and all(k["workers"] == judge.REPORT_WORKERS for k in seen)
    assert seen[0]["max_calls"] == 1 and all(0 <= k["timeout"] <= judge.REPORT_TIMEOUT_S for k in seen)
