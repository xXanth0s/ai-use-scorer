"""Analyst agent: tools, privacy, citations, no-key path, mocked tool loop, optional live smoke."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scorer import agent
from scorer.db import connect
from scorer.ingest import write_bundle
from scorer.model import ApiCall, Bundle, FileEdit, Session, ToolCall, Turn, iso, local_day

SECRET = "SECRET-" + "x" * 300          # long private prompt text; must never surface unclipped
BASE = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
DAY = local_day(iso(BASE))


def ts(minutes: float) -> str:
    return iso(BASE + timedelta(minutes=minutes))


@pytest.fixture(autouse=True)
def no_real_key(monkeypatch):
    """No test may hit Infisical or the network unless it opts in (live smoke)."""
    agent._CACHE.clear()
    monkeypatch.setattr(agent, "_infisical_key", lambda: None)
    yield
    agent._CACHE.clear()


@pytest.fixture
def dbpath(tmp_path):
    p = tmp_path / "t.db"
    conn = connect(p)
    b = Bundle(
        session=Session(id="s1", agent="claude", cwd="/home/u/proj", repo="/home/u/proj", started_at=ts(0)),
        turns=[
            Turn(idx=0, prompt_at=ts(0), prompt_text=SECRET, agent_started_at=ts(0.1), agent_finished_at=ts(5),
                 end_source="stop_hook"),
            Turn(idx=1, prompt_at=ts(6), prompt_text="run the tests please", agent_started_at=ts(6.1),
                 agent_finished_at=ts(7), end_source="interrupt", interrupted=True),
        ],
        api_calls=[ApiCall(turn_idx=0, request_id="r1", model="claude-opus-5-5", input_tok=1000, cached_tok=9000,
                           output_tok=500, context_window=200000, cost_usd=0.01, at=ts(1))],
        tool_calls=[
            ToolCall(turn_idx=0, tool_use_id="tu1", name="Edit", kind="edit", args_summary="/home/u/proj/" + "a" * 200,
                     started_at=ts(1), has_result=True),
            ToolCall(turn_idx=0, tool_use_id="tu2", name="Bash", kind="bash", bash_intent="test",
                     args_summary="pytest " + "-k " * 100, started_at=ts(2), has_result=True, is_error=True),
        ],
        file_edits=[FileEdit(tool_use_id="tu1", path="/home/u/proj/src/" + "b" * 150 + ".py", lines_added=3,
                             lines_removed=1)],
    )
    write_bundle(conn, b)
    b2 = Bundle(session=Session(id="s2", agent="codex", repo="/home/u/other"),
                turns=[Turn(idx=0, prompt_at=ts(2), prompt_text="hi", agent_finished_at=ts(3))])
    write_bundle(conn, b2)
    t0, t1 = [r[0] for r in conn.execute("SELECT id FROM turn WHERE session_id='s1' ORDER BY idx")]
    for run in (1, 2):
        conn.execute("INSERT INTO ingest_run(id, at, parser_version) VALUES (?,?,?)", (run, ts(0), "1"))
        for metric, val, unit in (("V", 0.25 * run, "share"), ("gap_bucket:lt30s", 2, "gaps"),
                                  ("gap_bucket:break", 1, "gaps"), ("H", 1, "edits(partial)")):
            mid = conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id,"
                               " value, unit, n) VALUES (?,?,?,?,?,?,?,?)",
                               (run, metric, "v1", "day", DAY, val, unit, 4)).lastrowid
            if metric == "V":
                conn.execute("INSERT INTO metric_evidence(metric_value_id, turn_id, note) VALUES (?,?,?)",
                             (mid, t0, f"unverified: a.py run{run}"))
                conn.execute("INSERT INTO metric_evidence(metric_value_id, turn_id, note) VALUES (?,?,?)",
                             (mid, t1, "verified_fail"))
    conn.commit()
    conn.close()
    return str(p)


@pytest.fixture
def conn(dbpath):
    c = sqlite3.connect(dbpath)
    yield c
    c.close()


def turn_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM turn WHERE session_id='s1' ORDER BY idx")]


def walk_strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield from walk_strings(k)
            yield from walk_strings(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from walk_strings(v)


# --- query_db --------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "INSERT INTO session(id) VALUES ('x')",
    "UPDATE turn SET prompt_text='y'",
    "DELETE FROM turn",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "SELECT 1; DROP TABLE turn",
    "SELECT 1; SELECT 2",
    "WITH a AS (SELECT 1) INSERT INTO session(id) SELECT * FROM a",
    "PRAGMA table_info(turn)",
    "CREATE TABLE z(a)",
    "SELECT load_extension('x')",
    "",
])
def test_query_db_rejects_writes(conn, sql):
    r = agent.run_tool(conn, "query_db", {"sql": sql})
    assert r["ok"] is False and r["error"]
    assert conn.execute("SELECT count(*) FROM session").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM turn").fetchone()[0] == 3


def test_query_db_semicolon_inside_string_ok(conn):
    r = agent.run_tool(conn, "query_db", {"sql": "SELECT 'a;b' AS s; "})
    assert r["ok"], r
    assert r["data"]["rows"] == [["a;b"]]


def test_query_db_row_cap(conn):
    sql = "WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM c WHERE i < 500) SELECT i FROM c"
    r = agent.run_tool(conn, "query_db", {"sql": sql, "limit": 10_000})
    assert r["ok"], r
    assert len(r["data"]["rows"]) == 200 and r["data"]["truncated"] is True
    r = agent.run_tool(conn, "query_db", {"sql": sql, "limit": 5})
    assert len(r["data"]["rows"]) == 5 and r["data"]["truncated"] is True
    r = agent.run_tool(conn, "query_db", {"sql": "SELECT 1"})
    assert r["data"]["truncated"] is False


def test_query_db_clips_private_columns(conn):
    for sql in ("SELECT prompt_text FROM turn", "SELECT prompt_text AS x FROM turn",
                "SELECT substr(prompt_text, 70) AS y FROM turn", "SELECT * FROM turn",
                "SELECT args_summary FROM tool_call", "SELECT path, manifest_dir FROM file_edit",
                "SELECT t.prompt_text FROM turn t JOIN session s ON s.id = t.session_id"):
        r = agent.run_tool(conn, "query_db", {"sql": sql})
        assert r["ok"], (sql, r)
        for s in walk_strings(r["data"]["rows"]):
            assert len(s) <= 80, (sql, s)
        assert SECRET not in str(r)
    r = agent.run_tool(conn, "query_db", {"sql": "SELECT prompt_text FROM turn WHERE prompt_text LIKE 'SECRET%'"})
    assert r["data"]["rows"][0][0].startswith("SECRET-") and len(r["data"]["rows"][0][0]) == 80


def test_query_db_raw_main_table_denied(conn):
    for sql in ("SELECT prompt_text FROM main.turn", "SELECT substr(prompt_text, 81, 80) FROM main.turn",
                "SELECT args_summary FROM main.tool_call"):
        r = agent.run_tool(conn, "query_db", {"sql": sql})
        assert r["ok"] is False and ("not authorized" in r["error"] or "prohibited" in r["error"]), (sql, r)
    # non-sensitive columns of main tables remain readable
    r = agent.run_tool(conn, "query_db", {"sql": "SELECT count(*) AS n FROM main.turn"})
    assert r["ok"] and r["data"]["rows"] == [[3]]


def test_query_db_timeout(conn, monkeypatch):
    monkeypatch.setattr(agent, "QUERY_TIMEOUT_S", 0.2)
    sql = "WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM c) SELECT count(*) FROM c"
    t = time.monotonic()
    r = agent.run_tool(conn, "query_db", {"sql": sql})
    assert r["ok"] is False and "cancelled" in r["error"]
    assert time.monotonic() - t < 3


def test_query_db_turn_citations(conn):
    r = agent.run_tool(conn, "query_db", {"sql": "SELECT id AS turn_id, prompt_kind FROM turn ORDER BY id"}, n0=4)
    assert [c["n"] for c in r["citations"]] == [4, 5, 6]
    assert r["data"]["citations"] == {str(c["turn_id"]): c["n"] for c in r["citations"]}


# --- other tools -------------------------------------------------------------------------------------

def test_get_turn(conn):
    t0, t1 = turn_ids(conn)
    r = agent.run_tool(conn, "get_turn", {"turn_id": t0})
    assert r["ok"], r
    d = r["data"]
    assert d["session"] == {"id": "s1", "agent": "claude", "repo": "proj", "cwd": "proj"}
    assert len(d["prompt"]) <= 80 and d["prompt"].startswith("SECRET-")
    assert d["duration_s"] == 300.0 and d["interrupted"] is False
    assert [c["name"] for c in d["tool_calls"]] == ["Edit", "Bash"]
    assert all(len(c["args"]) <= 80 for c in d["tool_calls"])
    assert d["tool_calls"][1]["is_error"] is True and d["tool_calls"][1]["bash_intent"] == "test"
    assert d["api_calls"]["count"] == 1 and d["api_calls"]["context_pct_max"] == 5.0
    assert d["file_edits"][0]["lines"] == "+3/-1" and "/" not in d["file_edits"][0]["file"]
    # evidence only from the latest run (run 2)
    assert [e["note"] for e in d["metric_evidence"]] == ["unverified: a.py run2"]
    assert r["citations"] == [{"n": 1, "turn_id": t0, "session_id": "s1", "label": r["citations"][0]["label"]}]
    assert d["n"] == 1
    assert SECRET not in str(r)
    r1 = agent.run_tool(conn, "get_turn", {"turn_id": t1})
    assert r1["data"]["interrupted"] is True and r1["data"]["end_source"] == "interrupt"


def test_get_turn_errors(conn):
    assert agent.run_tool(conn, "get_turn", {"turn_id": 99999})["ok"] is False
    assert agent.run_tool(conn, "get_turn", {"turn_id": "abc"})["ok"] is False
    assert agent.run_tool(conn, "nope", {})["ok"] is False


def test_get_metric_latest_run_and_citations(conn):
    t0, t1 = turn_ids(conn)
    r = agent.run_tool(conn, "get_metric", {"metric": "V", "day": DAY}, n0=3)
    assert r["ok"], r
    d = r["data"]
    assert d["run_id"] == 2 and d["values"][0]["value"] == 0.5
    assert [e["turn_id"] for e in d["evidence"]] == [t0, t1]
    assert [c["n"] for c in r["citations"]] == [3, 4]
    assert [e["n"] for e in d["evidence"]] == [3, 4]
    # default day comes from the dashboard day
    r = agent.run_tool(conn, "get_metric", {"metric": "V"}, day=DAY)
    assert r["ok"] and r["data"]["values"][0]["scope_id"] == DAY


def test_get_metric_prefix_missing_and_partial(conn):
    r = agent.run_tool(conn, "get_metric", {"metric": "gap_bucket", "day": DAY})
    assert r["ok"] and {v["metric"] for v in r["data"]["values"]} == {"gap_bucket:lt30s", "gap_bucket:break"}
    r = agent.run_tool(conn, "get_metric", {"metric": "C", "day": DAY})
    assert r["ok"] is False and "judge" in r["error"] and "V" in r["available"]
    r = agent.run_tool(conn, "get_metric", {"metric": "V", "day": "2001-01-01"})
    assert r["ok"] is False and r["days_with_values"] == [DAY]
    r = agent.run_tool(conn, "get_metric", {"metric": "H", "day": DAY})
    assert "partial" in r["data"]["note"]


def test_list_sessions(conn):
    r = agent.run_tool(conn, "list_sessions", {"day": DAY})
    assert r["ok"]
    ss = {s["id"]: s for s in r["data"]["sessions"]}
    assert set(ss) == {"s1", "s2"}
    assert ss["s1"]["turns"] == 2 and ss["s1"]["active_s"] == 360.0 and ss["s1"]["repo"] == "proj"
    assert ss["s2"]["agent"] == "codex"
    assert agent.run_tool(conn, "list_sessions", {"day": "2001-01-01"})["data"]["sessions"] == []


# --- key ---------------------------------------------------------------------------------------------

def test_no_key_path(conn, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(agent, "_make_client", lambda key: pytest.fail("must not build a client"))
    assert agent.has_key() is False
    evs = list(agent.stream_chat(conn, [{"role": "user", "content": "hi"}], DAY))
    assert evs == [{"type": "error", "message": agent.NO_KEY_MSG}, {"type": "done", "model": None}]
    assert agent.narrative(conn, DAY, {"V": 0.5}) is None


def test_infisical_failure_is_cached(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.undo()   # restore the real _infisical_key, then make the subprocess fail
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent._CACHE.clear()
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise FileNotFoundError("infisical")
    monkeypatch.setattr(agent.subprocess, "run", boom)
    assert agent.has_key() is False and agent.has_key() is False
    assert len(calls) == 1


def test_model_attr():
    assert agent.MODEL == "claude-opus-5-5"
    assert agent._models() == ("claude-opus-5-5", "claude-sonnet-5-5")


def test_env_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert agent.has_key() is True


# --- mocked loop -------------------------------------------------------------------------------------

def tool_use(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def text(t):
    return SimpleNamespace(type="text", text=t)


class FakeClient:
    """script: list of (exception | (deltas, content, stop_reason)). Records every request."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = self

    @contextmanager
    def stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        deltas, content, stop = item
        final = SimpleNamespace(content=content, stop_reason=stop, model=kw["model"])
        yield _Stream([text(d) for d in deltas], final)


class _Stream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final


def test_loop_tool_use_then_end_turn(conn):
    t0, _ = turn_ids(conn)
    fc = FakeClient([
        (["Let me check."], [text("Let me check."), tool_use("tu_a", "get_metric", {"metric": "V", "day": DAY})],
         "tool_use"),
        ([], [tool_use("tu_b", "get_turn", {"turn_id": t0}),
              tool_use("tu_c", "get_metric", {"metric": "V", "day": DAY})], "tool_use"),
        (["V is 50% [1]", " — see [3]."], [text("V is 50% [1] — see [3].")], "end_turn"),
    ])
    evs = list(agent.stream_chat(conn, [{"role": "user", "content": "why is V low?"}], DAY, client=fc))
    kinds = [e["type"] for e in evs]
    assert kinds == ["text-delta", "tool-start", "tool-result", "tool-start", "tool-result", "tool-start",
                     "tool-result", "text-delta", "text-delta", "done"]
    assert evs[-1] == {"type": "done", "model": "claude-opus-5-5"}
    r1 = evs[2]
    assert r1["ok"] and [c["n"] for c in r1["citations"]] == [1, 2]
    r2 = evs[4]
    assert [c["n"] for c in r2["citations"]] == [3] and r2["citations"][0]["turn_id"] == t0
    dup = evs[6]
    assert dup["ok"] is False and dup["summary"] == "duplicate call skipped"
    # request shape: cached system, tools, both tool results in one user message, day context
    req = fc.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "<definitions>" in req["system"][0]["text"] and "CREATE TABLE" in req["system"][0]["text"]
    assert {t["name"] for t in req["tools"]} == {"query_db", "get_turn", "get_metric", "list_sessions"}
    assert DAY in req["messages"][-1]["content"]
    third = fc.requests[2]["messages"]
    assert third[-1]["role"] == "user" and [b["tool_use_id"] for b in third[-1]["content"]] == ["tu_b", "tu_c"]
    assert third[-1]["content"][1]["is_error"] is True
    for m in third:
        assert SECRET not in str(m)


def test_loop_fallback_model(conn):
    import anthropic
    import httpx2
    err = anthropic.APIStatusError("overloaded", response=httpx2.Response(529, request=httpx2.Request(
        "POST", "https://api.anthropic.com/v1/messages")), body=None)
    fc = FakeClient([err, (["hello"], [text("hello")], "end_turn")])
    evs = list(agent.stream_chat(conn, [{"role": "user", "content": "hi"}], DAY, client=fc))
    assert [r["model"] for r in fc.requests] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert evs == [{"type": "text-delta", "delta": "hello"}, {"type": "done", "model": "claude-sonnet-5-5"}]


def test_loop_non_fallback_error(conn):
    import anthropic
    import httpx2
    err = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=httpx2.Request(
        "POST", "https://api.anthropic.com/v1/messages")), body=None)
    fc = FakeClient([err])
    evs = list(agent.stream_chat(conn, [{"role": "user", "content": "hi"}], DAY, client=fc))
    assert [e["type"] for e in evs] == ["error", "done"] and len(fc.requests) == 1


def test_loop_step_cap(conn, monkeypatch):
    monkeypatch.setattr(agent, "MAX_STEPS", 3)
    script = [([], [tool_use(f"t{i}", "list_sessions", {"day": f"2026-10-0{i}"})], "tool_use") for i in range(3)]
    fc = FakeClient(script)
    evs = list(agent._loop(fc, agent._Src(conn), [{"role": "user", "content": "x"}], DAY, max_steps=3))
    assert len(fc.requests) == 3
    assert fc.requests[-1]["tool_choice"] == {"type": "none"}
    assert evs[-2]["type"] == "error" and "step cap" in evs[-2]["message"]
    assert sum(e["type"] == "tool-start" for e in evs) == 2


def test_narrative_maps_citations(conn):
    t0, t1 = turn_ids(conn)
    fc = FakeClient([
        (["Checking."], [text("Checking."), tool_use("a", "get_metric", {"metric": "V", "day": DAY})], "tool_use"),
        (["- **Verdict:** V 50% [1][2]."], [text("- **Verdict:** V 50% [1][2].")], "end_turn"),
    ])
    out = agent.narrative(conn, DAY, {"V": 0.5}, client=fc)
    assert out == f"- **Verdict:** V 50% [turn:{t0}][turn:{t1}]."


def test_messages_normalised(conn):
    fc = FakeClient([(["ok"], [text("ok")], "end_turn")])
    msgs = [{"role": "assistant", "content": "welcome"}, {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"}, {"role": "system", "content": "x"},
            {"role": "user", "content": "q2"}]
    list(agent.stream_chat(conn, msgs, None, client=fc))
    sent = fc.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[-1]["content"].startswith("q2")


# --- live smoke (manual) -----------------------------------------------------------------------------

@pytest.mark.skipif(not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("SCORER_LIVE") == "1"),
                    reason="live smoke: set ANTHROPIC_API_KEY and SCORER_LIVE=1")
def test_live_smoke(conn):
    t = time.monotonic()
    evs = list(agent.stream_chat(conn, [{"role": "user", "content": "Why is my verification rate what it is on "
                                         "the day on screen? Show the evidence turns."}], DAY))
    dt = time.monotonic() - t
    text_out = "".join(e["delta"] for e in evs if e["type"] == "text-delta")
    tools = [e["name"] for e in evs if e["type"] == "tool-start"]
    errors = [e["message"] for e in evs if e["type"] == "error"]
    print(f"\nLIVE model={evs[-1]['model']} tools={tools} latency={dt:.1f}s errors={errors}\n{text_out}")
    assert not errors and evs[-1]["type"] == "done" and text_out
    assert tools
    assert SECRET not in text_out
