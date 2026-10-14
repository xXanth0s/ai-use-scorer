"""Codex rollout parser on redacted real fixtures + synthetic edge cases."""
from __future__ import annotations

import json
import os
from collections import Counter

import pytest

from scorer.model import parse_ts
from scorer.parse_codex import parse, parse_patch

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "codex")
GOLDEN3 = os.path.join(os.path.dirname(__file__), "golden", "codex_golden3.jsonl")


def fx(name):
    return parse(os.path.join(FIX, name))


def _invariants(b):
    for t in b.turns:
        assert parse_ts(t.agent_finished_at) >= parse_ts(t.prompt_at)
    for c in b.tool_calls:
        assert c.has_result or b.turns[c.turn_idx].interrupted
    assert len({a.request_id for a in b.api_calls}) == len(b.api_calls)
    assert b.session.agent == "codex"


def test_interrupt_multiturn():
    b = fx("codex_interrupt_multiturn.jsonl")
    _invariants(b)
    assert b.session.id == "01a124a2-a524-73f0-8856-14ebeef49100"
    assert len(b.turns) == 6
    assert [t.idx for t in b.turns] == list(range(6))
    intr = [t for t in b.turns if t.interrupted]
    assert len(intr) == 1 and intr[0].end_source == "interrupt"
    assert Counter(t.end_source for t in b.turns)["task_complete"] == 5
    assert all(t.initiator == "human" and t.prompt_kind == "prompt" for t in b.turns)
    assert len(b.api_calls) == 6 and sum(a.output_tok for a in b.api_calls) == 1185
    assert [c.name for c in b.tool_calls] == ["web__run"] and b.tool_calls[0].kind == "web"


def test_failing_exec_review_subagent():
    b = fx("codex_failing_exec_review.jsonl")
    _invariants(b)
    assert b.session.forked_from == "01a11840-b8dc-7ef3-acd7-88dad372ccd5"
    assert len(b.turns) == 1 and b.turns[0].initiator == "agent"
    assert len(b.api_calls) == 11
    bash = [c for c in b.tool_calls if c.kind == "bash"]
    assert len(bash) == 10 and all(c.tool_use_id.startswith("exec-") for c in bash)
    assert sum(c.is_error for c in bash) == 3
    assert all(c.sidechain_id == b.session.id for c in b.tool_calls)
    assert all(c.duration_ms is not None and c.duration_ms >= 0 for c in bash)


def test_apply_patch_forked_subagent_skips_inherited_history():
    b = fx("codex_apply_patch_subagent.jsonl")
    _invariants(b)
    # first session_meta = this thread; the parent's copied history (ordinal < 13) is skipped
    assert b.session.id == "01a13295-ac0b-7a00-b829-553e95b99c29"
    assert b.session.forked_from == "01a13292-4f4d-7783-8196-0d433ba6f14f"
    assert len(b.turns) == 1 and b.turns[0].initiator == "agent"
    assert len(b.api_calls) == 14 and all(a.sidechain_id == b.session.id for a in b.api_calls)
    edits = [c for c in b.tool_calls if c.kind == "edit"]
    assert len(edits) == 2 and all(c.name == "apply_patch" and c.has_result for c in edits)
    patch_ids = {c.tool_use_id for c in edits}
    patch_edits = [e for e in b.file_edits if e.tool_use_id in patch_ids]
    assert sorted((e.lines_added, e.lines_removed) for e in patch_edits) == [(6, 0), (59, 0)]
    # shell writes (here a `mkdir`) also become FileEdits, whatever the aggregated intent
    shell_ids = {c.tool_use_id for c in b.tool_calls if c.kind == "bash"}
    assert {e.tool_use_id for e in b.file_edits} - patch_ids <= shell_ids
    assert any(e.tool_use_id in shell_ids for e in b.file_edits)
    assert not any(c.name == "exec" for c in b.tool_calls)  # wrappers are named after the inner tool


def test_steer_becomes_queued_turn_and_subagent_events():
    b = fx("codex_steer_mcp.jsonl")
    _invariants(b)
    assert [(t.prompt_kind, t.initiator) for t in b.turns] == [("prompt", "human"), ("queued", "human")]
    q = b.turns[1]
    assert q.enqueued_at == q.prompt_at and q.end_source == "task_complete"
    assert b.turns[0].agent_finished_at == q.prompt_at  # steer: gap 0
    assert len(b.api_calls) == 27
    kinds = Counter(c.kind for c in b.tool_calls)
    assert kinds["subagent"] == 7 and kinds["bash"] == 7 and kinds["web"] == 12
    assert sum(1 for e in b.events if e.kind == "subagent") == 4
    tooling = [e.data for e in b.events if e.kind == "tooling"]
    assert tooling == [{"category": "skill", "name": "research", "initiator": "agent",
                        "rule": "codex:skill-item", "tool_use_id": tooling[0]["tool_use_id"]}]


def test_stop_hook_overrides_task_complete():
    sid = "00000000-0000-7000-8000-000000000003"
    hooks = [{"at": "2026-10-10T10:00:09.000Z", "agent": "codex", "event": "Stop", "payload": {"session_id": sid}},
             {"at": "2026-10-10T10:00:01.200Z", "agent": "codex", "event": "UserPromptSubmit",
              "payload": {"session_id": sid}}]
    b = parse(GOLDEN3, hooks)
    t = b.turns[0]
    assert t.end_source == "stop_hook" and t.agent_finished_at == "2026-10-10T10:00:09+00:00"
    assert sorted(e.data["event"] for e in b.events if e.kind == "hook") == ["Stop", "UserPromptSubmit"]


def _write(tmp_path, recs):
    p = tmp_path / "rollout-x.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return str(p)


def _base(sid="00000000-0000-7000-8000-00000000000a"):
    return [{"timestamp": "2026-10-10T10:00:00.000Z", "type": "session_meta", "ordinal": 0,
             "payload": {"id": sid, "session_id": sid, "cwd": "/tmp/x", "thread_source": "user"}}]


def _task(o, ts, tid):
    return {"timestamp": ts, "type": "event_msg", "ordinal": o, "payload": {"type": "task_started", "turn_id": tid}}


def _user(o, ts, text):
    return {"timestamp": ts, "type": "event_msg", "ordinal": o,
            "payload": {"type": "item_completed", "item": {"type": "UserMessage", "id": f"u{o}",
                                                           "content": [{"type": "text", "text": text}]}}}


def _end(o, ts, kind="task_complete"):
    return {"timestamp": ts, "type": "event_msg", "ordinal": o, "payload": {"type": kind, "turn_id": "t"}}


def test_tooling_prefixes_and_mcp(tmp_path):
    recs = _base() + [
        _task(1, "2026-10-10T10:00:01Z", "t1"), _user(2, "2026-10-10T10:00:01Z", "/review please"),
        {"timestamp": "2026-10-10T10:00:02Z", "type": "response_item", "ordinal": 3,
         "payload": {"type": "function_call", "name": "js", "namespace": "mcp__cua_repl", "call_id": "call_m1",
                     "arguments": "{}"}},
        {"timestamp": "2026-10-10T10:00:03Z", "type": "response_item", "ordinal": 4,
         "payload": {"type": "function_call_output", "call_id": "call_m1", "output": "ok"}},
        _end(5, "2026-10-10T10:00:04Z"),
        _task(6, "2026-10-10T10:01:00Z", "t2"), _user(7, "2026-10-10T10:01:00Z", "use $research for $HOME stuff"),
        _end(8, "2026-10-10T10:01:05Z"),
    ]
    b = parse(_write(tmp_path, recs))
    tooling = [(e.turn_idx, e.data["category"], e.data["name"], e.data["initiator"], e.data["rule"])
               for e in b.events if e.kind == "tooling"]
    assert tooling == [(0, "command", "review", "human", "codex:user-prefix"),
                       (0, "mcp", "cua_repl", "agent", "codex:mcp-call"),
                       (1, "skill", "research", "human", "codex:user-prefix")]
    assert b.turns[0].prompt_kind == "command"
    assert b.tool_calls[0].has_result and b.tool_calls[0].duration_ms == 1000


def test_path_prompt_is_not_a_command(tmp_path):
    recs = _base() + [_task(1, "2026-10-10T10:00:00Z", "t1"), _user(2, "2026-10-10T10:00:00Z", "/Users/x/a.py fix"),
                      _end(3, "2026-10-10T10:00:03Z")]
    b = parse(_write(tmp_path, recs))
    assert b.turns[0].prompt_kind == "prompt"
    assert not [e for e in b.events if e.kind == "tooling"]


def test_folded_task_with_activity_then_abort_marks_interrupt(tmp_path):
    recs = _base() + [
        _task(1, "2026-10-10T10:00:00Z", "t1"), _user(2, "2026-10-10T10:00:00Z", "hi"), _end(3, "2026-10-10T10:00:03Z"),
        _task(4, "2026-10-10T10:00:10Z", "t2"),
        {"timestamp": "2026-10-10T10:00:11Z", "type": "response_item", "ordinal": 5,
         "payload": {"type": "message", "role": "assistant", "content": []}},
        _end(6, "2026-10-10T10:00:12Z", "turn_aborted"),
    ]
    b = parse(_write(tmp_path, recs))
    assert len(b.turns) == 1 and b.turns[0].interrupted and b.turns[0].end_source == "interrupt"
    assert b.turns[0].agent_finished_at == "2026-10-10T10:00:12+00:00"


def test_no_prompt_task_folds_into_previous_turn(tmp_path):
    recs = _base() + [
        _task(1, "2026-10-10T10:00:00Z", "t1"), _user(2, "2026-10-10T10:00:00Z", "hi"),
        {"timestamp": "2026-10-10T10:00:02Z", "type": "response_item", "ordinal": 3,
         "payload": {"type": "message", "role": "assistant", "content": []}},
        _end(4, "2026-10-10T10:00:03Z"),
        _task(5, "2026-10-10T10:00:10Z", "t2"), _end(6, "2026-10-10T10:00:11Z", "turn_aborted"),
    ]
    b = parse(_write(tmp_path, recs))
    assert len(b.turns) == 1 and not b.turns[0].interrupted
    assert b.turns[0].agent_finished_at == "2026-10-10T10:00:03+00:00"


def test_parse_patch_headers_and_escaped_newlines():
    patch = ("*** Begin Patch\\n*** Add File: a.py\\n+x\\n+y\\n*** Update File: b.py\\n@@\\n-old\\n+new\\n"
             "*** Delete File: c.py\\n*** End Patch")
    assert parse_patch(patch) == [("add", "a.py", 2, 0), ("update", "b.py", 1, 1), ("delete", "c.py", 0, 0)]


def test_bad_file_returns_none(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("not json\n{}\n")
    assert parse(str(p)) is None


@pytest.mark.parametrize("name", sorted(f for f in os.listdir(FIX) if f.endswith(".jsonl")))
def test_fixtures_have_no_long_free_text(name):
    # redaction guard: no string value with whitespace-separated words beyond program names
    with open(os.path.join(FIX, name)) as f:
        for line in f:
            def walk(v):
                if isinstance(v, dict):
                    for x in v.values():
                        walk(x)
                elif isinstance(v, list):
                    for x in v:
                        walk(x)
                elif isinstance(v, str):
                    assert len(v.split()) <= 4 or set(v) <= set("0123456789abcdef\n+-@ "), v[:40]
            walk(json.loads(line))


def _exec_item(o, ts, cmd, out, ec=0):
    return {"timestamp": ts, "type": "event_msg", "ordinal": o,
            "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "id": f"exec-{o}", "command": ["/bin/zsh", "-lc", cmd], "cwd": "/tmp/x",
                "status": "completed" if ec == 0 else "failed", "aggregated_output": out, "exit_code": ec,
                "duration": {"secs": 1, "nanos": 0}}}}


def test_masked_test_failure_flags_exec(tmp_path):
    """`pytest | tail` exits 0 (exit_code 0) but the output shows failures -> is_error."""
    def call(o, ts, cid, cmd):
        return {"timestamp": ts, "type": "response_item", "ordinal": o,
                "payload": {"type": "function_call", "name": "exec_command", "call_id": cid,
                            "arguments": json.dumps({"cmd": cmd})}}

    def output(o, ts, cid, text):
        return {"timestamp": ts, "type": "response_item", "ordinal": o,
                "payload": {"type": "function_call_output", "call_id": cid, "output": text}}

    recs = _base() + [
        _task(1, "2026-10-10T10:00:01Z", "t1"), _user(2, "2026-10-10T10:00:01Z", "fix"),
        call(3, "2026-10-10T10:00:02Z", "c1", "pytest -q | tail -3"),
        output(4, "2026-10-10T10:00:03Z", "c1", "Exit code: 0\nOutput:\n1 failed, 2 passed in 0.1s"),
        call(5, "2026-10-10T10:00:04Z", "c2", "pytest -q | tail -3"),
        output(6, "2026-10-10T10:00:05Z", "c2", "Exit code: 0\nOutput:\ntest_error PASSED\n3 passed in 0.1s"),
        call(7, "2026-10-10T10:00:06Z", "c3", "cat ci.log"),
        output(8, "2026-10-10T10:00:07Z", "c3", "Exit code: 0\nOutput:\n1 failed in 0.1s"),
        {"timestamp": "2026-10-10T10:00:08Z", "type": "response_item", "ordinal": 9,
         "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c4", "input": "x"}},
        _exec_item(10, "2026-10-10T10:00:09Z", "go test ./... | tee log", "--- FAIL: TestA\nFAIL\tpkg\t0.1s"),
        _exec_item(11, "2026-10-10T10:00:10Z", "go test ./...", "ok  \tpkg\t0.1s"),
        _end(12, "2026-10-10T10:00:11Z"),
    ]
    b = parse(_write(tmp_path, recs))
    got = {(c.args_summary, c.is_error) for c in b.tool_calls}
    assert got == {("pytest -q | tail -3", True), ("pytest -q | tail -3", False), ("cat ci.log", False),
                   ("go test ./... | tee log", True), ("go test ./...", False)}
