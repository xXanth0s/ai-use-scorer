"""Claude goldens from spec §6b (1, 2, 4, 5, 7) + tooling. Files are hand-written minimal jsonl."""
from __future__ import annotations

import os

import pytest

from scorer.model import parse_ts
from scorer.parse_claude import parse

HERE = os.path.dirname(__file__)


def g(name):
    return parse(os.path.join(HERE, name))


def secs(a, b):
    return (parse_ts(b) - parse_ts(a)).total_seconds()


def test_g1_gap_boundaries():
    b = g("g1_gaps.jsonl")
    assert len(b.turns) == 4
    assert all(t.end_source == "turn_duration" for t in b.turns)
    gaps = [secs(p.agent_finished_at, n.prompt_at) for p, n in zip(b.turns, b.turns[1:])]
    assert gaps == pytest.approx([10, 90, 200], abs=0.01)


def test_g2_one_request_three_lines():
    b = g("g2_request_dedupe.jsonl")
    assert len(b.api_calls) == 2
    a = next(x for x in b.api_calls if x.request_id == "req_same")
    assert (a.input_tok, a.cached_tok, a.cache_write_tok, a.output_tok) == (5, 2000, 100, 300)
    assert a.at == b.turns[0].agent_started_at
    assert a.effort == "high" and a.model == "claude-opus-5-5"


def test_g4_queued_prompt():
    b = g("g4_queued.jsonl")
    assert [t.prompt_kind for t in b.turns] == ["prompt", "queued"]
    q = b.turns[1]
    assert secs(b.turns[0].prompt_at, q.enqueued_at) == pytest.approx(5)
    assert secs(b.turns[0].prompt_at, q.prompt_at) == pytest.approx(30.1)   # dequeue ts
    assert secs(b.turns[0].agent_finished_at, q.prompt_at) >= 0


def test_g5_interrupt_thrown_45s():
    b = g("g5_interrupt.jsonl")
    assert len(b.turns) == 2            # the marker is not a turn
    t = b.turns[0]
    assert t.interrupted and t.end_source == "interrupt"
    assert secs(t.agent_started_at, t.agent_finished_at) == pytest.approx(45)
    assert not b.turns[1].interrupted
    assert all(c.has_result for c in b.tool_calls)


def test_g7_user_modified():
    b = g("g7_user_modified.jsonl")
    fe = {f.path: f for f in b.file_edits}
    assert fe["/w/repo/src/a.py"].user_modified is True
    assert (fe["/w/repo/src/a.py"].lines_added, fe["/w/repo/src/a.py"].lines_removed) == (2, 1)
    assert fe["/w/repo/src/b.py"].user_modified is False
    assert fe["/w/repo/src/b.py"].lines_added == 3


def test_g8_overlap_files_parse():
    a, b = g("g8_overlap_a.jsonl"), g("g8_overlap_b.jsonl")
    assert a.session.cwd != b.session.cwd
    ta, tb = a.turns[0], b.turns[0]
    overlap = secs(tb.prompt_at, ta.agent_finished_at)
    assert overlap == pytest.approx(120)


def test_g9_tooling_events_and_initiators():
    b = g("g9_tooling.jsonl")
    ev = [(e.data["category"], e.data["name"], e.data["initiator"], e.data["rule"])
          for e in b.events if e.kind == "tooling"]
    assert ("skill", "superpowers:brainstorming", "human", "claude:command-name") in ev
    assert ("plugin", "superpowers", "human", "claude:command-name") in ev
    assert ("skill", "mattpocock-skills:tdd", "agent", "claude:Skill") in ev
    assert ("mcp", "linear-server", "agent", "claude:mcp") in ev
    assert ("skill", "review-loop", "agent", "claude:attributionSkill") in ev
    # built-in CLI commands are session plumbing, not tooling
    assert not any(x[1] == "clear" for x in ev)
    assert [e.data["name"] for e in b.events if e.kind == "command_builtin"] == ["clear"]
    # attributionSkill for a skill already invoked via the Skill tool in the same turn is deduped
    assert sum(1 for x in ev if x[1] == "mattpocock-skills:tdd" and x[0] == "skill") == 1
    # the trailing /clear has no agent work but still is a turn
    assert b.turns[-1].prompt_kind == "command" and b.turns[-1].agent_started_at is None
    bash = next(c for c in b.tool_calls if c.name == "Bash")
    assert bash.bash_intent == "test"
    assert bash.initiator == "human" and bash.initiator_rule.startswith("3:prompt:")
    first_turn_tools = [c for c in b.tool_calls if c.turn_idx == 0]
    assert not first_turn_tools
