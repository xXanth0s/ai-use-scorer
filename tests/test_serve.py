"""Dashboard JSON endpoints against a small fixture DB (never ~/.scorer)."""
from __future__ import annotations

import json
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
        events=[Event(kind="compaction", at=T(10, 55), turn_idx=3, data={"secret": LONG_PROMPT})],
    )
    b = Bundle(
        session=Session(id="bbbbbbbb-2222-4000-8000-000000000002", agent="codex",
                        cwd="/Users/x/Projects/api-gateway", repo="/Users/x/Projects/api-gateway"),
        turns=[Turn(idx=0, prompt_at=T(9, 10), prompt_text="fix the flaky pagination test",
                    agent_finished_at=T(9, 40), end_source="task_complete"),
               Turn(idx=1, prompt_at=T(9, 45), prompt_text="ok ship it", agent_finished_at=None)],
        tool_calls=[ToolCall(turn_idx=0, tool_use_id="tu-b0", name="shell", kind="bash", bash_intent="build",
                             args_summary="cargo check", started_at=T(9, 20), has_result=True, is_error=True)],
    )
    c = Bundle(
        session=Session(id="cccccccc-3333-4000-8000-000000000003", agent="claude", cwd="/Users/x/dotfiles"),
        turns=[Turn(idx=0, prompt_at=T(14, 0), prompt_text="tidy zshrc", agent_finished_at=T(14, 6))],
    )
    for bun in (a, b, c):
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
LONG_OK = {"where", "change"}


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
    assert day == {"day": DAY, "turns": 7, "sessions": 3}


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
    assert d["verdict"]["basis"] == "B_min" and "14" in d["verdict"]["change"]


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
    assert evs[1]["summary"].startswith("7 turns")
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
    assert r["ok"] and len(r["data"]["sessions"]) == 3
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
                body = b'{"ok": true}'
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
                       {"type": "tool-start", "id": "t1", "name": "get_turn", "input": {"turn_id": 2}},
                       {"type": "tool-result", "id": "t1", "name": "get_turn", "ok": True, "summary": "turn 2",
                        "citations": [{"n": 1, "turn_id": 2, "session_id": "a", "label": LONG_PROMPT}]},
                       {"type": "subagent-result", "id": "d1", "name": "turnInvestigator", "summary": LONG_PROMPT},
                       {"type": "text-delta", "delta": "answer [1]"},
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
        assert [e["type"] for e in evs] == ["subagent-start", "tool-start", "tool-result", "subagent-result",
                                           "text-delta", "done"]
        assert evs[-1] == {"type": "done", "model": "claude-opus-5-5", "backend": "mastra"}
        assert len(evs[0]["task"]) <= 80 and len(evs[3]["summary"]) <= 80
        assert len(evs[2]["citations"][0]["label"]) <= 80
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
