"""Claude parser on redacted real fixtures (tests/fixtures/claude) + unit cases."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from scorer.model import parse_ts
import pytest

from scorer.parse_claude import output_failed, parse, prompt_kind

ROOT = os.path.dirname(os.path.dirname(__file__))
FIX = os.path.join(ROOT, "tests", "fixtures", "claude")
GOLD = os.path.join(ROOT, "tests", "golden")


def fx(name):
    b = parse(os.path.join(FIX, name))
    assert b is not None
    return b


def kinds(b):
    return [t.prompt_kind for t in b.turns]


def test_interruption_fixture():
    b = fx("interruption.jsonl")
    assert kinds(b) == ["prompt", "queued"]
    t = b.turns[0]
    assert t.interrupted and t.end_source == "interrupt"
    assert (len(b.api_calls), len(b.tool_calls)) == (11, 11)
    assert b.turns[1].enqueued_at is not None


def test_error_tool_result_fixture():
    b = fx("error_tool_result.jsonl")
    assert len(b.turns) == 8
    assert (len(b.api_calls), len(b.tool_calls)) == (6, 6)
    assert sum(c.is_error for c in b.tool_calls) == 1
    assert sum(1 for e in b.events if e.kind == "tooling") == 2


def test_compaction_fixture():
    b = fx("compaction.jsonl")
    ev = [e for e in b.events if e.kind == "compaction"]
    assert len(ev) == 1 and ev[0].data["trigger"] in ("manual", "auto") and ev[0].data["preTokens"]
    assert len(b.turns) == 3


def test_resumed_fixture_spans_days_without_splitting():
    b = fx("resumed.jsonl")
    assert len(b.turns) == 3
    assert len({t.prompt_at[:10] for t in b.turns}) == 2
    assert (len(b.api_calls), len(b.tool_calls), len(b.file_edits)) == (16, 13, 2)
    for t in b.turns:   # no turn runs into the next day's prompt
        assert parse_ts(t.agent_finished_at) >= parse_ts(t.prompt_at)


def test_queued_fixture():
    b = fx("queued.jsonl")
    assert kinds(b).count("queued") == 1
    q = next(t for t in b.turns if t.prompt_kind == "queued")
    assert parse_ts(q.enqueued_at) <= parse_ts(q.prompt_at)
    assert (len(b.api_calls), len(b.tool_calls)) == (10, 6)


def test_subagent_fixture_folds_into_parent():
    b = fx("subagent.jsonl")
    assert len(b.turns) == 2
    side = [c for c in b.tool_calls if c.sidechain_id]
    assert len(side) == 14 and len(b.tool_calls) == 23
    assert {c.sidechain_id for c in side} == {"aa4ff8af5459477a8"}
    ev = [e for e in b.events if e.kind == "subagent"]
    assert len(ev) == 1 and ev[0].data["agent_id"] == "aa4ff8af5459477a8" and ev[0].data["n_tools"] == 14
    assert ev[0].turn_idx == 0
    # parent turn end covers the subagent activity, but never past the next prompt
    t0, t1 = b.turns
    assert parse_ts(t0.agent_finished_at) <= parse_ts(t1.prompt_at)
    assert all(c.turn_idx is not None for c in side)


def test_all_fixtures_have_no_negative_gaps():
    for name in os.listdir(FIX):
        if not name.endswith(".jsonl"):
            continue
        b = fx(name)
        for p, n in zip(b.turns, b.turns[1:]):
            assert (parse_ts(n.prompt_at) - parse_ts(p.agent_finished_at)).total_seconds() >= -1, name


def test_stop_hook_end_wins(tmp_path):
    path = os.path.join(GOLD, "g1_gaps.jsonl")
    base = parse(path)
    t0 = base.turns[0]
    stop_at = "2026-10-01T08:00:25+00:00"   # after the turn_duration end (08:00:20)
    hooks = [{"at": stop_at, "agent": "claude", "event": "Stop",
              "payload": {"session_id": base.session.id}, "git": {"head": "abc"}},
             {"at": "2026-10-01T08:00:00.5+00:00", "agent": "claude", "event": "UserPromptSubmit",
              "payload": {"session_id": base.session.id}}]
    b = parse(path, hooks)
    assert b.turns[0].end_source == "stop_hook"
    assert parse_ts(b.turns[0].agent_finished_at) == parse_ts(stop_at)
    assert b.turns[1].end_source == "turn_duration"
    kinds_ = sorted(e.kind for e in b.events)
    assert kinds_ == ["git_snapshot", "hook", "hook"]
    assert all(e.turn_idx == 0 for e in b.events)
    assert t0.end_source == "turn_duration"


def test_prompt_rule():
    def u(content, **kw):
        return {"type": "user", "message": {"role": "user", "content": content}, **kw}
    assert prompt_kind(u("hello")) == "prompt"
    assert prompt_kind(u("<command-message>x</command-message><command-name>/x</command-name>")) == "command"
    assert prompt_kind(u("<command-name>/clear</command-name>")) == "command"
    assert prompt_kind(u("<task-notification>done")) is None
    assert prompt_kind(u("hello", isMeta=True)) is None
    assert prompt_kind(u("summary", isCompactSummary=True)) is None
    assert prompt_kind(u("done", origin={"kind": "task-notification"})) is None
    assert prompt_kind(u("Another Claude session sent a message: hi")) is None
    assert prompt_kind(u([{"type": "text", "text": "[Request interrupted by user for tool use]"}])) == "interrupt"
    assert prompt_kind(u([{"type": "tool_result", "tool_use_id": "x", "content": "hello"}])) is None


def test_fork_copies_skipped_when_source_present(tmp_path):
    src = os.path.join(GOLD, "g1_gaps.jsonl")
    rows = [json.loads(line) for line in open(src)]
    sid_src = rows[0]["sessionId"]
    (tmp_path / f"{sid_src}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    new = "f0f0f0f0-0000-0000-0000-000000000000"
    fork = [{**r, "sessionId": new, "forkedFrom": {"sessionId": sid_src, "messageUuid": r["uuid"]}} for r in rows]
    fork += [{"type": "user", "uuid": "new-1", "timestamp": "2026-10-02T08:00:00Z", "sessionId": new,
              "message": {"role": "user", "content": "continue in fork"}},
             {"type": "assistant", "uuid": "new-2", "timestamp": "2026-10-02T08:00:01Z", "sessionId": new,
              "requestId": "req_fork", "message": {"model": "claude-opus-5-5", "usage": {}, "content": []}}]
    p = tmp_path / f"{new}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in fork))
    b = parse(str(p))
    assert len(b.turns) == 1 and b.session.forked_from == sid_src
    assert [a.request_id for a in b.api_calls] == ["req_fork"]


def test_redact_keeps_parser_markers(tmp_path):
    out = tmp_path / "r.jsonl"
    src = os.path.join(GOLD, "g9_tooling.jsonl")
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "redact.py"), src, str(out)], check=True,
                   capture_output=True)
    text = out.read_text()
    assert "run the tests please" not in text and "/w/repo" not in text
    b, r = parse(src), parse(str(out))
    assert kinds(b) == kinds(r)
    assert len(b.tool_calls) == len(r.tool_calls) and len(b.api_calls) == len(r.api_calls)


def test_detached_run_does_not_stretch_turn(tmp_path):
    """A run resumed hours later without a prompt (task notification) keeps rows but not the end."""
    rows = [json.loads(line) for line in open(os.path.join(GOLD, "g2_request_dedupe.jsonl"))]
    sid = rows[0]["sessionId"]
    late = [{"type": "user", "uuid": "late-1", "timestamp": "2026-10-03T08:00:00Z", "sessionId": sid,
             "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": "<task-notification>x"}},
            {"type": "assistant", "uuid": "late-2", "timestamp": "2026-10-03T08:00:05Z", "sessionId": sid,
             "requestId": "req_late", "message": {"model": "claude-opus-5-5", "usage": {"output_tokens": 1},
                                                  "content": [{"type": "text", "text": "ok"}]}},
            {"type": "system", "subtype": "turn_duration", "durationMs": 5000, "uuid": "late-3",
             "timestamp": "2026-10-03T08:00:06Z", "sessionId": sid}]
    p = tmp_path / "s.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows + late))
    b = parse(str(p))
    t = b.turns[0]
    assert t.end_source == "turn_duration" and t.agent_finished_at.startswith("2026-10-01T08:00:06")
    assert "req_late" in {a.request_id for a in b.api_calls}


def test_hookio_stop_line_through_ingest(tmp_path, scorer_home):
    """A Stop line written by the real `scorer event` hook ends the turn (stop_hook) and its git
    snapshot attaches to that turn, matched via payload.prompt_id."""
    from datetime import datetime, timedelta, timezone

    from scorer.db import connect
    from scorer.ingest import run_ingest
    from scorer.model import events_path

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@x", "-c", "user.name=t"]
    (repo / "hello.py").write_text("print('hello')\n")
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "hello.py"], check=True)
    subprocess.run(git + ["commit", "-qm", "init"], check=True)
    (repo / "hello.py").write_text("print('hello')\nassert False\n")

    sid, pid = "abcdabcd-0000-0000-0000-000000000001", "pid-0001"
    now = datetime.now(timezone.utc)
    ts = lambda s: (now - timedelta(seconds=s)).isoformat().replace("+00:00", "Z")  # noqa: E731
    rows = [
        {"type": "user", "uuid": "u1", "timestamp": ts(60), "sessionId": sid, "cwd": str(repo), "promptId": pid,
         "promptSource": "typed", "message": {"role": "user", "content": "add a failing test to hello.py"}},
        {"type": "assistant", "uuid": "u2", "timestamp": ts(55), "sessionId": sid, "requestId": "r1",
         "message": {"model": "claude-opus-5-5", "usage": {"output_tokens": 3},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                  "input": {"command": "cat >> hello.py <<'EOF'\nassert False\nEOF"}}]}},
        {"type": "user", "uuid": "u3", "timestamp": ts(54), "sessionId": sid, "promptId": pid,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": ""}]}},
        {"type": "assistant", "uuid": "u4", "timestamp": ts(50), "sessionId": sid, "requestId": "r2",
         "message": {"model": "claude-opus-5-5", "usage": {"output_tokens": 3},
                     "content": [{"type": "text", "text": "done"}]}},
        {"type": "system", "subtype": "turn_duration", "uuid": "u5", "timestamp": ts(49), "sessionId": sid,
         "durationMs": 11000},
    ]
    proj = tmp_path / "projects" / "-tmp-repo"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    payload = {"session_id": sid, "prompt_id": pid, "transcript_path": str(proj / f"{sid}.jsonl"),
               "cwd": str(repo), "hook_event_name": "Stop", "stop_hook_active": False}
    r = subprocess.run([sys.executable, "-m", "scorer", "event", "--agent", "claude", "--event", "Stop"],
                       input=json.dumps(payload), capture_output=True, text=True,
                       env={**os.environ, "SCORER_HOME": str(scorer_home)})
    assert r.returncode == 0 and r.stdout == ""
    line = json.loads(events_path().read_text().splitlines()[-1])
    assert line["event"] == "Stop" and line["payload"]["prompt_id"] == pid and "hello.py" in line["git"]["changed_paths"]

    conn = connect(tmp_path / "t.db")
    run_ingest(conn, since_days=1, claude_root=str(tmp_path / "projects"), codex_root=str(tmp_path / "none"),
               events_file=str(events_path()))
    t = conn.execute("SELECT id, end_source, agent_finished_at FROM turn WHERE session_id=?", (sid,)).fetchone()
    assert t["end_source"] == "stop_hook"
    assert t["agent_finished_at"] == line["at"]
    snap = conn.execute("SELECT turn_id, data FROM event WHERE kind='git_snapshot' AND session_id=?", (sid,)).fetchone()
    assert snap["turn_id"] == t["id"] and "hello.py" in json.loads(snap["data"])["git"]["changed_paths"]


def test_masked_exit_pytest_fixture():
    """Real session (redacted): `pytest ...; echo "exit code: $?"` exits 0 and Claude sets is_error=false,
    but the run failed. Install/--version calls in the same session stay non-errors."""
    b = fx("masked_exit_pytest.jsonl")
    got = [(c.bash_intent, c.is_error) for c in b.tool_calls]
    assert got == [("test", False), ("test", False), ("edit", False), ("test", True)]


@pytest.mark.parametrize("text", [
    "FAILED hello.py::test_x - AssertionError\n===== 1 failed in 0.02s =====",
    "1 failed, 3 passed in 1.20s",
    "==== 2 passed, 1 error in 0.30s ====",
    "web:test:unit:       Tests  9 failed | 300 passed (309)",
    " Test Files  1 failed (1)",
    "Test Suites: 4 failed, 3 passed, 7 total\nTests:       6 failed, 80 passed, 86 total",
    "(fail) suite > case\n\n 12 pass\n 2 fail\n Ran 14 tests",
    "--- FAIL: TestFoo (0.00s)\nFAIL\nFAIL\texample.com/pkg\t0.01s",
    "test result: FAILED. 3 passed; 1 failed; 0 ignored",
    "src/a.ts(3,5): error TS2304: Cannot find name 'x'.",
    "✖ 7 problems (1 error, 6 warnings)",
    'error: script "lint:ci" exited with code 1',
    " ERROR  run failed: command  exited (1)",
    "Failed:    web#typecheck",
    "ok\nexit code: 2",
    "EXIT=1",
    "Found 3 errors in 2 files (checked 10 source files)",
    "  2 failed\n  5 passed (3.1s)",
])
def test_output_failed_markers(text):
    assert output_failed(text)


@pytest.mark.parametrize("text", [
    "test_x.py::test_error PASSED\ntest_x.py::test_fail_path PASSED\n===== 2 passed in 0.10s =====",
    "Tests  300 passed (300)",
    "Tests:       80 passed, 80 total",
    " 14 pass\n 0 fail\n Ran 14 tests",
    "ok  \texample.com/pkg\t0.01s",
    "test result: ok. 4 passed; 0 failed",
    "✖ 6 problems (0 errors, 6 warnings)",
    "exit code: 0",
    "Success: no issues found in 3 source files",
    "handles error: missing file\nall good",
    "",
])
def test_output_failed_ignores_passing_runs(text):
    assert not output_failed(text)


def _bash_session(tmp_path, cmd, content, is_error=False, tur=None):
    sid = "00000000-0000-4000-8000-0000000000b1"
    base = {"sessionId": sid, "cwd": "/tmp/r", "isSidechain": False}
    rows = [
        {**base, "type": "user", "uuid": "u1", "timestamp": "2026-10-01T08:00:00Z", "promptId": "p1",
         "origin": {"kind": "human"}, "message": {"role": "user", "content": "go"}},
        {**base, "type": "assistant", "uuid": "a1", "timestamp": "2026-10-01T08:00:01Z", "requestId": "r1",
         "message": {"model": "claude-opus-5-5", "usage": {"output_tokens": 1},
                     "content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": cmd}}]}},
        {**base, "type": "user", "uuid": "u2", "timestamp": "2026-10-01T08:00:05Z", "promptId": "p1",
         "toolUseResult": tur if tur is not None else {"stdout": content, "stderr": "", "interrupted": False},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1",
                                                    "content": content, "is_error": is_error}]}},
    ]
    p = tmp_path / f"{sid}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return parse(str(p)).tool_calls[0]


def test_masked_failure_only_for_test_build_intents(tmp_path):
    out = "=== 1 failed in 0.1s ==="
    assert _bash_session(tmp_path, "pytest -q 2>&1 | tail -3", out).is_error
    assert _bash_session(tmp_path, "pnpm build | tail", [{"type": "text", "text": "x.ts(1,1): error TS2304: y"}]).is_error
    assert not _bash_session(tmp_path, "cat ci.log", out).is_error           # reading a log is not a run
    assert not _bash_session(tmp_path, "pytest -q", "3 passed in 0.1s").is_error
    assert _bash_session(tmp_path, "pytest", "Exit code 1\nboom", is_error=True).is_error


def test_interrupted_bash_is_error(tmp_path):
    tc = _bash_session(tmp_path, "pytest", "partial", tur={"stdout": "partial", "stderr": "", "interrupted": True})
    assert tc.is_error


def test_bash_edit_paths_recorded_whatever_the_intent(tmp_path):
    body = "x = 1\n" * 200
    cmd = f"cat > a.py <<'EOF'\n{body}EOF\npytest -q tests/test_a.py"
    tc = _bash_session(tmp_path, cmd, "1 passed in 0.1s")
    assert tc.bash_intent == "test"
    # long command: summary keeps the edit and the deciding pytest segment, drops the heredoc body
    assert len(cmd) > 500 and "pytest -q tests/test_a.py" in tc.args_summary and "x = 1" not in tc.args_summary
    b = parse(str(tmp_path / "00000000-0000-4000-8000-0000000000b1.jsonl"))
    assert [e.path for e in b.file_edits] == ["/tmp/r/a.py"]
