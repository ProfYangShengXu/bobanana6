"""Error classification: the §5.8 table and the two ordering disciplines."""

from __future__ import annotations

import pytest

from bobanana6.agentloop.errors import (
    REASON_ACTION,
    BobananaAction,
    FailoverReason,
    classify_error,
    progress_ruler,
)


class ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ReadTimeout(Exception):
    pass


def classify(status: int | None = None, message: str = ""):
    return classify_error(status_code=status, message=message)


def test_reason_action_table_has_the_twenty_two_rows() -> None:
    assert len(REASON_ACTION) == 22
    assert set(REASON_ACTION) == {reason for reason in FailoverReason if reason is not FailoverReason.UNKNOWN}
    assert set(REASON_ACTION.values()) == set(BobananaAction)


@pytest.mark.parametrize(
    ("reason", "action"),
    [
        (FailoverReason.RATE_LIMIT, BobananaAction.RETRY_LOCAL),
        (FailoverReason.OVERLOADED, BobananaAction.RETRY_LOCAL),
        (FailoverReason.SERVER_ERROR, BobananaAction.RETRY_LOCAL),
        (FailoverReason.TIMEOUT, BobananaAction.RETRY_LOCAL),
        (FailoverReason.UPSTREAM_RATE_LIMIT, BobananaAction.RETRY_LOCAL),
        (FailoverReason.CONTEXT_OVERFLOW, BobananaAction.RESHARD_TASK),
        (FailoverReason.PAYLOAD_TOO_LARGE, BobananaAction.RESHARD_TASK),
        (FailoverReason.TRUNCATED, BobananaAction.RESHARD_TASK),
        (FailoverReason.REPETITION, BobananaAction.RESHARD_TASK),
        (FailoverReason.THINKING_EXHAUSTED, BobananaAction.RESHARD_TASK),
        (FailoverReason.FORMAT_ERROR, BobananaAction.FIX_CONSTRAINT),
        (FailoverReason.ROLE_ALTERNATION, BobananaAction.FIX_CONSTRAINT),
        (FailoverReason.EMPTY_RESPONSE, BobananaAction.FIX_CONSTRAINT),
        (FailoverReason.INVALID_BODY, BobananaAction.FIX_CONSTRAINT),
        (FailoverReason.MODEL_NOT_FOUND, BobananaAction.SWITCH_SOURCE),
        (FailoverReason.MODEL_ENTITLEMENT, BobananaAction.SWITCH_SOURCE),
        (FailoverReason.POLICY_BLOCKED, BobananaAction.SWITCH_SOURCE),
        (FailoverReason.UPSTREAM_BLOCKED, BobananaAction.SWITCH_SOURCE),
        (FailoverReason.INTERPRETER_SHUTDOWN, BobananaAction.ABORT),
        (FailoverReason.BILLING, BobananaAction.ABORT),
        (FailoverReason.AUTH_PERMANENT, BobananaAction.ABORT),
        (FailoverReason.CONTENT_POLICY_BLOCKED, BobananaAction.ABORT),
    ],
)
def test_mapping_table_row_by_row(reason: FailoverReason, action: BobananaAction) -> None:
    assert REASON_ACTION[reason] is action


# --------------------------------------------------------------------------- #
# discipline (1): 413 is a byte problem, overflow is a token problem
# --------------------------------------------------------------------------- #


def test_413_is_a_byte_problem_even_with_overflow_wording() -> None:
    result = classify(413, "context length exceeded")
    assert result.reason == "payload_too_large"
    assert progress_ruler(FailoverReason(result.reason)) == "bytes"


def test_context_overflow_uses_the_token_ruler() -> None:
    result = classify(400, "context length exceeded")
    assert result.reason == "context_overflow"
    assert progress_ruler(FailoverReason(result.reason)) == "tokens"


def test_progress_ruler_is_none_for_a_size_neutral_failure() -> None:
    assert progress_ruler(FailoverReason.FORMAT_ERROR) == "none"


# --------------------------------------------------------------------------- #
# discipline (2): priority is load-bearing
# --------------------------------------------------------------------------- #


def test_429_context_length_becomes_reshard_not_retry() -> None:
    result = classify(429, "context length exceeded")
    assert result.reason == "context_overflow"
    assert result.action is BobananaAction.RESHARD_TASK


def test_429_output_limit_becomes_reshard() -> None:
    result = classify(429, "maximum output tokens exceeded")
    assert result.reason == "context_overflow"
    assert result.action is BobananaAction.RESHARD_TASK


def test_429_plain_rate_limit_stays_retry_local() -> None:
    result = classify(429, "rate limit exceeded")
    assert result.reason == "rate_limit"
    assert result.action is BobananaAction.RETRY_LOCAL


def test_429_overloaded_stays_overloaded() -> None:
    assert classify(429, "server overloaded").reason == "overloaded"


def test_413_precedes_the_generic_4xx_branch() -> None:
    assert classify(413, "request too large").reason == "payload_too_large"


def test_400_validation_precedes_overflow() -> None:
    result = classify(400, "unsupported parameter: 'max_tokens'")
    assert result.reason == "format_error"
    assert result.action is BobananaAction.FIX_CONSTRAINT


def test_400_context_length_is_overflow_not_format_error() -> None:
    assert classify(400, "context window exceeded").reason == "context_overflow"


def test_5xx_overflow_precedes_server_error() -> None:
    assert classify(500, "context length exceeded").reason == "context_overflow"


def test_5xx_overload_vs_plain_server_error() -> None:
    assert classify(503, "overloaded").reason == "overloaded"
    assert classify(500, "internal error").reason == "server_error"


def test_404_model_not_found_vs_entitlement() -> None:
    assert classify(404, "model not found").reason == "model_not_found"
    assert classify(404, "not entitled to this model").reason == "model_entitlement"


def test_403_content_policy_beats_auth() -> None:
    assert classify(403, "content policy violation").reason == "content_policy_blocked"
    assert classify(403, "invalid api key").reason == "auth_permanent"


def test_402_billing_aborts() -> None:
    result = classify(402, "insufficient credits")
    assert result.reason == "billing"
    assert result.action is BobananaAction.ABORT
    assert result.retryable is False


# --------------------------------------------------------------------------- #
# status-less path, transport, unknown
# --------------------------------------------------------------------------- #


def test_statusless_message_rules() -> None:
    assert classify(None, "rate limit exceeded").reason == "rate_limit"
    assert classify(None, "interpreter shutdown").reason == "interpreter_shutdown"
    assert classify(None, "model not found").reason == "model_not_found"


def test_transport_exception_is_timeout() -> None:
    result = classify_error(ReadTimeout("read timed out"))
    assert result.reason == "timeout"
    assert result.action is BobananaAction.RETRY_LOCAL


def test_unknown_is_retryable_and_does_not_guess() -> None:
    result = classify(None, "something entirely unfamiliar")
    assert result.reason == "unknown"
    assert result.action is BobananaAction.RETRY_LOCAL
    assert result.retryable is True


def test_retryable_is_false_exactly_for_abort() -> None:
    assert classify(402, "").retryable is False  # billing
    assert classify(401, "").retryable is False  # auth_permanent
    assert classify(429, "rate limit").retryable is True
    assert classify(413, "").retryable is True  # reshard, not abort


def test_should_compress_only_for_the_overflow_family() -> None:
    assert classify(413, "").should_compress is True
    assert classify(400, "context length exceeded").should_compress is True
    assert classify(400, "bad format").should_compress is False


def test_cause_chain_captures_the_lower_level_exceptions() -> None:
    try:
        try:
            raise ValueError("inner boom")
        except ValueError as inner:
            raise RuntimeError("outer boom") from inner
    except RuntimeError as outer:
        result = classify_error(outer)
    assert any("ValueError: inner boom" in entry for entry in result.cause_chain)
    assert result.cause_chain[0] == "RuntimeError: outer boom"


def test_status_code_and_message_are_read_off_the_exception() -> None:
    result = classify_error(ApiError("context length exceeded", status_code=429))
    assert result.reason == "context_overflow"
    assert result.detail == "context length exceeded"
