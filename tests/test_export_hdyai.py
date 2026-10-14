import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scorer import export_hdyai as x
from test_report import DAY, conn, filled, repo  # noqa: F401 - fixtures


@pytest.fixture()
def bundle(filled):
    c, _ = filled
    return x.build(c, DAY)


def test_build_shapes_and_ids(bundle):
    assert [r["id"] for r in bundle["reports"]] == [f"scorer-{DAY}"]
    rep = bundle["reports"][0]["local"]
    x.validate(rep)
    assert len(rep["content"]["findings"]) == 7
    ids = {e["id"] for s in rep["snapshot"] for e in s["events"]}
    for s in rep["snapshot"]:
        assert len(s["id"]) == 24 and ":" not in s["id"] and s["nativeId"] in ("s1aaaaaaaaaa", "s2bbbbbbbbbb")
        assert s["tokenUsage"]["finalOutputCountKnown"] is False and s["tokenUsage"]["finalOutputs"] == []
        assert all(not k.startswith("_") for k in s)
    sm = rep["scorerMetrics"]
    assert [t["key"] for t in sm["tiles"]] == ["V", "C", "episodes", "H", "B_min", "context", "cost"]
    assert all(e["id"] in ids for t in sm["tiles"] for e in t["evidence"])
    assert {l["sessionId"] for l in sm["lanes"]} == {s["id"] for s in rep["snapshot"]}
    assert all(i["evidenceId"] in ids for i in sm["interrupts"]) and sm["interrupts"]
    metric_ids = [i for i in ids if i.startswith("metric:")]
    assert metric_ids and all(i.startswith(f"metric:{DAY}:") for i in metric_ids)
    # TaskSpan ids: Claude = <our session id>:<idx>
    s1 = next(s for s in rep["snapshot"] if s["nativeId"] == "s1aaaaaaaaaa")
    assert s1["tasks"][0]["id"] == "s1aaaaaaaaaa:0" and s1["tasks"][-1]["aborted"] is True
    rec = s1["tokenUsage"]["records"][0]
    assert rec["inputTokens"] == 6000 and rec["totalTokens"] == 6200 and rec["cachedInputTokens"] == 5000


def test_ids_stable_across_rebuild(filled):
    c, _ = filled
    a = x.build(c, DAY)["reports"][0]["local"]
    b = x.build(c, DAY)["reports"][0]["local"]
    ev = lambda r: sorted(e["id"] for s in r["snapshot"] for e in s["events"])  # noqa: E731
    assert ev(a) == ev(b)


def test_write_local_idempotent(bundle, tmp_path):
    out = tmp_path / ".local" / "sessions.sqlite"
    x.write_local(bundle, out)
    x.write_local(bundle, out)
    c = sqlite3.connect(out)
    assert c.execute("SELECT count(*) FROM reports").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    data = json.loads(c.execute("SELECT data FROM reports").fetchone()[0])
    assert data["status"] == "complete" and data["content"]["findings"]


def test_analysis_request_hosted_rules(bundle):
    p = bundle["reports"][0]
    rid = x.hosted_id(p)
    assert rid.startswith(f"scorer-{DAY}-") and rid == x.hosted_id(p)
    req = x.analysis_request(p, rid)
    assert set(req) == {"requestId", "name", "role", "metrics", "input"}
    assert all(k.startswith("scorer_") or k in x._metrics_ts([]) or k.startswith("token") for k in req["metrics"])
    assert all(len(k) <= 40 and (v is None or v >= 0) for k, v in req["metrics"].items())
    evs = [e for s in req["input"] for e in s["evidence"]]
    assert evs and all(e["kind"] != "metric" and len(e["text"]) <= 1800 for e in evs)
    assert len(json.dumps(req)) < x.PROMPT_BUDGET


class _Stub(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _Stub.calls.append((self.path, self.headers.get("authorization"), body))
        if self.path == "/api/activate":
            return self._send(200, {"deviceToken": "hdyai_test", "userId": "u1", "expiresAt": "2026-11-13T00:00:00Z"})
        if self.path == "/api/analyze":
            return self._send(202, {"jobId": "j1", "status": "running"})
        return self._send(200, {"synced": True})

    def do_GET(self):  # noqa: N802
        _Stub.calls.append((self.path, self.headers.get("authorization"), None))
        req = next(b for p, _, b in _Stub.calls if p == "/api/analyze")
        first = req["input"][0]["evidence"][0]["id"]
        content = {"title": "t", "summary": "s", "findings": [], "followUps": [], "limitations": [],
                   "opportunities": [], "moments": [{"title": "m", "observation": "o", "attribution": "Candidate",
                                                     "evidenceIds": [first]}]}
        return self._send(200, {"jobId": "j1", "status": "complete", "content": content})


@pytest.fixture()
def stub():
    _Stub.calls = []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_activate_and_sync_against_stub(bundle, stub):
    r = x.activate(stub, "invite_" + "a" * 43)
    assert r["userId"] == "u1" and oct(x.token_path().stat().st_mode & 0o777) == "0o600"
    out = x.sync(bundle, stub, poll_s=0.01, echo=lambda *_: None)
    assert out[0]["cited"] == 1
    paths = [p for p, _, _ in _Stub.calls]
    assert paths == ["/api/activate", "/api/sync", "/api/analyze", "/api/analyses/j1", "/api/sync"]
    assert all(a == "Bearer hdyai_test" for p, a, _ in _Stub.calls[1:])
    pub = _Stub.calls[-1][2]["report"]
    job_content = pub["content"]
    assert job_content["moments"][0]["title"] == "m"  # Eve content byte-identical, not ours
    req = _Stub.calls[2][2]
    sent = {e["id"]: e for s in req["input"] for e in s["evidence"]}
    for e in pub["evidenceIndex"]:
        core = {k: v for k, v in e.items() if k not in ("sessionTitle", "agent")}
        assert core == sent[e["id"]]


def test_dry_run_no_http(bundle, tmp_path):
    f = tmp_path / "payload.json"
    out = x.sync(bundle, "http://127.0.0.1:9", dry_run=str(f))
    assert out[0]["prompt_chars"] > 0 and json.loads(f.read_text())[0]["analyze"]["input"]


def test_bad_invite_rejected():
    with pytest.raises(ValueError):
        x.activate("http://127.0.0.1:9", "nope")
