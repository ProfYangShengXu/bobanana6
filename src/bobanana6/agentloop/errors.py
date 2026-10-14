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
    "DECLARED_REASON_ATTRIBUTE",
    "PROGRESS_RULER",
    "REASON_ACTION",
    "SERVER_SIDE_REASONS",
    "BobananaAction",
    "ClassifiedError",
    "FailoverReason",
    "classify_error",
    "declared_failure",
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
#: ★★ **服务端类的失败**：503/529（服务太忙）、5xx、限速、网络超时 —— 它们的共同点是
#: **模型改不了**：重发的是一模一样的东西，服务器还是忙。
#: ⇒ 处置是**等一等再打**（退避重发），不是"把报错文本灌进上下文让它反思"。
#: ★ 两个读者：`recovery.decide_recovery`（别把这类送进"拿着事实反思"那支）、
#:   `loop`（这类才退避；其余的回灌原因）。判据是**错误身份**，不是措辞猜测。
#: 实测（qwen4b14）：T-01/T-02 被 `HTTP 503 System is too busy now` 打挂，走了反思那支 ⇒
#: 注入了原因、**没退避**、两次瞬间用完 ⇒ 块被判死。
SERVER_SIDE_REASONS: frozenset[str] = frozenset(
    str(reason)
    for reason in (
        FailoverReason.OVERLOADED,
        FailoverReason.SERVER_ERROR,
        FailoverReason.RATE_LIMIT,
        FailoverReason.UPSTREAM_RATE_LIMIT,
        FailoverReason.TIMEOUT,
    )
)

_UNKNOWN_ACTION = BobananaAction.RETRY_LOCAL

#: Discipline (1) as data: which ruler measures progress for a reason. A 413 is
#: counted in bytes; overflow/truncation in tokens. ``none`` means "this failure
#: says nothing about size".
#: ★ `#41`: 仍然**没有读者** —— 决定与理由写在 `agentloop/README.md` 的接线点清单里
#:   （`truncated` / `repetition` 现在一分类就终止本块，没有"重试到耗尽"的场景；
#:   `REPETITION` 故意不进表：它说的是"在转圈"，与尺寸无关）。
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


#: The attribute an exception declares its own reason with.
#: ★ Read first: whoever raises a failure knows *why* it happened. Everything
#: below this line is inference -- from a class name, then from wording -- and
#: inference is what made the same exception classify two different ways
#: depending on whether the caller happened to include the class name in the
#: message (``CandidateFormatError`` matched ``_FORMAT_SIGNALS`` by coincidence,
#: a class named ``BadAnswer`` would not have).
DECLARED_REASON_ATTRIBUTE = "reason"


def declared_failure(error: Any) -> FailoverReason | BobananaAction | None:
    """What the raiser labelled itself with, or ``None`` if it did not label.

    The label may name a **reason** (the usual case) or an **action**. The action
    form exists because the 22-reason vocabulary has no entry for some failures --
    "the data source is unreachable" is not one of them -- and inventing a reason
    would put a lie in the field attribution reads. Naming the action says exactly
    what the raiser knows and nothing more.

    An unreadable label is *ignored* rather than honoured: a typo must not decide
    the recovery action. ``test_every_declared_reason_is_in_the_vocabulary`` is
    what keeps a typo from shipping in the first place.
    """
    label = getattr(error, DECLARED_REASON_ATTRIBUTE, None)
    if isinstance(label, (FailoverReason, BobananaAction)):
        return label
    if isinstance(label, str):
        for enum in (FailoverReason, BobananaAction):
            try:
                return enum(label)
            except ValueError:
                continue
    return None


def _by_type(error: Any) -> FailoverReason | None:
    """Fallback: the class **name**.

    Local exceptions declare a reason instead; this exists for exception types we
    do not own and cannot annotate.
    """
    name = type(error).__name__ if error is not None else ""
    if "Timeout" in name or "Connection" in name:
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

    ★ Priority, most authoritative first:

      1. what the exception **declares** (:data:`DECLARED_REASON_ATTRIBUTE`):
         a reason, or an action when no reason fits -- the raiser knows why it
         failed, so it gets to say so;
      2. the provider's **status code**, which is structured data from the other
         side and carries its own ordering rules (413 before generic 4xx, ...);
      3. the exception's **class name**;
      4. the **wording** of the message -- a fallback for third-party exceptions
         that cannot be annotated, kept because dropping it would lose the
         provider quirks the tables below encode;
      5. ``unknown``, which is retryable and never guesses a blame.

    Stages 3-4 are *fallbacks by construction*: they infer a cause from a name or
    from prose, so the same failure can classify differently depending on how the
    caller spelled the message. Anything local can and should declare instead.
    """
    status = status_code if status_code is not None else _status_of(error)
    text = (message if message is not None else _message_of(error)).lower()
    declared = declared_failure(error)
    if isinstance(declared, BobananaAction):
        # The label named an action, not a reason: keep the action, and say so in
        # ``reason`` instead of inventing a reason that would not be true.
        reason: FailoverReason | None = None
        action = declared
        reason_text = declared.value
    else:
        reason = (
            declared
            or (_by_status(status, text) if status is not None else None)
            or _by_type(error)
            or _by_message(text)
            or FailoverReason.UNKNOWN
        )
        action = REASON_ACTION.get(reason, _UNKNOWN_ACTION)
        reason_text = reason.value
    return ClassifiedError(
        reason=reason_text,
        action=action,
        retryable=action is not BobananaAction.ABORT,
        should_compress=reason in _COMPRESSIBLE if reason is not None else False,
        detail=message if message is not None else _message_of(error),
        cause_chain=_cause_chain(error),
    )
