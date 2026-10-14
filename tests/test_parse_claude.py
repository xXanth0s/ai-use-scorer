"""Claude parser on redacted real fixtures (tests/fixtures/claude) + unit cases."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from scorer.model import parse_ts
from scorer.parse_claude import parse, prompt_kind

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
