"""Tool execution: strict parsing, synthesized refusals, guardrails, call-level
concurrency.

FORK-NOTES §A calls this "the block this project should copy most". Three rules
are copied as-is:

  1. **strict parsing, never repair** (``parse_tool_arguments``). Guessing what the
     model meant hides its real intent *and* can cause side effects. Bad arguments
     become an error result the model can self-correct from.
  2. **a refused call still gets a legal tool result** (``blocked_tool_result``).
     Otherwise the transcript carries a dangling ``tool_call`` and the next
     request is rejected outright by the provider.
  3. **guardrails are pure logic, and ``reset_for_turn`` must run every turn** --
     per-turn state that leaks across turns misjudges a fresh call as a repeat.

Plus the call-level concurrency rule from 架构设计 §7.6 ⑤: parallelisability is a
property of *this call*, not of the tool. Reads may overlap; a write is a barrier.
Results are always written back in the model's original call order.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

__all__ = [
    "GuardrailConfig",
    "ToolCall",
    "ToolCallError",
    "ToolCallGuardrailController",
    "ToolResult",
    "blocked_tool_result",
    "call_signature",
    "invalid_arguments_result",
    "order_results",
    "parse_tool_arguments",
    "plan_tool_segments",
]

TOOL_EFFECTS: tuple[str, ...] = ("read", "write")


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    #: Whether *this call* mutates state. A property of the call, not the tool.
    effect: str = "read"
    #: Paths/resource names this call touches, used only for overlap admission.
    targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.call_id or not self.name:
            raise ValueError("ToolCall.call_id and ToolCall.name must not be empty")
        if self.effect not in TOOL_EFFECTS:
            raise ValueError(f"unknown tool effect {self.effect!r}; expected one of {TOOL_EFFECTS}")


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    ok: bool
    content: str
    blocked: bool = False
    error_type: str | None = None


class ToolCallError(ValueError):
    """The arguments are unusable. They are never repaired -- see the module docstring."""


def parse_tool_arguments(raw_arguments: Any) -> Mapping[str, Any]:
    """Parse model-emitted arguments without repairing or coercing them."""
    try:
        parsed = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ToolCallError("tool arguments are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ToolCallError("tool arguments must be a JSON object")
    return parsed


def invalid_arguments_result(call_id: str, message: str = "") -> ToolResult:
    """The error result a mis-parsed call returns, so the model can self-correct."""
    detail = message or "Tool arguments must be a valid JSON object; the tool was not executed."
    return ToolResult(call_id=call_id, ok=False, content=detail, error_type="invalid_tool_arguments")


def blocked_tool_result(call_id: str, reason: str, *, error_type: str = "guardrail_block") -> ToolResult:
    """Synthesize the result for a refused call.

    ★ A blocked call is still answered: a dangling ``tool_call`` makes the next
    provider request invalid, and the model would see an unexplained hole.
    """
    return ToolResult(call_id=call_id, ok=False, content=reason or "blocked", blocked=True, error_type=error_type)


# --------------------------------------------------------------------------- #
# Call-level concurrency
# --------------------------------------------------------------------------- #


def plan_tool_segments(calls: Sequence[ToolCall]) -> tuple[tuple[str, tuple[ToolCall, ...]], ...]:
    """Split a batch into ordered ``("parallel"|"sequential", calls)`` segments.

    A write is a barrier: it closes the current read run and owns its own segment.
    A read joins the open run. Runs shorter than two calls demote to sequential,
    because a single call has no adjacency to exploit.
    """
    segments: list[tuple[str, tuple[ToolCall, ...]]] = []
    run: list[ToolCall] = []

    def close_run() -> None:
        nonlocal run
        if run:
            kind = "parallel" if len(run) >= 2 else "sequential"
            segments.append((kind, tuple(run)))
            run = []

    def extend_sequential(call: ToolCall) -> None:
        if segments and segments[-1][0] == "sequential":
            kind, existing = segments[-1]
            segments[-1] = (kind, (*existing, call))
        else:
            segments.append(("sequential", (call,)))

    for call in calls:
        if call.effect == "write":
            close_run()
            extend_sequential(call)
            continue
        run.append(call)
    close_run()
    return tuple(segments)


def order_results(calls: Sequence[ToolCall], results: Iterable[ToolResult]) -> tuple[ToolResult, ...]:
    """Write results back in the model's original call order.

    Concurrency may finish out of order; the transcript may not. A call with no
    result still gets one synthesized, so no ``tool_call`` is ever left dangling.
    """
    by_id = {result.call_id: result for result in results}
    ordered: list[ToolResult] = []
    for call in calls:
        ordered.append(by_id.get(call.call_id) or blocked_tool_result(call.call_id, "no result was produced"))
    return tuple(ordered)


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


def call_signature(call: ToolCall) -> str:
    """A content fingerprint of a call -- never an index or ``id()`` (坑 11)."""
    payload = json.dumps(
        {"name": call.name, "effect": call.effect, "arguments": call.arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GuardrailConfig:
    repeat_block_after: int = 4
    same_tool_block_after: int = 6
    max_calls_per_turn: int = 0  # 0 = unlimited

    def __post_init__(self) -> None:
        if min(self.repeat_block_after, self.same_tool_block_after, self.max_calls_per_turn) < 0:
            raise ValueError("guardrail thresholds must be >= 0")


class ToolCallGuardrailController:
    """Per-turn loop detector. Pure logic; no I/O, no clock.

    ★ ``reset_for_turn`` must be called at the start of every turn. Forgetting it
    lets a previous turn's failures block a legitimate call, or lets a loop run
    forever by resetting at the wrong moment.
    """

    def __init__(self, config: GuardrailConfig | None = None) -> None:
        self._config = config or GuardrailConfig()
        self.reset_for_turn()

    @property
    def config(self) -> GuardrailConfig:
        return self._config

    def reset_for_turn(self) -> None:
        self._repeat_failures: dict[str, int] = {}
        self._tool_failures: dict[str, int] = {}
        self._calls_this_turn = 0
        self._halted = False

    @property
    def halted(self) -> bool:
        return self._halted

    def before_call(self, call: ToolCall) -> ToolResult | None:
        """``None`` = allow; a :class:`ToolResult` = block with that result."""
        self._calls_this_turn += 1
        if self._config.max_calls_per_turn and self._calls_this_turn > self._config.max_calls_per_turn:
            self._halted = True
            return blocked_tool_result(
                call.call_id, f"per-turn call cap of {self._config.max_calls_per_turn} reached", error_type="loop_cap"
            )

        signature = call_signature(call)
        repeats = self._repeat_failures.get(signature, 0)
        if repeats >= self._config.repeat_block_after:
            self._halted = True
            return blocked_tool_result(
                call.call_id, f"the same call already failed {repeats} times", error_type="repeat_failure"
            )
        if self._tool_failures.get(call.name, 0) >= self._config.same_tool_block_after:
            self._halted = True
            return blocked_tool_result(
                call.call_id, f"tool {call.name!r} is failing repeatedly", error_type="same_tool_failure"
            )
        return None

    def observe(self, call: ToolCall, result: ToolResult) -> None:
        if result.ok:
            if call.effect == "write":
                # ★ A successful mutation makes every earlier identical failure a
                # new experiment, so the repeat counter is no longer evidence.
                self._repeat_failures.clear()
            self._tool_failures[call.name] = 0
            return
        self._repeat_failures[call_signature(call)] = self._repeat_failures.get(call_signature(call), 0) + 1
        self._tool_failures[call.name] = self._tool_failures.get(call.name, 0) + 1
