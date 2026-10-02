"""Compression: threshold arithmetic, tool-pair integrity, strict shrink, recovery."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.compress import (
    TRUNCATION_MARKER,
    CompressionPolicy,
    compact,
    compression_threshold,
    estimate_tokens,
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
