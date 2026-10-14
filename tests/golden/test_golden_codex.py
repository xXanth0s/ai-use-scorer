"""Golden 3 (spec §6b): Codex usage dedupe + cumulative token_count never summed."""
from __future__ import annotations

import os

from scorer.parse_codex import parse

HERE = os.path.dirname(__file__)
G3 = os.path.join(HERE, "codex_golden3.jsonl")
G3_CUM = os.path.join(HERE, "codex_golden3_cumulative.jsonl")


def test_two_records_same_response_id_are_one_apicall():
    b = parse(G3)
    ids = [a.request_id for a in b.api_calls]
    assert ids.count("resp_golden3_A") == 1
    assert ids == ["resp_golden3_A", "resp_golden3_B"]
    a = b.api_calls[0]
    # cached_input_tokens is a subset of input_tokens
    assert (a.input_tok, a.cached_tok, a.output_tok) == (400, 600, 50)
    assert a.model == "gpt-6-astra" and a.effort == "high" and a.context_window == 258400
    # per-request sum, NOT the cumulative token_count totals (which would give 50+50+120)
    assert sum(x.output_tok for x in b.api_calls) == 120
    assert sum(x.input_tok + x.cached_tok for x in b.api_calls) == 2500


def test_cumulative_fallback_uses_differences_not_sums():
    b = parse(G3_CUM)
    assert [a.request_id for a in b.api_calls] == [f"{b.session.id}:1", f"{b.session.id}:2"]
    assert [a.output_tok for a in b.api_calls] == [50, 70]  # duplicate token_count emission ignored
    assert sum(a.input_tok + a.cached_tok for a in b.api_calls) == 2500


def test_golden3_turn_and_tool():
    b = parse(G3)
    assert len(b.turns) == 1  # environment_context injection is not a prompt
    t = b.turns[0]
    assert t.prompt_at == "2026-10-10T10:00:01+00:00"
    assert t.end_source == "task_complete" and t.agent_finished_at == "2026-10-10T10:00:08.100000+00:00"
    assert not t.interrupted
    (tc,) = b.tool_calls  # the `exec` wrapper is represented by its CommandExecution
    assert tc.tool_use_id == "exec-g3-1" and tc.kind == "bash" and tc.args_summary == "uv run pytest -q"
    assert tc.is_error and tc.has_result and tc.duration_ms == 1500
    assert tc.bash_intent == "test"
    assert (tc.initiator, tc.initiator_rule) == ("human", "3:prompt:run")
