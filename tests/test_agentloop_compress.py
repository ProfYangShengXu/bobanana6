"""Compression: threshold arithmetic, tool-pair integrity, strict shrink, recovery."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.compress import (
    TRUNCATION_MARKER,
    CompressionPolicy,
    compact,
    compression_threshold,
    estimate_tokens,
    message_sequence_problem,
    recoverable,
    sanitize_tool_pairs,
    truncate_tool_results,
)


def conversation(count: int = 12, size: int = 300) -> tuple[dict, ...]:
    rows: list[dict] = [{"role": "system", "content": "stable protocol " + "s" * size}]
    for index in range(count):
        rows.append({"role": "user", "content": f"message {index} " + "x" * size})
        rows.append({"role": "assistant", "content": f"reply {index} " + "y" * size})
    return tuple(rows)


# --------------------------------------------------------------------------- #
# threshold arithmetic
# --------------------------------------------------------------------------- #


def test_threshold_subtracts_the_output_reservation_first() -> None:
    """坑 4: a large max_tokens must not push us past the provider's limit."""
    policy = CompressionPolicy(context_length=1000, max_tokens=200, pct=0.5)
    assert compression_threshold(policy) == 400


def test_policy_rejects_an_output_reservation_larger_than_the_window() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        CompressionPolicy(context_length=100, max_tokens=100)


def test_policy_rejects_bad_ratios_and_caps() -> None:
    with pytest.raises(ValueError):
        CompressionPolicy(context_length=100, max_tokens=1, pct=0.0)
    with pytest.raises(ValueError):
        CompressionPolicy(context_length=100, max_tokens=1, keep_recent=-1)


def test_estimate_grows_with_content() -> None:
    assert estimate_tokens(({"role": "user", "content": "short"},)) < estimate_tokens(
        ({"role": "user", "content": "short " * 100},)
    )


# --------------------------------------------------------------------------- #
# tool-pair integrity (坑 12)
# --------------------------------------------------------------------------- #


def test_sanitize_drops_an_orphan_tool_result() -> None:
    messages = (
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
    )
    assert sanitize_tool_pairs(messages) == ({"role": "user", "content": "hi"},)


def test_sanitize_keeps_a_paired_tool_result() -> None:
    messages = (
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "result"},
    )
    assert len(sanitize_tool_pairs(messages)) == 2


def test_sanitize_strips_an_unanswered_tool_call() -> None:
    messages = (
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "let me look", "tool_calls": [{"id": "unanswered"}]},
    )
    cleaned = sanitize_tool_pairs(messages)
    assert cleaned[-1]["tool_calls"] == ()


# --------------------------------------------------------------------------- #
# truncation
# --------------------------------------------------------------------------- #


def test_truncate_caps_tool_results_and_keeps_the_original_for_recovery() -> None:
    original = {"role": "tool", "tool_call_id": "a", "content": "z" * 500}
    capped, retained = truncate_tool_results((original,), 100)
    assert len(capped[0]["content"]) < 500
    assert capped[0]["content"].endswith(TRUNCATION_MARKER)
    assert retained == (original,)


def test_truncate_leaves_short_results_alone() -> None:
    original = {"role": "tool", "tool_call_id": "a", "content": "ok"}
    capped, retained = truncate_tool_results((original,), 100)
    assert capped == (original,) and retained == ()


# --------------------------------------------------------------------------- #
# compaction
# --------------------------------------------------------------------------- #


def test_under_threshold_does_not_drop_anything() -> None:
    policy = CompressionPolicy(context_length=100000, max_tokens=1000)
    messages = conversation(count=2, size=10)
    result = compact(messages, policy)
    assert result.dropped == ()
    assert result.committed is True
    assert recoverable(result, messages)


def test_over_threshold_drops_the_oldest_unprotected_but_keeps_recent() -> None:
    policy = CompressionPolicy(context_length=200, max_tokens=20, pct=0.5, keep_recent=2)
    messages = conversation(count=10, size=100)
    result = compact(messages, policy)
    assert result.dropped, "the oldest messages should have been dropped"
    assert result.tokens_after < result.tokens_before
    assert recoverable(result, messages)
    # The most recent rows survive.
    assert messages[-1] in result.messages


def test_the_summary_is_traceable_to_the_dropped_messages() -> None:
    policy = CompressionPolicy(context_length=200, max_tokens=20, pct=0.5, keep_recent=2)
    messages = conversation(count=10, size=100)
    seen: list[tuple] = []

    def summarize(dropped: tuple) -> str:
        seen.append(dropped)
        return f"summary of {len(dropped)} messages"

    result = compact(messages, policy, summarize=summarize)
    assert seen and len(seen[0]) == len(result.dropped)
    assert result.summary is not None
    assert recoverable(result, messages)


def test_a_pinned_instruction_is_moved_to_the_tail() -> None:
    policy = CompressionPolicy(context_length=100000, max_tokens=100)
    pinned = {"role": "user", "content": "current task", "pinned": True}
    messages = ({"role": "system", "content": "sys"}, pinned, {"role": "user", "content": "later"})
    result = compact(messages, policy)
    assert result.messages[-1] is pinned or result.messages[-1].get("pinned") is True


def test_a_non_shrinking_compaction_is_refused() -> None:
    """坑 10: a compression that does not shrink must not be committed."""
    policy = CompressionPolicy(context_length=200, max_tokens=20, pct=0.5, keep_recent=2)
    messages = conversation(count=10, size=100)

    def bloated_summarize(dropped: tuple) -> str:
        return "z" * 100000

    result = compact(messages, policy, summarize=bloated_summarize)
    assert result.committed is False
    assert result.messages == messages
    assert result.shrank is False


def test_recoverable_is_content_based() -> None:
    policy = CompressionPolicy(context_length=200, max_tokens=20, pct=0.5, keep_recent=2)
    messages = conversation(count=10, size=100)
    result = compact(messages, policy)
    assert recoverable(result, messages)
    assert not recoverable(result, messages + ({"role": "user", "content": "brand new"},))


# --------------------------------------------------------------------------- #
# ★ 消息序列合规：assistant(tool_calls) → tool(id…) 的**断言**
#   （同一个不变量的三面：sanitize 修、message_sequence_problem 检、loop 建）
# --------------------------------------------------------------------------- #


def _round(call_ids: tuple[str, ...], results: tuple[str, ...]) -> tuple[dict, ...]:
    return (
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": "brief"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "sources_read", "arguments": "{}"}}
                for call_id in call_ids
            ],
        },
        *({"role": "tool", "tool_call_id": call_id, "content": text} for call_id, text in zip(call_ids, results)),
    )


def test_a_well_formed_tool_round_has_no_sequence_problem() -> None:
    assert message_sequence_problem(_round(("a", "b"), ("ra", "rb"))) == ""


def _old_shape(call_ids: tuple[str, ...], results: tuple[str, ...]) -> tuple[dict, ...]:
    """修复前的形态：只追加 tool 行，assistant(tool_calls) 那条缺席。"""
    rows = _round(call_ids, results)
    return (*rows[:2], *rows[3:])


def test_the_old_shape_is_exactly_the_400_the_endpoint_returns() -> None:
    """★ 修复前后都过不了的那一条：assistant 行缺席 ⇒ 每个 tool 行都是孤儿。"""
    assert "no preceding assistant row" in message_sequence_problem(_old_shape(("a",), ("ra",)))


def test_a_tool_row_whose_id_is_not_declared_is_named() -> None:
    broken = _round(("a", "b"), ("ra", "rb"))
    broken = (*broken[:3], {"role": "tool", "tool_call_id": "c", "content": "stray"})
    assert "is not declared by the preceding assistant row" in message_sequence_problem(broken)


def test_an_unanswered_declared_call_is_named() -> None:
    assert "unanswered tool call" in message_sequence_problem(_round(("a", "b"), ("ra",)))


def test_a_message_that_interrupts_a_tool_round_is_named() -> None:
    broken = (*_round(("a", "b"), ("ra", "rb"))[:3], {"role": "user", "content": "wait"})
    assert "interrupts a tool round" in message_sequence_problem(broken)


def test_compressing_a_real_tool_round_keeps_the_whole_chain() -> None:
    """★ 压缩路径：一串合规的工具轮过 `sanitize_tool_pairs` 之后仍然逐条配得上。

    （这正是旧形态的第二个症状：没有 assistant 行时，sanitizer 会把**所有** tool 行当孤儿丢掉。）
    """
    transcript = (
        *_round(("a", "b"), ("ra", "rb")),
        {"role": "user", "content": "next"},
        *_round(("c",), ("rc",)),
    )
    assert sanitize_tool_pairs(transcript) == transcript
    assert message_sequence_problem(sanitize_tool_pairs(transcript)) == ""
    # 旧形态（只有 tool 行）会被 san 掉 —— 这条是那次静默丢数据的证据
    assert sanitize_tool_pairs(_old_shape(("a",), ("ra",))) == (
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": "brief"},
    )


def _long_round(index: int, *, size: int = 400) -> tuple[dict, ...]:
    """One tool round big enough to matter for the threshold."""
    call_id = f"call-{index}"
    return (
        {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "sources_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": f"result {index} " + "z" * size},
    )


def test_compaction_drops_a_tool_round_whole_and_never_splits_it() -> None:
    """★ 压缩的丢弃以【工具轮】为单位：切在 assistant 与它的结果之间会留下孤儿 tool 行。

    旧行为按**下标**取前一半 —— 切点落在轮中间时，assistant 行被丢掉、它的 tool 行留下，
    下一个请求就变成端点那条 400 的形状。
    """
    rows: list[dict] = [{"role": "system", "content": "protocol"}, {"role": "user", "content": "brief"}]
    for index in range(4):
        rows.extend(_long_round(index))
    rows.append({"role": "user", "content": "next"})
    messages = tuple(rows)
    policy = CompressionPolicy(context_length=900, max_tokens=100, pct=0.5, tool_result_char_cap=0, keep_recent=2)
    result = compact(messages, policy)
    assert result.committed and result.dropped
    assert message_sequence_problem(result.messages) == ""
    kept_ids = {
        call["id"]
        for message in result.messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or ()
    }
    tool_ids = {row["tool_call_id"] for row in result.messages if row.get("role") == "tool"}
    assert tool_ids == kept_ids, (kept_ids, tool_ids)
    # 丢掉的也是整轮（每个被丢的 assistant 调用都有对应的 tool 行被丢）
    dropped_assistant = [m for m in result.dropped if m.get("role") == "assistant"]
    dropped_tools = [m for m in result.dropped if m.get("role") == "tool"]
    assert len(dropped_assistant) == len(dropped_tools)


def test_a_sanitizer_backstop_keeps_the_sequence_legal() -> None:
    """兜底：把一个**故意被切坏**的序列交给压缩的最后一步，出来仍是合法序列。"""
    broken = (
        {"role": "system", "content": "protocol"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "kept"}]},
        {"role": "tool", "tool_call_id": "kept", "content": "r"},
        {"role": "tool", "tool_call_id": "orphan", "content": "r"},
    )
    assert "is not declared by the preceding assistant row" in message_sequence_problem(broken)
    assert message_sequence_problem(sanitize_tool_pairs(broken)) == ""


# --------------------------------------------------------------------------- #
# block budget: the splitter's anchor (same ruler as the threshold)
# --------------------------------------------------------------------------- #


def test_the_block_budget_is_a_fraction_of_the_compression_threshold() -> None:
    """★★ 判据的锚：单块预算 = 阈值 × 余量，且与 `estimate_tokens` 同一把尺子。

    默认 0.7 ⇒ 块的输入落在阈值之内，不会一进门就触发压缩。
    """
    from bobanana6.agentloop.compress import CHARS_PER_SIZE_UNIT, block_char_budget

    policy = CompressionPolicy(context_length=32768, max_tokens=8192, pct=0.5)
    assert compression_threshold(policy) == 12288
    # 0.7 余量：预算严格小于阈值，且换算回 size unit 就是 0.7 × 阈值。
    assert block_char_budget(policy) == int(12288 * CHARS_PER_SIZE_UNIT * 0.7)
    assert block_char_budget(policy) == int(0.7 * compression_threshold(policy) * CHARS_PER_SIZE_UNIT)
    assert block_char_budget(policy, headroom=1.0) == compression_threshold(policy) * CHARS_PER_SIZE_UNIT


def test_the_estimator_and_the_budget_share_one_ruler() -> None:
    """同一段文本：预算消耗的 size unit 数 == `estimate_tokens` 数它的数。

    两侧都从 `CHARS_PER_SIZE_UNIT` 派生 ⇒ 改一处不会让它们悄悄漂开。
    """
    from bobanana6.agentloop.compress import CHARS_PER_SIZE_UNIT, block_char_budget

    policy = CompressionPolicy(context_length=1000, max_tokens=200)
    chars = block_char_budget(policy, headroom=1.0)
    assert estimate_tokens([{"role": "user", "content": "x" * chars}]) == 4 + chars // CHARS_PER_SIZE_UNIT


def test_a_degenerate_policy_yields_a_degenerate_budget() -> None:
    """★ 已知的配置陷阱：`--max-output >= --context-length` ⇒ 阈值塌成 0。

    预算跟着是 0 —— 调用方必须把它读成"没有可用预算"，**不是**"把每份源拆碎"。
    """
    from bobanana6.agentloop.compress import block_char_budget

    collapsed = CompressionPolicy(context_length=32769, max_tokens=32768)
    assert compression_threshold(collapsed) == 0
    assert block_char_budget(collapsed) == 0


def test_the_block_budget_rejects_a_bad_headroom() -> None:
    from bobanana6.agentloop.compress import block_char_budget

    policy = CompressionPolicy(context_length=1000, max_tokens=200)
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="headroom"):
            block_char_budget(policy, headroom=bad)


# --------------------------------------------------------------------------- #
# ★★ the two-level budget: read band + write-layer ceiling, one ruler
# --------------------------------------------------------------------------- #


def test_the_two_level_block_band_is_the_same_ruler_times_four_fractions() -> None:
    """★★ 两级预算的四个数都从 `compression_threshold` × `CHARS_PER_SIZE_UNIT` 派生。

    默认参数（32768 / 8192）下：threshold = 12288 units = 49152 字符
      读块输入 ∈ [0.6, 0.8] × 49152 = [29491, 39321]
      写块输入 ∈ [0.1, 0.3] × 49152 = [4915, 14745]（所有 part 的总和）
    """
    from bobanana6.agentloop.compress import (
        CHARS_PER_SIZE_UNIT,
        READ_BUDGET_HIGH,
        READ_BUDGET_LOW,
        WRITE_BUDGET_HIGH,
        WRITE_BUDGET_LOW,
        block_char_band,
    )

    policy = CompressionPolicy(context_length=32768, max_tokens=8192, pct=0.5)
    threshold_chars = compression_threshold(policy) * CHARS_PER_SIZE_UNIT
    assert threshold_chars == 49152
    band = block_char_band(policy)
    assert (band.read_min, band.read_max) == (
        int(READ_BUDGET_LOW * threshold_chars),
        int(READ_BUDGET_HIGH * threshold_chars),
    )
    assert (band.read_min, band.read_max) == (29491, 39321)
    assert (band.write_min, band.write_max) == (
        int(WRITE_BUDGET_LOW * threshold_chars),
        int(WRITE_BUDGET_HIGH * threshold_chars),
    )
    assert (band.write_min, band.write_max) == (4915, 14745)


def test_a_degenerate_policy_collapses_the_whole_band_to_zero() -> None:
    """阈值塌 0 ⇒ 四个数全 0，调用方读成"没有预算"（与单层预算同一条出口）。"""
    from bobanana6.agentloop.compress import block_char_band

    collapsed = CompressionPolicy(context_length=32769, max_tokens=32768)
    band = block_char_band(collapsed)
    assert (band.read_min, band.read_max, band.write_min, band.write_max) == (0, 0, 0, 0)
