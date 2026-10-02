"""Tools: strict parsing, synthesized refusals, guardrails, call-level concurrency."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.tools import (
    GuardrailConfig,
    ToolCall,
    ToolCallError,
    ToolCallGuardrailController,
    ToolResult,
    blocked_tool_result,
    call_signature,
    invalid_arguments_result,
    order_results,
    parse_tool_arguments,
    plan_tool_segments,
)


def read(call_id: str = "1", name: str = "read", **arguments: object) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=arguments, effect="read")


def write(call_id: str = "w", name: str = "write_file", **arguments: object) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=arguments, effect="write", targets=tuple(str(a) for a in arguments.values()))


# --------------------------------------------------------------------------- #
# strict parsing -- never repair
# --------------------------------------------------------------------------- #


def test_valid_arguments_parse() -> None:
    assert parse_tool_arguments('{"path": "a.csv"}') == {"path": "a.csv"}


@pytest.mark.parametrize("raw", ["{not json", "", "[]", "42", "null", None])
def test_bad_arguments_are_rejected_not_repaired(raw: object) -> None:
    with pytest.raises(ToolCallError):
        parse_tool_arguments(raw)


def test_invalid_arguments_result_is_a_legal_tool_result() -> None:
    result = invalid_arguments_result("call-1")
    assert result.ok is False and result.blocked is False
    assert result.error_type == "invalid_tool_arguments"
    assert result.call_id == "call-1"


def test_blocked_result_is_synthesized_so_no_call_dangles() -> None:
    result = blocked_tool_result("call-2", "policy")
    assert result.blocked is True and result.ok is False
    assert result.call_id == "call-2"


# --------------------------------------------------------------------------- #
# call-level concurrency
# --------------------------------------------------------------------------- #


def test_reads_batch_into_a_parallel_segment() -> None:
    segments = plan_tool_segments((read("1"), read("2")))
    assert len(segments) == 1
    assert segments[0][0] == "parallel"
    assert [call.call_id for call in segments[0][1]] == ["1", "2"]


def test_a_write_is_a_barrier() -> None:
    calls = (read("1"), read("2"), write("w"), read("3"), read("4"))
    segments = plan_tool_segments(calls)
    kinds = [kind for kind, _ in segments]
    assert kinds[0] == "parallel"
    assert any(kind == "sequential" and any(call.effect == "write" for call in group) for kind, group in segments)
    # Order is preserved: a later call never crosses an earlier barrier.
    flat = [call.call_id for _, group in segments for call in group]
    assert flat == ["1", "2", "w", "3", "4"]


def test_a_lone_read_demotes_to_sequential() -> None:
    segments = plan_tool_segments((read("1"),))
    assert segments[0][0] == "sequential"


def test_results_are_written_back_in_call_order() -> None:
    calls = (read("1"), read("2"), read("3"))
    results = (
        ToolResult(call_id="3", ok=True, content="three"),
        ToolResult(call_id="1", ok=True, content="one"),
    )
    ordered = order_results(calls, results)
    assert [result.call_id for result in ordered] == ["1", "2", "3"]
    assert ordered[1].blocked is True  # the missing call still gets a legal row
    assert ordered[1].content == "no result was produced"


# --------------------------------------------------------------------------- #
# signatures and guardrails
# --------------------------------------------------------------------------- #


def test_signature_is_content_based_and_key_order_insensitive() -> None:
    a = ToolCall(call_id="1", name="read", arguments={"p": "x", "q": 1})
    b = ToolCall(call_id="2", name="read", arguments={"q": 1, "p": "x"})
    other = ToolCall(call_id="3", name="read", arguments={"p": "y", "q": 1})
    assert call_signature(a) == call_signature(b)
    assert call_signature(a) != call_signature(other)


def test_reset_for_turn_clears_thresholds() -> None:
    controller = ToolCallGuardrailController(GuardrailConfig(repeat_block_after=1))
    call = read()
    controller.observe(call, ToolResult(call_id="1", ok=False, content="boom"))
    assert controller.before_call(call) is not None
    controller.reset_for_turn()
    assert controller.before_call(call) is None  # ★ per-turn state must not leak


def test_repeated_identical_failure_is_blocked() -> None:
    controller = ToolCallGuardrailController(GuardrailConfig(repeat_block_after=2, same_tool_block_after=99))
    call = read()
    failure = ToolResult(call_id="1", ok=False, content="boom")
    controller.observe(call, failure)
    controller.observe(call, failure)
    decision = controller.before_call(call)
    assert decision is not None and decision.error_type == "repeat_failure"
    assert controller.halted is True


def test_same_tool_failure_is_blocked_across_different_arguments() -> None:
    controller = ToolCallGuardrailController(GuardrailConfig(repeat_block_after=99, same_tool_block_after=2))
    first = read("1", path="a")
    second = read("2", path="b")
    controller.observe(first, ToolResult(call_id="1", ok=False, content="boom"))
    controller.observe(second, ToolResult(call_id="2", ok=False, content="boom"))
    decision = controller.before_call(read("3", path="c"))
    assert decision is not None and decision.error_type == "same_tool_failure"


def test_a_successful_write_resets_the_repeat_counter() -> None:
    controller = ToolCallGuardrailController(GuardrailConfig(repeat_block_after=2, same_tool_block_after=99))
    call = read()
    failure = ToolResult(call_id="1", ok=False, content="boom")
    controller.observe(call, failure)
    controller.observe(call, failure)
    controller.observe(write("w"), ToolResult(call_id="w", ok=True, content="written"))
    assert controller.before_call(call) is None  # a mutation makes the retry a new experiment


def test_per_turn_call_cap_blocks() -> None:
    controller = ToolCallGuardrailController(GuardrailConfig(max_calls_per_turn=1))
    assert controller.before_call(read("1")) is None
    decision = controller.before_call(read("2"))
    assert decision is not None and decision.error_type == "loop_cap"


def test_guardrail_config_validation() -> None:
    with pytest.raises(ValueError):
        GuardrailConfig(repeat_block_after=-1)
