"""Context compression: the minimal recoverable kernel.

FORK-NOTES §A/§C are explicit: copy only the small kernel and treat the rest of
Hermes' 10k-line compression subsystem as net liability. A worker's context is
naturally much smaller than a single agent running the whole task, so
micro-compaction, session rotation, watermarks and durable locks are not worth
their complexity here.

The kernel is:

    estimate budget -> per-tool-result char cap -> sanitize tool pairs
    -> threshold = (context_length - max_tokens) * pct
    -> over threshold: drop the oldest unprotected messages, optional one summary
    -> ★ refuse to commit if the result is not strictly smaller
    -> pin the current task instruction at the tail

Two fork pitfalls are load-bearing:

  * the threshold subtracts the output reservation **first** (坑 4), otherwise a
    large ``max_tokens`` hits a provider 400 before compression ever runs;
  * compression must be **recoverable** (架构设计 §8.3): an unrecoverable summary
    cuts the attribution chain, so the dropped/rewritten originals are kept in
    the result and :func:`recoverable` proves nothing was lost.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

__all__ = [
    "TRUNCATION_MARKER",
    "CompressionPolicy",
    "CompressionResult",
    "compact",
    "compression_threshold",
    "estimate_tokens",
    "message_sequence_problem",
    "recoverable",
    "sanitize_tool_pairs",
    "truncate_tool_results",
]

TRUNCATION_MARKER = "…[truncated]"
_SUMMARY_ROLE = "assistant"
_SUMMARY_MARKER = "…[summary of dropped messages]"


@dataclass(frozen=True)
class CompressionPolicy:
    context_length: int
    #: Output reservation, subtracted before the percentage is applied (坑 4).
    max_tokens: int
    pct: float = 0.5
    tool_result_char_cap: int = 2000
    keep_recent: int = 4

    def __post_init__(self) -> None:
        if self.context_length <= 0:
            raise ValueError("CompressionPolicy.context_length must be > 0")
        if self.max_tokens < 0 or self.max_tokens >= self.context_length:
            raise ValueError("CompressionPolicy.max_tokens must be >= 0 and < context_length")
        if not 0.0 < self.pct <= 1.0:
            raise ValueError("CompressionPolicy.pct must be in (0, 1]")
        if self.tool_result_char_cap < 0 or self.keep_recent < 0:
            raise ValueError("caps and keep_recent must be >= 0")


def compression_threshold(policy: CompressionPolicy) -> int:
    """★ Output reservation is subtracted before the percentage is applied."""
    return int((policy.context_length - policy.max_tokens) * policy.pct)


def digest(message: Mapping[str, Any]) -> str:
    payload = json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, sort_keys=True, ensure_ascii=True, default=str)


def estimate_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """A rough, deterministic size estimate -- never used to reset a breaker."""
    total = 0
    for message in messages:
        total += 4 + max(1, len(_text(message)) // 4)
    return total


@dataclass(frozen=True)
class CompressionResult:
    messages: tuple[Mapping[str, Any], ...]
    #: Whole messages removed, kept so the summary stays traceable to its source.
    dropped: tuple[Mapping[str, Any], ...]
    #: Pre-rewrite originals of messages whose content was truncated.
    retained: tuple[Mapping[str, Any], ...]
    summary: str | None
    tokens_before: int
    tokens_after: int
    committed: bool

    @property
    def shrank(self) -> bool:
        return self.tokens_after < self.tokens_before


def sanitize_tool_pairs(messages: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """Keep the call chain intact: no orphan tool result, no unanswered tool call.

    A single unpaired ``tool`` row makes the next provider request invalid, so the
    sanitizer runs before anything else.
    """
    declared: dict[str, int] = {}
    output: list[Mapping[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            calls = message.get("tool_calls") or ()
            ids = tuple(str(call.get("id", "")) for call in calls if isinstance(call, Mapping))
            for call_id in ids:
                declared[call_id] = len(output)
            output.append(message)
        elif role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            if call_id in declared:
                declared.pop(call_id)
                output.append(message)
            # else: orphan tool result -- dropped
        else:
            output.append(message)

    if declared:
        # Unanswered tool calls: strip exactly those ids so the assistant row is
        # not left dangling (a later request would be rejected outright).
        cleaned: list[Mapping[str, Any]] = []
        for message in output:
            calls = message.get("tool_calls")
            if message.get("role") == "assistant" and calls:
                remaining = tuple(
                    call
                    for call in calls
                    if isinstance(call, Mapping) and str(call.get("id", "")) not in declared
                )
                if remaining or _text(message):
                    cleaned.append({**message, "tool_calls": remaining})
                continue
            cleaned.append(message)
        output = cleaned
    return tuple(output)


def message_sequence_problem(messages: Sequence[Mapping[str, Any]]) -> str:
    """Why a strict OpenAI-compatible endpoint would refuse these messages (``""`` = it would not).

    The invariant ``sanitize_tool_pairs`` repairs, stated as a check:

    * every ``tool`` row answers a tool call declared by the assistant row **right
      before it** (the endpoints enforce "must be a response to a preceding message
      with ``tool_calls``");
    * the responses to one assistant row arrive contiguously -- a ``user`` /
      ``system`` / new ``assistant`` row in the middle leaves a dangling call;
    * the transcript never ends with an unanswered tool call.

    It lives next to the sanitizer on purpose: one invariant, one repair, one check.
    Tests and the acceptance probe use it to assert the sequence the loop builds; the
    loop itself does not need to call it (it builds the rows in one place).
    """
    pending: set[str] = set()
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            if pending:
                return (
                    f"message {index}: an assistant row follows {len(pending)} unanswered tool call(s) "
                    f"{sorted(pending)!r}"
                )
            calls = message.get("tool_calls") or ()
            pending = {str(call.get("id", "")) for call in calls if isinstance(call, Mapping)}
        elif role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            if not pending:
                return f"message {index}: role 'tool' has no preceding assistant row with tool_calls"
            if call_id not in pending:
                return (
                    f"message {index}: tool_call_id {call_id!r} is not declared by the preceding assistant row "
                    f"(it declares {sorted(pending)!r})"
                )
            pending.discard(call_id)
        elif pending:
            return (
                f"message {index}: role {role!r} interrupts a tool round "
                f"({len(pending)} call(s) still unanswered)"
            )
    if pending:
        return f"the transcript ends with {len(pending)} unanswered tool call(s) {sorted(pending)!r}"
    return ""


def truncate_tool_results(
    messages: Sequence[Mapping[str, Any]], cap: int
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    """Cap each tool result and return the pre-truncation originals for recovery."""
    if cap <= 0:
        return tuple(messages), ()
    output: list[Mapping[str, Any]] = []
    retained: list[Mapping[str, Any]] = []
    for message in messages:
        text = _text(message)
        if message.get("role") == "tool" and len(text) > cap:
            retained.append(message)
            output.append({**message, "content": text[:cap] + TRUNCATION_MARKER})
        else:
            output.append(message)
    return tuple(output), tuple(retained)


def _is_protected(message: Mapping[str, Any]) -> bool:
    return message.get("role") == "system" or message.get("pinned") is True


def _round_units(messages: Sequence[Mapping[str, Any]]) -> tuple[tuple[int, ...], ...]:
    """Index groups that have to be dropped **together** to keep the sequence legal.

    ★ A tool round (``assistant(tool_calls)`` + the ``tool`` rows that answer it) is
    one unit. Compaction picks whole units; a cut that fell between the assistant row
    and its results would leave orphan ``tool`` rows, which the next request is
    rejected for outright -- the same 400 this module's sanitizer exists to prevent,
    reintroduced by the *drop* step instead of the *append* step.
    """
    units: list[tuple[int, ...]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            end = index + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            units.append(tuple(range(index, end)))
            index = end
        else:
            units.append((index,))
            index += 1
    return tuple(units)


def _pin_instruction(messages: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Dynamic data belongs at the tail (prefix-cache discipline): move pinned rows last."""
    pinned = [message for message in messages if message.get("pinned") is True]
    if not pinned:
        return messages
    return [message for message in messages if message.get("pinned") is not True] + pinned


def compact(
    messages: Sequence[Mapping[str, Any]],
    policy: CompressionPolicy,
    *,
    summarize: Callable[[tuple[Mapping[str, Any], ...]], str] | None = None,
) -> CompressionResult:
    """Compress toward the threshold, refusing to commit a non-shrinking result."""
    original = tuple(messages)
    tokens_before = estimate_tokens(original)

    sanitized = sanitize_tool_pairs(original)
    capped, retained = truncate_tool_results(sanitized, policy.tool_result_char_cap)
    threshold = compression_threshold(policy)

    dropped: tuple[Mapping[str, Any], ...] = ()
    summary: str | None = None
    rebuilt: list[Mapping[str, Any]] = list(capped)

    if estimate_tokens(capped) > threshold and capped:
        recent = set(range(max(0, len(capped) - policy.keep_recent), len(capped)))
        # ★ 丢弃以【工具轮】为单位：一轮的 assistant 行与它的 tool 行要么一起留、要么一起走
        #   （切在中间会留下孤儿 tool 行 —— 下一个请求直接被端点拒，与 append 那一侧的毛病同源）。
        units = _round_units(capped)
        droppable_units = [
            unit
            for unit in units
            if all(index not in recent for index in unit)
            and all(not _is_protected(capped[index]) for index in unit)
        ]
        chosen_units = droppable_units[: len(droppable_units) // 2]
        chosen = [index for unit in chosen_units for index in unit]
        if chosen:
            chosen_set = set(chosen)
            dropped = tuple(capped[index] for index in chosen)
            if summarize is not None:
                summary = summarize(dropped)
            rebuilt = []
            inserted = False
            for index, message in enumerate(capped):
                if index in chosen_set:
                    if not inserted and summary:
                        rebuilt.append({"role": _SUMMARY_ROLE, "content": f"{_SUMMARY_MARKER}\n{summary}"})
                    inserted = True
                    continue
                rebuilt.append(message)

    rebuilt = _pin_instruction(rebuilt)
    # ★ 兜底：压缩之后的序列必须**仍然**合法。整轮丢弃已经保证了这一点，这一步是
    #   "单点收口"（`AGENTS §7③`）—— 将来谁再动丢弃逻辑，也不许把不合法的序列留在
    #   消息里（它会一路活到下一个请求，然后被端点 400）。
    rebuilt = list(sanitize_tool_pairs(rebuilt))
    tokens_after = estimate_tokens(rebuilt)
    if dropped and tokens_after >= tokens_before:
        # 坑 10: an attempted compression that does not shrink must not be committed.
        return CompressionResult(
            messages=original,
            dropped=(),
            retained=(),
            summary=None,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            committed=False,
        )
    return CompressionResult(
        messages=tuple(rebuilt),
        dropped=dropped,
        retained=retained,
        summary=summary,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        committed=True,
    )


def recoverable(result: CompressionResult, original: Sequence[Mapping[str, Any]]) -> bool:
    """Every original message is still reachable from the result.

    Reachable = still present verbatim, kept as a dropped whole message, or kept as
    the pre-rewrite original of a message whose content was truncated. This is what
    keeps a summary from cutting the attribution chain (架构设计 §8.3).
    """
    available = {digest(message) for message in (*result.messages, *result.dropped, *result.retained)}
    return all(digest(message) in available for message in original)
