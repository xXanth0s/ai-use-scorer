"""Dashboard JSON endpoints against a small fixture DB (never ~/.scorer)."""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from scorer import db as dbm
from scorer import serve
from scorer.ingest import write_bundle
from scorer.model import ApiCall, Bundle, Event, FileEdit, Session, ToolCall, Turn, iso

DAY = "2026-10-13"
SENTINEL = "ZZSECRETSENTINELZZ"
LONG_PROMPT = "please refactor the parser " + "x" * 160 + SENTINEL
TEMP_CWD = "/private/var/folders/" + "z" * 90 + "/T/scorer-e2e.AbC123"


def T(h: int, m: int, s: int = 0) -> str:
    """Local wall-clock time on DAY -> ISO UTC (matches the parser contract)."""
    return iso(datetime(2026, 10, 13, h, m, s).astimezone())


def build_fixture(path: str) -> dict:
    """3 sessions (2 claude, 1 codex): overlap, a >15 min break, an interrupt, one unverified edit turn."""
    conn = dbm.connect(path)
    run_id = conn.execute("INSERT INTO ingest_run(at, parser_version) VALUES (?, '1')", (T(23, 0),)).lastrowid
    a = Bundle(
        session=Session(id="aaaaaaaa-1111-4000-8000-000000000001", agent="claude",
                        cwd="/Users/x/Projects/ai-use-scorer", repo="/Users/x/Projects/ai-use-scorer",
                        branch="main", started_at=T(9, 0)),
        turns=[
            Turn(idx=0, prompt_at=T(9, 0), prompt_text=LONG_PROMPT, agent_started_at=T(9, 0, 5),
                 agent_finished_at=T(9, 25), end_source="stop_hook"),
            Turn(idx=1, prompt_at=T(9, 27), prompt_text="add tests for the codex parser",
                 agent_finished_at=T(9, 50), end_source="stop_hook"),
            Turn(idx=2, prompt_at=T(10, 30), prompt_text="now wire the dashboard",
                 agent_finished_at=T(10, 41), end_source="interrupt", interrupted=True),
            Turn(idx=3, prompt_at=T(10, 42), prompt_text="stop, use stdlib http.server instead",
                 agent_finished_at=T(11, 5), end_source="stop_hook"),
        ],
        api_calls=[ApiCall(turn_idx=0, request_id="req-a0", model="claude-opus-5-5", input_tok=1200,
                           cached_tok=90000, output_tok=3000, cost_usd=0.42, at=T(9, 10)),
                   ApiCall(turn_idx=3, request_id="req-a3", model="claude-sonnet-5", input_tok=800,
                           cached_tok=40000, output_tok=2000, cost_usd=None, at=T(10, 50))],
        tool_calls=[
            ToolCall(turn_idx=0, tool_use_id="tu-a0-edit", name="Edit", kind="edit",
                     args_summary="/Users/x/Projects/ai-use-scorer/src/scorer/parse_claude.py" + "y" * 80,
                     started_at=T(9, 5)),
            ToolCall(turn_idx=0, tool_use_id="tu-a0-test", name="Bash", kind="bash", bash_intent="test",
                     initiator="human", initiator_rule="3:prompt:/test/ " + LONG_PROMPT,
                     args_summary="uv run pytest -q " + LONG_PROMPT, started_at=T(9, 20), has_result=True),
            ToolCall(turn_idx=1, tool_use_id="tu-a1-edit", name="Write", kind="edit",
                     args_summary="tests/test_codex.py", started_at=T(9, 30)),
        ],
        file_edits=[FileEdit(tool_use_id="tu-a0-edit", path="src/scorer/parse_claude.py", lines_added=40)],
        events=[Event(kind="compaction", at=T(10, 55), turn_idx=3, data={"secret": LONG_PROMPT})]
        + [Event(kind="tooling", at=T(9, 1), turn_idx=0, data={"category": "skill", "name": "design-consultation",
                                                                "initiator": "human"}) for _ in range(2)]
        + [Event(kind="tooling", at=T(9, 28), turn_idx=1, data={"category": "skill", "name": "review",
                                                                 "initiator": "human"})]
        + [Event(kind="tooling", at=T(9, 29), turn_idx=1, data={"category": "mcp", "name": "mcp__context7__query-docs",
                                                                 "initiator": "agent"}) for _ in range(4)],
    )
    b = Bundle(
        session=Session(id="bbbbbbbb-2222-4000-8000-000000000002", agent="codex",
                        cwd="/Users/x/Projects/api-gateway", repo="/Users/x/Projects/api-gateway"),
        turns=[Turn(idx=0, prompt_at=T(9, 10), prompt_text="fix the flaky pagination test",
                    agent_finished_at=T(9, 40), end_source="task_complete"),
               Turn(idx=1, prompt_at=T(9, 45), prompt_text="ok ship it", agent_finished_at=None)],
        tool_calls=[ToolCall(turn_idx=0, tool_use_id="tu-b0", name="shell", kind="bash", bash_intent="build",
                             args_summary="cargo check", started_at=T(9, 20), has_result=True, is_error=True),
                    ToolCall(turn_idx=0, tool_use_id="tu-b-spawn", name="spawn_agent", kind="subagent",
                             initiator="agent", started_at=T(9, 15))],
        events=[Event(kind="subagent", at=T(9, 15, 5), turn_idx=0,
                      data={"kind": "started", "agent_thread_id": "child-thread-1", "agent_path": "/root/x",
                            "tool_use_id": "tu-b-spawn"}),
                Event(kind="subagent", at=T(9, 18, 5), turn_idx=0,
                      data={"kind": "completed", "agent_thread_id": "child-thread-1", "agent_path": "/root/x"})],
    )
    c = Bundle(
        session=Session(id="cccccccc-3333-4000-8000-000000000003", agent="claude", cwd="/Users/x/dotfiles"),
        turns=[Turn(idx=0, prompt_at=T(14, 0), prompt_text="tidy zshrc", agent_finished_at=T(14, 6))],
        tool_calls=[ToolCall(turn_idx=0, tool_use_id="tu-c-agent", name="Agent", kind="subagent", initiator="human",
                             started_at=T(14, 1)),
                    ToolCall(turn_idx=0, tool_use_id="tu-c-side", name="Read", kind="read", sidechain_id="sub-claude-1",
                             args_summary="/Users/x/dotfiles/.zshrc", started_at=T(14, 2), duration_ms=60000)],
        api_calls=[ApiCall(turn_idx=0, request_id="req-c-side", model="claude-sonnet-5-5", input_tok=1000,
                           output_tok=500, cost_usd=0.05, at=T(14, 4), sidechain_id="sub-claude-1")],
        events=[Event(kind="subagent", at=T(14, 1, 30), turn_idx=0,
                      data={"agent_id": "sub-claude-1", "model": "claude-sonnet-5-5", "agent_type": "Explore"})],
    )
    tmp = Bundle(   # a scorer e2e scratch session: hidden from /api/day unless include_temp=1
        session=Session(id="dddddddd-4444-4000-8000-000000000004", agent="claude", cwd=TEMP_CWD),
        turns=[Turn(idx=0, prompt_at=T(12, 0), prompt_text="e2e " + LONG_PROMPT, agent_finished_at=T(12, 5))],
        tool_calls=[ToolCall(turn_idx=0, tool_use_id="tu-d-edit", name="Edit", kind="edit", args_summary="x.py",
                             started_at=T(12, 1))],
    )
    for bun in (a, b, c, tmp):
        write_bundle(conn, bun)
    tid = {r["idx"]: r["id"] for r in conn.execute("SELECT id, idx FROM turn WHERE session_id=?", (a.session.id,))}
    tc = conn.execute("SELECT id FROM tool_call WHERE tool_use_id='tu-a1-edit'").fetchone()[0]

    def mv(metric, value, unit=None, n=None, scope="day", sid=DAY, ev=()):
        i = conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id, value,"
                         " unit, n) VALUES (?,?,?,?,?,?,?,?)", (run_id, metric, "v1", scope, sid, value, unit, n)
                         ).lastrowid
        for turn_id, tool_id, note in ev:
            conn.execute("INSERT INTO metric_evidence(metric_value_id, turn_id, tool_call_id, note) VALUES (?,?,?,?)",
                         (i, turn_id, tool_id, note))

    mv("V", 0.5, "share", 2, ev=[(tid[1], tc, "edit batch without test " + LONG_PROMPT)])
    mv("H", 0, "count", 0)
    mv("B_min", 14, "min", 1, ev=[(tid[1], None, "codex idle while you worked in claude")])
    mv("active_s", 7200, "s")
    mv("overlap_s", 1800, "s")
    mv("gap_bucket:<30s", 2, "count")
    mv("model_mix:claude-opus-5-5", 0.5, "share")
    mv("cost_usd", 0.42, "usd")
    mv("some_new_metric", 3, None)
    mv("subagents", 2, "count")
    mv("tooling:skill:human", 3, "count", ev=[(tid[0], None, "skill:design-consultation:human x2"),
                                               (tid[1], None, "skill:review:human x1")])
    mv("tooling:mcp:agent", 4, "count", ev=[(tid[3], None, "mcp:mcp__context7__query-docs:agent x4")])
    mv("tooling:skill", 3, "count")
    mv("tooling_human_share", 0.43, "share")
    mv("V", 0.8, "share", 5, sid="2026-10-12")
    conn.commit()
    conn.close()
    return {"turn_ids": tid}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scorer")
    mp = pytest.MonkeyPatch()
    mp.setenv("SCORER_HOME", str(tmp))
    mp.delenv("SCORER_MASTRA_URL", raising=False)
    path = str(tmp / "scorer.db")
    info = build_fixture(path)
    srv = serve.make_server(path, 0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", info
    srv.shutdown()
    srv.server_close()
    mp.undo()


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def post(base, path, body: bytes):
    req = urllib.request.Request(base + path, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# generated sentences may exceed 80 chars; everything else is data and must be clipped
LONG_OK = {"where", "change", "text", "prevent", "judge_doubt", "markdown", "summary"}


def assert_private(raw: bytes):
    assert SENTINEL.encode() not in raw

    def walk(o, key=None):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str) and key not in LONG_OK:
            assert len(o) <= 80, (key, len(o))
    walk(json.loads(raw))


def test_days(server):
    base, _ = server
    code, raw, h = get(base, "/api/days")
    assert code == 200 and h["Cache-Control"] == "no-store"
    d = json.loads(raw)
    assert d["run_id"] == 1
    day = next(x for x in d["days"] if x["day"] == DAY)
    assert day == {"day": DAY, "turns": 8, "sessions": 4}     # /api/days counts temp sessions too


def test_day_shape(server):
    base, info = server
    code, raw, _ = get(base, f"/api/day/{DAY}")
    assert code == 200
    assert_private(raw)
    d = json.loads(raw)
    assert [s["agent"] for s in d["sessions"]] == ["claude", "codex", "claude"]
    a = d["sessions"][0]
    assert a["repo"] == "ai-use-scorer" and len(a["turns"]) == 4
    assert a["interrupts"] and a["interrupts"][0]["turn_id"] == info["turn_ids"][2]
    codex = d["sessions"][1]
    assert codex["turns"][1]["open"] is True              # missing finish -> open-ended, not dropped
    assert d["overlap"], "claude 9:00-9:25 vs codex 9:10-9:40 overlap"
    assert any(g["bucket"] == "break" for g in d["gaps"])  # 9:50 -> 10:30
    assert d["computed"]["sessions"] == 3 and d["computed"]["interrupts"] == 1
    s = d["scored"]
    assert s["V"]["value"] == 0.5 and s["V"]["evidence"][0]["turn_id"] == info["turn_ids"][1]
    assert s["B_min"]["value"] == 14 and s["V_final"] is None and s["C"] == []
    diag = {x["metric"]: x for x in d["diagnostics"]}
    assert "some_new_metric" in diag and diag["gap_bucket:<30s"]["key"] == "<30s"
    lev = d["leverage"]
    assert lev["subagents"] == 2 and lev["compactions"] is None
    tl = lev["tooling"]
    assert tl["human_share"] == 0.43 and tl["categories"]["skill"]["human"] == 3
    assert tl["categories"]["mcp"]["total"] == 4 and tl["categories"]["plugin"]["total"] is None
    assert tl["items"][0] == {"category": "mcp", "name": "mcp__context7__query-docs", "initiator": "agent", "count": 4}
    assert {"category": "skill", "name": "design-consultation", "initiator": "human", "count": 2} in tl["items"]
    vd = d["verdict"]   # report.facts(): blocked 14 min > 2 unverified batches x 5 min
    assert vd["basis"] == "B_min" and vd["category"] == "blocked" and vd["minutes"] == 14 and "14 min" in vd["where"]


def test_empty_day(server):
    base, _ = server
    code, raw, _ = get(base, "/api/day/2020-01-01")
    d = json.loads(raw)
    assert code == 200 and d["sessions"] == [] and d["scored"]["V"] is None
    assert get(base, "/api/day/bogus")[0] == 400


def test_turn(server):
    base, info = server
    code, raw, _ = get(base, f"/api/turn/{info['turn_ids'][0]}")
    assert code == 200
    assert_private(raw)
    t = json.loads(raw)
    assert len(t["prompt"]) <= 80 and t["prompt"].endswith("…")
    assert t["tier"] == "verified_pass"
    assert [c["bash_intent"] for c in t["tools"]] == [None, "test"]
    assert t["api"]["calls"] == 1 and t["api"]["cost_usd"] == 0.42
    t1 = json.loads(get(base, f"/api/turn/{info['turn_ids'][1]}")[1])
    assert t1["tier"] == "none"
    t3 = json.loads(get(base, f"/api/turn/{info['turn_ids'][3]}")[1])
    assert t3["events"] == [{"kind": "compaction", "at": T(10, 55)}] and t3["api"]["unpriced"] == 1
    assert get(base, "/api/turn/999999")[0] == 404


def test_trend(server):
    base, _ = server
    code, raw, _ = get(base, f"/api/trend?end={DAY}&days=7")
    d = json.loads(raw)
    assert code == 200 and len(d["days"]) == 7 and d["days"][-1] == DAY
    assert d["series"]["V"][-2:] == [0.8, 0.5]
    assert d["series"]["active_h"][-1] == 2.0 and d["series"]["C"][-1] is None


def test_all_responses_private(server):
    base, info = server
    for p in ["/api/days", f"/api/day/{DAY}", f"/api/trend?end={DAY}"] + \
             [f"/api/turn/{i}" for i in info["turn_ids"].values()]:
        code, raw, _ = get(base, p)
        assert code == 200, p
        assert_private(raw)


class FakeAgent:
    MODEL = "claude-opus-5-5"

    @staticmethod
    def has_key():
        return True

    @staticmethod
    def stream_chat(conn, messages, day):
        n = conn.execute("SELECT count(*) FROM turn").fetchone()[0]
        yield {"type": "tool-start", "id": "t1", "name": "query_db", "input": {"sql": "SELECT " + LONG_PROMPT}}
        yield {"type": "tool-result", "id": "t1", "name": "query_db", "ok": True, "summary": f"{n} turns " + LONG_PROMPT,
               "citations": [{"n": 1, "turn_id": 2, "session_id": "aaaa", "label": LONG_PROMPT}]}
        yield {"type": "text-delta", "delta": f"day={day} msgs={len(messages)}"}
        yield {"type": "done"}


class BrokenAgent(FakeAgent):
    @staticmethod
    def stream_chat(conn, messages, day):
        yield {"type": "text-delta", "delta": "partial"}
        raise RuntimeError(LONG_PROMPT)


def sse_events(raw: bytes) -> list[dict]:
    return [json.loads(chunk[6:]) for chunk in raw.decode().split("\n\n") if chunk.startswith("data: ")]


def test_chat_unavailable(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(serve, "_load_agent", lambda: None)
    code, raw = post(base, "/api/chat", json.dumps({"messages": [{"role": "user", "content": "hi"}],
                                                    "day": DAY}).encode())
    assert code == 501 and json.loads(raw)["error"] == "not_implemented"
    st = json.loads(get(base, "/api/chat/status")[1])
    assert st["available"] is False and st["model"] is None
    assert post(base, "/api/chat", b"not json")[0] == 400
    assert post(base, "/api/nope", b"{}")[0] == 404


def test_chat_sse(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(serve, "_load_agent", lambda: FakeAgent)
    assert json.loads(get(base, "/api/chat/status")[1]) == {"available": True, "model": "claude-opus-5-5",
                                                            "backend": "python", "reason": None}
    req = urllib.request.Request(base + "/api/chat", method="POST", data=json.dumps(
        {"messages": [{"role": "user", "content": "why is V low?"}, {"role": "system", "content": "x"}],
         "day": DAY}).encode())
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        raw = r.read()
    assert SENTINEL.encode() not in raw
    evs = sse_events(raw)
    assert [e["type"] for e in evs] == ["tool-start", "tool-result", "text-delta", "done"]
    assert evs[1]["citations"][0]["turn_id"] == 2 and len(evs[1]["citations"][0]["label"]) <= 80
    assert evs[1]["summary"].startswith("8 turns")
    assert evs[2]["delta"] == f"day={DAY} msgs=1"             # non user/assistant roles dropped
    assert evs[3] == {"type": "done", "backend": "python"}


def test_chat_agent_crash(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(serve, "_load_agent", lambda: BrokenAgent)
    req = urllib.request.Request(base + "/api/chat", method="POST", data=b'{"messages": []}')
    with urllib.request.urlopen(req, timeout=5) as r:
        raw = r.read()
    assert SENTINEL.encode() not in raw
    assert [e["type"] for e in sse_events(raw)] == ["text-delta", "error", "done"]


def test_static_and_404(server):
    base, _ = server
    code, raw, h = get(base, "/")
    assert code == 200 and h["Content-Type"].startswith("text/html")
    assert get(base, "/fonts/HankenGrotesk-var-latin.woff2")[2]["Content-Type"] == "font/woff2"
    assert get(base, "/../serve.py")[0] == 404
    assert get(base, "/api/unknown")[0] == 404


# --- C counted by cause only (trend) ------------------------------------------------------------

def test_c_total_counts_causes_only():
    assert serve.c_total({}) is None
    assert serve.c_total({"C": 4, "C:cause:info_missing": 9}) == 4
    rows = {"C:cause:info_missing": 2, "C:cause:instruction_violated": 1, "C:event:correction": 3,
            "C:detection:human_review": 3, "C:defect_caught": 1, "C:resolved": 3}
    assert serve.c_total(rows) == 3
    assert serve.c_total({"C:info_missing": 2, "C:event:correction": 2, "C:resolved": 2}) == 2  # legacy C:<cause>
    assert serve.c_total({"C:event:correction": 0}) == 0.0                                    # judged, no cause rows


def test_trend_c_by_cause(tmp_path):
    path = str(tmp_path / "c.db")
    build_fixture(path)
    conn = dbm.connect(path)
    rid = dbm.latest_run_id(conn)
    for metric, v in [("C:cause:info_missing", 2), ("C:cause:requirement_changed", 1), ("C:event:correction", 3),
                      ("C:detection:human_review", 3), ("C:defect_caught", 1), ("C:resolved", 3)]:
        conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id, value)"
                     " VALUES (?,?,?,?,?,?)", (rid, metric, "v1", "day", DAY, v))
    conn.commit()
    assert serve.trend_payload(conn, DAY, 3)["series"]["C"] == [None, None, 3]
    conn.close()


# --- tool + system-prompt endpoints (Mastra sidecar data access) --------------------------------

def test_tools_endpoint(server):
    base, info = server
    t0, t1 = info["turn_ids"][0], info["turn_ids"][1]

    def tool(name, body):
        code, raw = post(base, f"/api/tools/{name}", json.dumps(body).encode())
        assert code == 200, (name, raw[:200])
        assert_private(raw)
        return json.loads(raw)

    r = tool("query_db", {"input": {"sql": "SELECT id AS turn_id, prompt_text FROM turn ORDER BY id LIMIT 3"},
                          "n0": 4})
    assert r["ok"] and [c["n"] for c in r["citations"]] == [4, 5, 6]
    assert all(len(row[1] or "") <= 80 for row in r["data"]["rows"])
    r = tool("get_turn", {"input": {"turn_id": t0}, "n0": 9})
    assert r["ok"] and r["citations"][0] == {**r["citations"][0], "n": 9, "turn_id": t0}
    assert r["data"]["prompt"].endswith("…") and r["data"]["n"] == 9
    r = tool("get_metric", {"input": {"metric": "V"}, "day": DAY})
    assert r["ok"] and r["data"]["values"][0]["value"] == 0.5 and r["citations"][0]["turn_id"] == t1
    r = tool("list_sessions", {"input": {}, "day": DAY})
    assert r["ok"] and len(r["data"]["sessions"]) == 4      # the agent tool sees temp sessions too
    r = tool("query_db", {"input": {"sql": "DELETE FROM turn"}})
    assert r["ok"] is False and "SELECT" in r["error"]
    assert post(base, "/api/tools/drop_tables", b"{}")[0] == 404
    assert post(base, "/api/tools/get_turn", b'{"input": [1]}')[0] == 400
    assert post(base, "/api/tools/get_turn", b"nope")[0] == 400


def test_system_prompt_endpoint(server):
    from scorer import agent
    base, _ = server
    code, raw, _ = get(base, "/api/agent/system-prompt")
    d = json.loads(raw)
    assert code == 200 and d["system_prompt"] == agent._SYSTEM and d["max_steps"] == agent.MAX_STEPS
    assert "<grounding>" in d["system_prompt"] and len(d["today"]) == 10


# --- /api/chat proxy to the optional Mastra sidecar -------------------------------------------

class _Upstream:
    """Fake agent-mastra sidecar: /health + /chat SSE (with over-long fields serve.py must clip)."""

    def __init__(self, health_delay: float = 0.0):
        import http.server
        import time
        seen = self.seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                time.sleep(health_delay)
                body = b'{"ok": true, "model": "claude-sonnet-5-5", "fallback": "claude-opus-5-5"}'
                self.send_response(200 if self.path == "/health" else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                seen["body"] = json.loads(self.rfile.read(n))
                seen["scorer_url"] = self.headers.get("X-Scorer-Url")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                evs = [{"type": "subagent-start", "id": "d1", "name": "turnInvestigator", "task": LONG_PROMPT},
                       {"type": "tool-start", "id": "t1", "name": "get_turn", "input": {"turn_id": 2},
                        "agent": "turnInvestigator"},
                       {"type": "tool-start", "id": "t2", "name": "query_db",
                        "input": {"sql": "SELECT count(*) FROM turn WHERE " + " OR ".join(["idx = 1"] * 60)}},
                       {"type": "tool-result", "id": "t1", "name": "get_turn", "ok": True, "summary": "turn 2",
                        "citations": [{"n": 1, "turn_id": 2, "session_id": "a", "label": LONG_PROMPT}]},
                       {"type": "subagent-result", "id": "d1", "name": "turnInvestigator", "summary": LONG_PROMPT},
                       {"type": "text-delta", "delta": "answer [1]"},
                       {"type": "suggestions", "items": [{"label": "Compare with yesterday",
                                                           "prompt": "Compare today's V with yesterday's."}]},
                       {"type": "done", "model": "claude-opus-5-5", "backend": "mastra"}]
                for ev in evs:
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                    self.wfile.flush()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def chat(base) -> list[dict]:
    req = urllib.request.Request(base + "/api/chat", method="POST", data=json.dumps(
        {"messages": [{"role": "user", "content": "why is V low?"}], "day": DAY}).encode())
    with urllib.request.urlopen(req, timeout=5) as r:
        raw = r.read()
    assert SENTINEL.encode() not in raw
    return sse_events(raw)


def test_chat_proxy_on(server, monkeypatch):
    base, _ = server
    up = _Upstream()
    try:
        monkeypatch.setattr(serve, "_load_agent", lambda: FakeAgent)
        monkeypatch.setenv("SCORER_MASTRA_URL", up.url)
        assert json.loads(get(base, "/api/chat/status")[1])["backend"] == "mastra"
        evs = chat(base)
        assert [e["type"] for e in evs] == ["subagent-start", "tool-start", "tool-start", "tool-result",
                                           "subagent-result", "text-delta", "suggestions", "done"]
        assert evs[1]["agent"] == "turnInvestigator" and evs[1]["subagent_id"] == "d1"
        q = evs[2]
        assert len(q["input"]["sql"]) == 80 and len(q["sql_full"]) == 500 and q["sql_full"].startswith("SELECT count(*)")
        assert evs[6] == {"type": "suggestions", "items": [{"label": "Compare with yesterday",
                                                           "prompt": "Compare today's V with yesterday's."}]}
        st = json.loads(get(base, "/api/chat/status")[1])
        assert st["model"] == "claude-sonnet-5-5" and st["fallback"] == "claude-opus-5-5" and st["available"] is True
        assert evs[-1] == {"type": "done", "model": "claude-opus-5-5", "backend": "mastra"}
        assert len(evs[0]["task"]) <= 80 and len(evs[4]["summary"]) <= 80
        assert len(evs[3]["citations"][0]["label"]) <= 80
        assert up.seen["body"] == {"messages": [{"role": "user", "content": "why is V low?"}], "day": DAY}
        assert up.seen["scorer_url"] == base
    finally:
        up.close()


def test_chat_proxy_off(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(serve, "_load_agent", lambda: FakeAgent)
    monkeypatch.setenv("SCORER_MASTRA_URL", "http://127.0.0.1:9")          # nothing listening
    assert json.loads(get(base, "/api/chat/status")[1])["backend"] == "python"
    assert chat(base)[-1] == {"type": "done", "backend": "python"}
    slow = _Upstream(health_delay=1.0)                                     # /health slower than 300 ms
    try:
        monkeypatch.setenv("SCORER_MASTRA_URL", slow.url)
        assert chat(base)[-1] == {"type": "done", "backend": "python"}
        assert "body" not in slow.seen
    finally:
        slow.close()


# --- /api/day additions: cwd, hours, per-session numbers, temp filter, leverage ------------------

def test_day_additions(server):
    base, info = server
    code, raw, _ = get(base, f"/api/day/{DAY}")
    assert code == 200
    assert_private(raw)
    d = json.loads(raw)
    assert d["include_temp"] is False and d["temp_hidden"] == 1
    assert all(not s["temp"] for s in d["sessions"])
    a, b, c = d["sessions"]
    assert a["cwd"] == "/Users/x/Projects/ai-use-scorer" and a["parent_id"] is None
    assert a["active_s"] == (25 + 23 + 11 + 23) * 60
    assert a["cost_usd"] >= 0.42 and a["tokens"]["in"] == 2000 and a["tokens"]["cached"] == 130000
    assert {m["model"] for m in a["models"]} == {"claude-opus-5-5", "claude-sonnet-5"}
    assert a["verification"] == {"batches": 2, "verified_pass": 1, "verified_fail": 0, "inspected": 0, "none": 1}
    assert b["verification"]["batches"] == 0 and b["subagents"]["count"] == 1
    assert c["subagents"] == {"count": 1, "tokens": 1500, "cost_usd": 0.05}
    assert c["cost_usd"] == 0.05 and c["tokens"] == {"in": 1000, "cached": 0, "out": 500}
    # hours: local hours from the first active hour; waiting = gaps < 15 min not covered by any agent
    hrs = d["hours"]
    assert hrs[0]["start"] == T(9, 0) and len(hrs) == 6
    assert (hrs[0]["active_s"], hrs[0]["waiting_s"]) == (3000, 0)
    assert (hrs[1]["active_s"], hrs[1]["waiting_s"]) == (1740, 60)
    assert hrs[3]["active_s"] == 0 and hrs[5]["active_s"] == 360         # 12:00 temp session hidden
    # leverage
    lev = d["leverage"]
    assert lev["subagent_types"] == [
        {"name": "Explore", "runs": 1, "initiators": {"human": 1, "agent": 0}, "median_s": 150,
         "model": "claude-sonnet-5-5", "tokens": 1500, "cost_usd": 0.05},
        {"name": "codex subagent", "runs": 1, "initiators": {"human": 0, "agent": 1}, "median_s": 180,
         "model": None, "tokens": 0, "cost_usd": None}]
    items = lev["tooling"]["items"]
    assert items[0] == {"category": "mcp", "name": "mcp__context7__query-docs", "initiator": "agent", "count": 4}
    assert {"category": "skill", "name": "review", "initiator": "human", "count": 1} in items


def test_day_include_temp(server):
    base, _ = server
    code, raw, _ = get(base, f"/api/day/{DAY}?include_temp=1")
    assert code == 200
    assert_private(raw)
    d = json.loads(raw)
    assert d["include_temp"] is True and d["temp_hidden"] == 0 and len(d["sessions"]) == 4
    t = next(s for s in d["sessions"] if s["temp"])
    assert len(t["cwd"]) == 80 and t["cwd"].startswith("…") and t["cwd"].endswith("/T/scorer-e2e.AbC123")
    assert t["verification"]["batches"] == 1 and d["computed"]["sessions"] == 4
    assert any(h["active_s"] for h in d["hours"] if h["start"] == T(12, 0))


def test_is_temp_and_lclip():
    assert serve.is_temp("/private/var/folders/bf/x/T/foo") and serve.is_temp("/tmp/x")
    assert serve.is_temp("/Users/x/scorer-capture-repo.oWx") and serve.is_temp("/Users/x/scorer-e2e.1/sub")
    assert not serve.is_temp("/Users/x/Projects/ai-use-scorer") and not serve.is_temp(None)
    assert serve.lclip("a" * 100 + "/end") == "…" + ("a" * 100 + "/end")[-79:]
    assert serve.lclip("/short") == "/short" and serve.lclip(None) == ""


# --- C from the newest run that has judge rows ----------------------------------------------------

def test_c_from_newest_judged_run(tmp_path):
    path = str(tmp_path / "c2.db")
    build_fixture(path)
    conn = dbm.connect(path)
    for metric, v in [("C:cause:info_missing", 2), ("C:event:correction", 2)]:
        i = conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id, value)"
                         " VALUES (1,?,?,?,?,?)", (metric, "v1", "day", DAY, v)).lastrowid
        conn.execute("INSERT INTO metric_evidence(metric_value_id, turn_id, note) VALUES (?, 1, 'correction: x')", (i,))
    conn.execute("INSERT INTO ingest_run(id, at, parser_version) VALUES (2, ?, '1')", (T(23, 30),))   # re-ingest
    conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id, value, unit, n)"
                 " SELECT 2, metric, definition_version, scope_type, scope_id, value, unit, n FROM metric_value"
                 " WHERE run_id=1 AND metric NOT LIKE 'C%'")
    conn.commit()
    d = serve.day_payload(conn, DAY)
    assert [c["metric"] for c in d["scored"]["C"]] == ["C:cause:info_missing", "C:event:correction"]
    assert d["sessions"][0]["corrections"] == 1
    tr = serve.trend_payload(conn, DAY, 2)["series"]
    assert tr["C"] == [None, 2] and tr["C:cause:info_missing"] == [None, 2]
    rep = serve.report_payload(conn, DAY)
    assert rep["corrections"] and rep["corrections"][0]["cause"] == "info_missing" and rep["judged"]
    conn.close()


# --- trend additions --------------------------------------------------------------------------------

def test_trend_series(server):
    base, _ = server
    code, raw, _ = get(base, f"/api/trend?end={DAY}&days=3")
    assert code == 200
    assert_private(raw)
    t = json.loads(raw)
    s = t["series"]
    assert s["cost_usd"] == [None, None, 0.42]
    assert s["V"] == [None, 0.8, 0.5] and s["V_n"] == [None, 5, 2]
    assert s["subagents"][-1] == 2 and s["subagent_tokens"][-1] == 1500 and s["subagent_cost_usd"][-1] == 0.05
    assert s["gap_bucket:<30s"] == [None, 0, 2] and s["tokens_in"][-1] is None
    assert s["overlap_h"][-1] == 0.5 and "claude-opus-5-5" not in t["models"]    # only cost_usd:<model> keys
    for k in s:
        assert len(s[k]) == 3


# --- /api/report ---------------------------------------------------------------------------------------

def test_report(server):
    base, info = server
    code, raw, _ = get(base, f"/api/report/{DAY}")
    assert code == 200
    assert_private(raw)
    f = json.loads(raw)
    assert f["verdict"]["text"] and f["verdict"]["metric"] in ("B_min", "V", "H", "interrupts")
    assert set(f["tomorrow"]) >= {"change", "evidence_turns"}
    assert not any(x["session_id"].startswith("dddd") for x in f["unverified_now"])     # temp hidden
    inc = json.loads(get(base, f"/api/report/{DAY}?include_temp=1")[1])
    assert any(x["session_id"].startswith("dddd") for x in inc["unverified_now"])
    u = next(x for x in f["unverified_now"] if x["session_id"].startswith("aaaa"))
    assert u["turn_id"] == info["turn_ids"][1] and u["tier"] == "none" and "test_codex.py" in u["files"]
    assert set(u) >= {"session_id", "turn_id", "tier", "files", "command"}
    p = f["pattern"]
    assert p["kind"] == "verified_session" and p["verified"] == 1 and p["batches"] == 2
    assert set(p) >= {"session", "verified", "batches", "command", "human_initiated", "turns"}
    assert len(p["command"]) <= 80 and "corrections" in f and "worst_window" in f
    code, raw, _ = get(base, "/api/report/2020-01-01")
    assert code == 404 and b"unknown" not in raw and b"not_implemented" not in raw   # UI keeps the endpoint
    assert get(base, "/api/report/bogus")[0] == 400


def test_clip_strings():
    o = serve.clip_strings({"text": "t " * 150, "note": "n" * 200, "xs": ["p" * 90], "k": {"prevent": "q" * 300}})
    assert len(o["text"]) > 80 and len(o["note"]) == 80 and len(o["xs"][0]) == 80 and len(o["k"]["prevent"]) == 300


# --- /api/narrative --------------------------------------------------------------------------------------

class NarrAgent(FakeAgent):
    calls = 0
    key = True

    @staticmethod
    def has_key():
        return NarrAgent.key

    @staticmethod
    def narrative(conn, day, facts):
        NarrAgent.calls += 1
        assert facts["day"] == day
        t = {r["idx"]: r["id"] for r in conn.execute("SELECT id, idx FROM turn WHERE session_id LIKE 'aaaa%'")}
        return f"- **Verdict:** 14 min blocked [turn:{t[1]}].\n- **Tomorrow:** verify [turn:{t[0]}][turn:{t[1]}]."


def test_narrative(server, monkeypatch):
    base, info = server
    monkeypatch.setattr(serve, "_load_agent", lambda: NarrAgent)
    NarrAgent.calls, NarrAgent.key = 0, True
    code, raw, _ = get(base, f"/api/narrative?day={DAY}")
    assert code == 200
    assert_private(raw)
    n = json.loads(raw)
    t0, t1 = info["turn_ids"][0], info["turn_ids"][1]
    assert n["markdown"] == "- **Verdict:** 14 min blocked [1].\n- **Tomorrow:** verify [2][1]."
    assert [(c["n"], c["turn_id"]) for c in n["citations"]] == [(1, t1), (2, t0)]
    assert n["citations"][1]["session_id"].startswith("aaaa") and n["citations"][1]["label"].endswith("…")
    assert n["cached"] is False and n["model"] == "claude-opus-5-5" and n["generated_at"]
    n2 = json.loads(get(base, f"/api/narrative?day={DAY}")[1])
    assert n2["cached"] is True and n2["markdown"] == n["markdown"] and NarrAgent.calls == 1


def test_narrative_no_key_or_none(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(serve, "_load_agent", lambda: NarrAgent)
    NarrAgent.key, NarrAgent.calls = False, 0
    assert json.loads(get(base, "/api/narrative?day=2026-10-12")[1]) == {"markdown": None, "citations": [], "cached": False}
    assert NarrAgent.calls == 0
    NarrAgent.key = True
    monkeypatch.setattr(NarrAgent, "narrative", staticmethod(lambda conn, day, facts: None))
    assert json.loads(get(base, "/api/narrative?day=2026-10-12")[1])["markdown"] is None   # failures are not cached
    monkeypatch.setattr(serve, "_load_agent", lambda: None)
    assert get(base, "/api/narrative?day=2026-10-12")[0] == 501


# --- /api/summary --------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, seen):
        self.seen = seen
        self.messages = self

    def create(self, **kw):
        from types import SimpleNamespace
        self.seen.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Refactored the parser; tests verified.")])


def test_summary(server, monkeypatch):
    base, info = server
    seen: list = []
    monkeypatch.setattr(serve, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(serve, "_summary_client", lambda key: FakeClient(seen))
    sid = "aaaaaaaa-1111-4000-8000-000000000001"
    code, raw, _ = get(base, f"/api/summary?session_id={sid}")
    assert code == 200
    assert_private(raw)
    assert json.loads(raw) == {"summary": "Refactored the parser; tests verified.", "cached": False}
    assert json.loads(get(base, f"/api/summary?session_id={sid}")[1])["cached"] is True and len(seen) == 1
    kw = seen[0]
    assert kw["model"] == "claude-haiku-5-5"
    text = kw["messages"][0]["content"]
    assert SENTINEL not in text and "x" * 80 not in text and "/Users/x" not in text
    assert "parse_claude.py" in text and "verified_pass" in text and "edit x1" in text
    code, raw, _ = get(base, f"/api/summary?session_id={sid}&turn_id={info['turn_ids'][1]}")
    assert code == 200 and len(seen) == 2 and "One turn" in seen[1]["messages"][0]["content"]
    assert get(base, f"/api/summary?session_id={sid}&turn_id=999999")[0] == 404
    assert get(base, "/api/summary?session_id=nope")[0] == 404
    assert get(base, "/api/summary")[0] == 400


def test_summary_no_key_and_failure(server, monkeypatch):
    base, _ = server
    sid = "cccccccc-3333-4000-8000-000000000003"
    monkeypatch.setattr(serve, "_api_key", lambda: None)
    code, raw, _ = get(base, f"/api/summary?session_id={sid}")
    assert code == 404 and b"unknown" not in raw
    monkeypatch.setattr(serve, "_api_key", lambda: "sk-test")

    def boom(key):
        raise TimeoutError(LONG_PROMPT)
    monkeypatch.setattr(serve, "_summary_client", boom)
    code, raw, _ = get(base, f"/api/summary?session_id={sid}")
    assert code == 502 and SENTINEL.encode() not in raw


# --- SSE event mapping (subagents, suggestions) ------------------------------------------------------

def test_event_mapper_subagents():
    m = serve.EventMapper()
    s = m({"type": "subagent-start", "id": "d1", "name": "turnInvestigator", "task": LONG_PROMPT})
    assert s == {"type": "subagent-start", "id": "d1", "name": "turnInvestigator", "task": s["task"]}
    assert len(s["task"]) <= 80
    assert m({"type": "tool-start", "id": "t1", "name": "get_turn", "input": {}})["subagent_id"] == "d1"
    assert m({"type": "tool-start", "id": "t2", "name": "get_turn", "input": {}, "parentId": "zz"})["subagent_id"] == "zz"
    r = m({"type": "subagent-result", "id": "d1", "name": "turnInvestigator", "summary": "found it"})
    assert r == {"type": "subagent-result", "id": "d1", "name": "turnInvestigator", "ok": True, "summary": "found it",
                 "citations": []}
    assert "subagent_id" not in m({"type": "tool-start", "id": "t3", "name": "query_db", "input": {}})
    f = m({"type": "subagent-result", "id": "d2", "agent": "sqlAnalyst", "summary": "failed: boom",
           "citations": [{"n": 1, "turn_id": 2, "session_id": "a", "label": LONG_PROMPT}]})
    assert f["ok"] is False and f["name"] == "sqlAnalyst" and len(f["citations"][0]["label"]) <= 80
    a = serve.sanitize_event({"type": "subagent-start", "toolCallId": "x", "agent": "a", "parentId": "p",
                              "input": {"task": "look"}})
    assert (a["id"], a["name"], a["task"], a["parent_id"]) == ("x", "a", "look", "p")


def test_sanitize_suggestions():
    ev = serve.sanitize_event({"type": "suggestions", "items": [
        {"label": "L" * 60, "prompt": "P" * 300, "extra": SENTINEL}, {"label": "b", "prompt": "why?"},
        {"prompt": "only a prompt"}, {"label": "no prompt"}, "junk", {"label": "c", "prompt": "c"},
        {"label": "d", "prompt": "d"}, {"label": "e", "prompt": "e"}]})
    assert ev["type"] == "suggestions" and len(ev["items"]) == 4
    assert len(ev["items"][0]["label"]) == 40 and len(ev["items"][0]["prompt"]) == 200
    assert ev["items"][1] == {"label": "b", "prompt": "why?"} and ev["items"][2]["label"] == "only a prompt"
    assert SENTINEL not in json.dumps(ev)
    assert serve.sanitize_event({"type": "suggestions"}) == {"type": "suggestions", "items": []}


# --- every GET endpoint routes with a query string attached -----------------------------------------

def test_query_strings_everywhere(server, monkeypatch):
    base, info = server
    assert get(base, "/api/agent/system-prompt?x=1")[0] == 200     # real agent module
    assert post(base, "/api/tools/get_turn?x=1", json.dumps({"input": {"turn_id": info["turn_ids"][0]}}).encode())[0] == 200
    assert post(base, "/api/chat?x=1", b"nope")[0] == 400        # routed to chat (bad body), not 404
    monkeypatch.setattr(serve, "_load_agent", lambda: NarrAgent)
    monkeypatch.setattr(serve, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(serve, "_summary_client", lambda key: FakeClient([]))
    NarrAgent.key = True
    sid = "bbbbbbbb-2222-4000-8000-000000000002"
    for p in ["/api/days?x=1", f"/api/day/{DAY}?include_temp=0&x=1", f"/api/report/{DAY}?x=1",
              f"/api/turn/{info['turn_ids'][0]}?x=1", f"/api/trend?end={DAY}&days=3&x=1", "/api/chat/status?x=1",
              f"/api/narrative?day={DAY}&x=1", f"/api/summary?session_id={sid}&x=1",
              "/?day=2026-10-13", "/app.js?v=2"]:
        code, raw, _ = get(base, p)
        assert code == 200, (p, raw[:120])
        assert b"unknown endpoint" not in raw


def test_day_cost_matches_sessions(tmp_path):
    """Σ per-session cost == the day's cost (what compute_all stores as cost_usd), within a cent."""
    from scorer import metrics
    path = str(tmp_path / "cost.db")
    build_fixture(path)
    conn = dbm.connect(path)
    d = serve.day_payload(conn, DAY, include_temp=True)
    day_cost = sum(r["cost"] for r in metrics.api_rows(conn, DAY) if r["cost"] is not None)
    assert day_cost > 0.42
    assert abs(sum(s["cost_usd"] or 0 for s in d["sessions"]) - day_cost) <= 0.01
    assert sum(s["tokens"]["in"] for s in d["sessions"]) == 3000
    conn.close()


# --- /api/actions ------------------------------------------------------------------------------------------

def test_actions(server, monkeypatch):
    from scorer import judge
    base, info = server
    monkeypatch.delattr(judge, "action_items", raising=False)
    code, raw, _ = get(base, f"/api/actions?day={DAY}")
    assert code == 200 and json.loads(raw) == {"day": DAY, "available": False, "items": []}
    t1 = info["turn_ids"][1]

    def items(conn, day):
        assert day == DAY
        return [{"text": "Name the test command in every edit prompt " + "t" * 200, "category": "verification",
                 "count": 3, "turn_ids": [t1, "7", "x"], "example_quote": LONG_PROMPT,
                 "claude_md_line": "- Always run `uv run pytest -q` after edits.", "uncalibrated": True, "source": "judge"},
                {"text": "Answer blocked sessions first", "category": "blocked", "count": 2, "turn_ids": [],
                 "source": "elsewhere"}, {"category": "no text"}, "junk"]
    monkeypatch.setattr(judge, "action_items", items, raising=False)
    code, raw, _ = get(base, f"/api/actions?day={DAY}&x=1")
    assert code == 200 and SENTINEL.encode() not in raw
    d = json.loads(raw)
    a, b = d["items"]
    assert d["available"] is True and len(a["text"]) == 160 and len(a["example_quote"]) == 80
    assert a["turn_ids"] == [t1, 7] and a["uncalibrated"] is True and a["source"] == "judge"
    assert a["claude_md_line"].startswith("- Always run")
    assert b["source"] == "metrics" and b["claude_md_line"] is None and b["example_quote"] is None

    def boom(conn, day):
        raise RuntimeError(LONG_PROMPT)
    monkeypatch.setattr(judge, "action_items", boom, raising=False)
    code, raw, _ = get(base, f"/api/actions?day={DAY}")
    assert code == 200 and SENTINEL.encode() not in raw and json.loads(raw)["items"] == []
    assert get(base, "/api/actions?day=bogus")[0] == 400


def test_sql_display_clips_literals():
    sql = "SELECT id FROM turn WHERE prompt_text LIKE '%" + LONG_PROMPT + "%' AND x = 'it''s' AND " + "y" * 120
    out = serve.sql_display(sql)
    assert SENTINEL not in out and "'it''s'" in out and len(out) <= 500
    lits = re.findall(r"'(?:[^']|'')*'", out)
    assert all(len(x) <= 80 for x in lits) and max(len(t) for t in out.split()) <= 80
    ev = serve.sanitize_event({"type": "tool-start", "id": "q", "name": "query_db", "input": {"sql": sql}})
    assert SENTINEL not in json.dumps(ev) and ev["sql_full"] == out
    assert "sql_full" not in serve.sanitize_event({"type": "tool-start", "id": "g", "name": "get_turn",
                                                    "input": {"sql": "SELECT 1"}})


# --- you / agent attribution ---------------------------------------------------------------------------

def test_strengths_and_attribution(server, monkeypatch):
    from scorer import judge
    base, info = server
    for name in ("strengths", "attribution"):
        monkeypatch.delattr(judge, name, raising=False)
    assert json.loads(get(base, f"/api/strengths?day={DAY}")[1]) == {"day": DAY, "available": False, "items": []}
    assert json.loads(get(base, f"/api/attribution?day={DAY}")[1]) == {
        "day": DAY, "available": False, "corrections": None, "verification": None, "tooling": None}
    t0, t1 = info["turn_ids"][0], info["turn_ids"][1]
    monkeypatch.setattr(judge, "strengths", lambda conn, day: [
        {"text": "You asked for the test run right after the parser edit", "category": "verification", "count": 1,
         "turn_ids": [t0], "example_quote": LONG_PROMPT, "uncalibrated": True, "source": "judge",
         "episode": "good_spec"},
        {"text": "x", "count": 1, "turn_ids": [], "episode": LONG_PROMPT}], raising=False)
    monkeypatch.setattr(judge, "attribution", lambda conn, day: {
        "corrections": {"user_preventable": {"count": 2, "turn_ids": [t1, "x"]}, "agent_error": {"count": 1, "turn_ids": [t0]},
                        "environment": {"count": 0, "turn_ids": []}, "secret": LONG_PROMPT},
        "verification": {"asked_by_you": {"count": 1, "turn_ids": [t0]}, "agent_initiative": {"count": 0},
                         "none": {"count": 1, "turn_ids": [t1]}},
        "tooling": {"you": 3, "agent": "4", "default": {"count": 2, "turn_ids": [t0]}, "x": LONG_PROMPT}}, raising=False)
    code, raw, _ = get(base, f"/api/strengths?day={DAY}&x=1")
    assert code == 200 and SENTINEL.encode() not in raw
    it = json.loads(raw)["items"][0]
    assert it["uncalibrated"] and it["turn_ids"] == [t0] and len(it["example_quote"]) == 80
    assert it["episode"] == "good_spec" and "episode" not in json.loads(raw)["items"][1]
    code, raw, _ = get(base, f"/api/attribution?day={DAY}")
    assert code == 200 and SENTINEL.encode() not in raw
    a = json.loads(raw)
    assert a["corrections"]["user_preventable"] == {"count": 2, "turn_ids": [t1]}
    assert a["corrections"]["unclear"] == {"count": 0, "turn_ids": []} and "secret" not in a["corrections"]
    assert a["verification"]["agent_initiative"] == {"count": 0, "turn_ids": []}
    assert a["tooling"] == {"you": 3, "agent": 4, "default": 2}
    monkeypatch.setattr(judge, "attribution", lambda conn, day: 1 / 0, raising=False)
    a = json.loads(get(base, f"/api/attribution?day={DAY}")[1])
    assert a["available"] is True and a["corrections"] is None and "ZeroDivisionError" in a["error"]
    assert get(base, "/api/strengths?day=nope")[0] == 400


def test_day_split(server):
    base, _ = server
    d = json.loads(get(base, f"/api/day/{DAY}")[1])
    sp = d["split"]
    assert sp["verification"] == {"asked_by_you": 1, "agent_initiative": 0, "none": 1}   # the pytest call was human
    assert sp["time"]["agent_s"] == d["computed"]["active_s"] and sp["time"]["waiting_s"] == 60
    assert set(sp["blocked"]) == {"elsewhere_s", "quick_reply_s"}
    a = d["sessions"][0]["split"]
    assert a["verification"] == {"asked_by_you": 1, "agent_initiative": 0, "none": 1}
    assert a["tooling"] == {"you": 3, "agent": 4, "default": 0}
