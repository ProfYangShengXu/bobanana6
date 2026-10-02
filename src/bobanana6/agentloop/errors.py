"""Error classification: map a low-level exception to a :class:`BobananaAction`.

The *shape* is forked from Hermes ``agent/error_classifier.py`` (MIT,
NousResearch/hermes-agent): a priority-ordered stage pipeline where the first
stage that matches wins. Provider-specific branches are dropped (FORK-NOTES §C);
only the reason -> action mapping this project needs is kept.

Two ordering rules are load-bearing and are asserted by tests (实现规格 §5.8):

  1. The scoring ruler is per scenario: a 413 is a *byte* problem while a
     context overflow is a *token / message-count* problem. They must never
     collapse into one reason, or "no progress" gets measured with the wrong
     ruler and the loop reports none forever.
  2. Priority is load-bearing: 413 precedes generic 4xx, and a context-length
     error -- including one a provider wraps in a 429 or a 5xx -- precedes the
     non-retryable client errors. A 429 that is really an output-limit error
     must become ``RESHARD_TASK``, not ``RETRY_LOCAL``; getting this backwards
     makes the system retry a full context forever.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "PROGRESS_RULER",
    "REASON_ACTION",
    "BobananaAction",
    "ClassifiedError",
    "FailoverReason",
    "classify_error",
    "progress_ruler",
]


class BobananaAction(StrEnum):
    """What the caller should *do* about a failure (实现规格 §5.8)."""

    RETRY_LOCAL = "retry_local"  # same worker, back off and retry
    RESHARD_TASK = "reshard_task"  # ★ back to the splitter: the block is too big/hard
    FIX_CONSTRAINT = "fix_constraint"  # ★ request shape is wrong: fix constraint/message
    SWITCH_SOURCE = "switch_source"  # switch worker / model / provider
    ABORT = "abort"  # stop, do not retry


class FailoverReason(StrEnum):
    """The failure identities the mapping table is written against.

    Names come from the fork's ``FailoverReason``; the spec's table uses the
    project's normalised subset. ``UNKNOWN`` exists so classification is total;
    it is deliberately *not* in :data:`REASON_ACTION`.
    """

    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    UPSTREAM_RATE_LIMIT = "upstream_rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    TRUNCATED = "truncated"
    REPETITION = "repetition"
    THINKING_EXHAUSTED = "thinking_exhausted"
    FORMAT_ERROR = "format_error"
    ROLE_ALTERNATION = "role_alternation"
    EMPTY_RESPONSE = "empty_response"
    INVALID_BODY = "invalid_body"
    MODEL_NOT_FOUND = "model_not_found"
    MODEL_ENTITLEMENT = "model_entitlement"
    POLICY_BLOCKED = "policy_blocked"
    UPSTREAM_BLOCKED = "upstream_blocked"
    INTERPRETER_SHUTDOWN = "interpreter_shutdown"
    BILLING = "billing"
    AUTH_PERMANENT = "auth_permanent"
    CONTENT_POLICY_BLOCKED = "content_policy_blocked"
    UNKNOWN = "unknown"


#: The mapping table from 实现规格 §5.8, verbatim -- do not improvise here.
REASON_ACTION: Mapping[FailoverReason, BobananaAction] = {
    FailoverReason.RATE_LIMIT: BobananaAction.RETRY_LOCAL,
    FailoverReason.OVERLOADED: BobananaAction.RETRY_LOCAL,
    FailoverReason.SERVER_ERROR: BobananaAction.RETRY_LOCAL,
    FailoverReason.TIMEOUT: BobananaAction.RETRY_LOCAL,
    FailoverReason.UPSTREAM_RATE_LIMIT: BobananaAction.RETRY_LOCAL,
    FailoverReason.CONTEXT_OVERFLOW: BobananaAction.RESHARD_TASK,
    FailoverReason.PAYLOAD_TOO_LARGE: BobananaAction.RESHARD_TASK,
    FailoverReason.TRUNCATED: BobananaAction.RESHARD_TASK,
    FailoverReason.REPETITION: BobananaAction.RESHARD_TASK,
    FailoverReason.THINKING_EXHAUSTED: BobananaAction.RESHARD_TASK,
    FailoverReason.FORMAT_ERROR: BobananaAction.FIX_CONSTRAINT,
    FailoverReason.ROLE_ALTERNATION: BobananaAction.FIX_CONSTRAINT,
    FailoverReason.EMPTY_RESPONSE: BobananaAction.FIX_CONSTRAINT,
    FailoverReason.INVALID_BODY: BobananaAction.FIX_CONSTRAINT,
    FailoverReason.MODEL_NOT_FOUND: BobananaAction.SWITCH_SOURCE,
    FailoverReason.MODEL_ENTITLEMENT: BobananaAction.SWITCH_SOURCE,
    FailoverReason.POLICY_BLOCKED: BobananaAction.SWITCH_SOURCE,
    FailoverReason.UPSTREAM_BLOCKED: BobananaAction.SWITCH_SOURCE,
    FailoverReason.INTERPRETER_SHUTDOWN: BobananaAction.ABORT,
    FailoverReason.BILLING: BobananaAction.ABORT,
    FailoverReason.AUTH_PERMANENT: BobananaAction.ABORT,
    FailoverReason.CONTENT_POLICY_BLOCKED: BobananaAction.ABORT,
}

#: What an unclassifiable failure does. Hermes retries ``unknown`` with backoff,
#: and the retry budget (agentloop/loop, step 21) is the ceiling. Keeping that
#: default means an unrecognised error cannot become a silent abort.
_UNKNOWN_ACTION = BobananaAction.RETRY_LOCAL

#: Discipline (1) as data: which ruler measures progress for a reason. A 413 is
#: counted in bytes; overflow/truncation in tokens. ``none`` means "this failure
#: says nothing about size".
#: Reader: agentloop/loop.py's no-progress check (step 21, not built yet).
PROGRESS_RULER: Mapping[FailoverReason, str] = {
    FailoverReason.PAYLOAD_TOO_LARGE: "bytes",
    FailoverReason.CONTEXT_OVERFLOW: "tokens",
    FailoverReason.TRUNCATED: "tokens",
}

_COMPRESSIBLE = frozenset(
    {FailoverReason.CONTEXT_OVERFLOW, FailoverReason.PAYLOAD_TOO_LARGE, FailoverReason.TRUNCATED}
)


def progress_ruler(reason: FailoverReason) -> str:
    """The ruler to score "did we make progress" with for this reason."""
    return PROGRESS_RULER.get(reason, "none")


@dataclass(frozen=True)
class ClassifiedError:
    """Structured classification: the action plus the evidence for it."""

    reason: str
    action: BobananaAction
    retryable: bool
    should_compress: bool
    detail: str
    cause_chain: tuple[str, ...] = ()


# -- signal vocabulary (lowercased substrings, Hermes-style) --------------- #

_OVERFLOW_SIGNALS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "reduce the length",
    "maximum number of tokens",
)
_OUTPUT_LIMIT_SIGNALS = (
    "output limit",
    "max output",
    "maximum output",
    "max_tokens",
    "max completion",
    "finish_reason=length",
    "finish reason: length",
    "length limit",
)
_VALIDATION_SIGNALS = (
    "unsupported parameter",
    "unexpected keyword",
    "invalid type",
    "is not supported",
    "invalid_request_error",
)
_ROLE_SIGNALS = ("role alternation", "roles must alternate", "same role", "must alternate")
_EMPTY_SIGNALS = ("empty response", "empty body", "no content", "returned no content")
_INVALID_BODY_SIGNALS = ("invalid body", "invalid_request_body", "invalid request body", "malformed message")
_REPETITION_SIGNALS = ("repetition", "repeating", "degenerate")
_THINKING_SIGNALS = ("thinking_exhausted", "reasoning exhausted", "ran out of reasoning")
_OVERLOADED_SIGNALS = ("overloaded", "over capacity", "at capacity", "busy", "overloaded_error")
_RATE_LIMIT_SIGNALS = ("rate limit", "rate_limit", "too many requests", "throttl", "quota exceeded")
_UPSTREAM_SIGNALS = ("upstream",)
_MODEL_NOT_FOUND_SIGNALS = ("model not found", "does not exist", "unknown model", "no such model")
_ENTITLEMENT_SIGNALS = ("entitlement", "not available for", "no access to", "not entitled")
_POLICY_SIGNALS = ("policy", "privacy", "account data")
_UPSTREAM_BLOCKED_SIGNALS = ("upstream_blocked", "upstream blocked", "waf", "cdn")
_CONTENT_POLICY_SIGNALS = ("content policy", "safety", "content_filter", "flagged")
_BILLING_SIGNALS = ("billing", "insufficient", "payment required", "credits", "balance")
_AUTH_SIGNALS = ("auth", "unauthorized", "forbidden", "api key", "invalid key")
_INTERPRETER_SIGNALS = ("interpreter", "shutdown", "process exited")
_TIMEOUT_SIGNALS = ("timeout", "timed out")
_FORMAT_SIGNALS = ("format", "bad request", "malformed request")


def _has(message: str, signals: Sequence[str]) -> bool:
    return any(signal in message for signal in signals)


def _is_overflow(message: str) -> bool:
    return _has(message, _OVERFLOW_SIGNALS) or _has(message, _OUTPUT_LIMIT_SIGNALS)


def _status_of(error: Any) -> int | None:
    status = getattr(error, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status


def _message_of(error: Any) -> str:
    if error is None:
        return ""
    message = getattr(error, "message", None)
    if not isinstance(message, str) or not message:
        message = str(error)
    return message


def _cause_chain(error: Any, limit: int = 8) -> tuple[str, ...]:
    """The lower-level exceptions, as ``Type: message`` strings.

    This is evidence, not a conclusion: it is what the caller shows when it has
    to explain *why* a classification was chosen.
    """
    chain: list[str] = []
    seen: set[int] = set()
    current = error
    while current is not None and len(chain) < limit and id(current) not in seen:
        seen.add(id(current))
        message = _message_of(current)
        entry = f"{type(current).__name__}: {message}" if message else type(current).__name__
        if entry not in chain:
            chain.append(entry)
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return tuple(chain)


def _by_transport(error: Any, message: str) -> FailoverReason | None:
    name = type(error).__name__ if error is not None else ""
    if "Timeout" in name or "Connection" in name or _has(message, _TIMEOUT_SIGNALS):
        return FailoverReason.TIMEOUT
    return None


def _by_status(status: int, message: str) -> FailoverReason | None:
    # (2) Priority is load-bearing -- the branches below are ordered, not equal.
    if status == 413:
        return FailoverReason.PAYLOAD_TOO_LARGE  # byte ruler, before generic 4xx
    if status == 429:
        # Overflow disguised as throttling must not become RETRY_LOCAL.
        if _is_overflow(message):
            return FailoverReason.CONTEXT_OVERFLOW
        if _has(message, _OVERLOADED_SIGNALS):
            return FailoverReason.OVERLOADED
        if _has(message, _UPSTREAM_SIGNALS):
            return FailoverReason.UPSTREAM_RATE_LIMIT
        return FailoverReason.RATE_LIMIT
    if status in (400, 422):
        if _has(message, _ROLE_SIGNALS):
            return FailoverReason.ROLE_ALTERNATION
        if _has(message, _INVALID_BODY_SIGNALS):
            return FailoverReason.INVALID_BODY
        if _has(message, _EMPTY_SIGNALS):
            return FailoverReason.EMPTY_RESPONSE
        # A validation rejection mentioning max_tokens is a request-shape error,
        # not an overflow (Hermes: validation precedes overflow on 400).
        if _has(message, _VALIDATION_SIGNALS):
            return FailoverReason.FORMAT_ERROR
        if _is_overflow(message):
            return FailoverReason.CONTEXT_OVERFLOW
        if _has(message, _REPETITION_SIGNALS):
            return FailoverReason.REPETITION
        if _has(message, _THINKING_SIGNALS):
            return FailoverReason.THINKING_EXHAUSTED
        return FailoverReason.FORMAT_ERROR
    if status in (401, 403):
        if _has(message, _CONTENT_POLICY_SIGNALS):
            return FailoverReason.CONTENT_POLICY_BLOCKED
        if _has(message, _UPSTREAM_BLOCKED_SIGNALS):
            return FailoverReason.UPSTREAM_BLOCKED
        if _has(message, _POLICY_SIGNALS):
            return FailoverReason.POLICY_BLOCKED
        return FailoverReason.AUTH_PERMANENT
    if status == 402:
        return FailoverReason.BILLING
    if status == 404:
        if _has(message, _ENTITLEMENT_SIGNALS):
            return FailoverReason.MODEL_ENTITLEMENT
        return FailoverReason.MODEL_NOT_FOUND
    if status == 408:
        return FailoverReason.TIMEOUT
    if 500 <= status < 600:
        if _is_overflow(message):
            return FailoverReason.CONTEXT_OVERFLOW  # overflow arriving as 5xx
        if status in (503, 529) or _has(message, _OVERLOADED_SIGNALS):
            return FailoverReason.OVERLOADED
        return FailoverReason.SERVER_ERROR
    if 400 <= status < 500:
        return FailoverReason.FORMAT_ERROR
    return None


def _by_message(message: str) -> FailoverReason | None:
    """Status-less path: ordered rules, overload before rate limit, overflow last."""
    if _has(message, _THINKING_SIGNALS):
        return FailoverReason.THINKING_EXHAUSTED
    if _has(message, _REPETITION_SIGNALS):
        return FailoverReason.REPETITION
    if _has(message, _OVERLOADED_SIGNALS):
        return FailoverReason.OVERLOADED
    if _has(message, _INTERPRETER_SIGNALS):
        return FailoverReason.INTERPRETER_SHUTDOWN
    if _has(message, _BILLING_SIGNALS):
        return FailoverReason.BILLING
    if _has(message, _CONTENT_POLICY_SIGNALS):
        return FailoverReason.CONTENT_POLICY_BLOCKED
    if _has(message, _UPSTREAM_BLOCKED_SIGNALS):
        return FailoverReason.UPSTREAM_BLOCKED
    if _has(message, _UPSTREAM_SIGNALS) and _has(message, _RATE_LIMIT_SIGNALS):
        return FailoverReason.UPSTREAM_RATE_LIMIT
    if _has(message, _RATE_LIMIT_SIGNALS):
        return FailoverReason.RATE_LIMIT
    if _has(message, _AUTH_SIGNALS):
        return FailoverReason.AUTH_PERMANENT
    if _has(message, _ENTITLEMENT_SIGNALS):
        return FailoverReason.MODEL_ENTITLEMENT
    if _has(message, _MODEL_NOT_FOUND_SIGNALS):
        return FailoverReason.MODEL_NOT_FOUND
    if _has(message, _POLICY_SIGNALS):
        return FailoverReason.POLICY_BLOCKED
    if _has(message, _ROLE_SIGNALS):
        return FailoverReason.ROLE_ALTERNATION
    if _has(message, _INVALID_BODY_SIGNALS):
        return FailoverReason.INVALID_BODY
    if _has(message, _EMPTY_SIGNALS):
        return FailoverReason.EMPTY_RESPONSE
    if _has(message, _TIMEOUT_SIGNALS):
        return FailoverReason.TIMEOUT
    if _is_overflow(message):
        return FailoverReason.CONTEXT_OVERFLOW
    if _has(message, _FORMAT_SIGNALS):
        return FailoverReason.FORMAT_ERROR
    return None


def classify_error(
    error: Any = None,
    *,
    status_code: int | None = None,
    message: str | None = None,
) -> ClassifiedError:
    """Classify one failure. Pure and deterministic.

    ``status_code`` / ``message`` override what is read off ``error``, which is
    what makes the ordering rules testable without a provider SDK.
    """
    status = status_code if status_code is not None else _status_of(error)
    text = (message if message is not None else _message_of(error)).lower()
    reason = (
        (_by_status(status, text) if status is not None else None)
        or _by_transport(error, text)
        or _by_message(text)
        or FailoverReason.UNKNOWN
    )
    action = REASON_ACTION.get(reason, _UNKNOWN_ACTION)
    return ClassifiedError(
        reason=reason.value,
        action=action,
        retryable=action is not BobananaAction.ABORT,
        should_compress=reason in _COMPRESSIBLE,
        detail=message if message is not None else _message_of(error),
        cause_chain=_cause_chain(error),
    )
