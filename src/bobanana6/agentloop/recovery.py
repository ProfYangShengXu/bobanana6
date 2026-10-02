"""Self-healing: the three exits plus taint.

实现规格 §5.9.7 / 架构设计 §7.9. Three exits, in order, and they are not the same
thing:

  1. **retry != reflect.** A retry sends the same input again (transient faults);
     a reflection adds the failure reason to the context and sends again
     (capability shortfalls). Using one for the other wastes the budget on the
     wrong problem.
  2. **degrade to a single agent.** When the split itself is wrong -- the blocks
     are not independently verifiable -- retrying or switching sources is money
     burnt for nothing. This is the ``N -> 1`` legal exit.
  3. **decline.** Handled by :mod:`bobanana6.agentloop.gate`: the third legal
     result, not a failure.

And **taint**: the same tool failing N times in a row gets an explicit "switch
tool" hint instead of another generic retry. It is the single biggest saving on a
weak model, whose typical death is jumping into the same hole repeatedly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import BobananaAction
from .gate import GateVerdict

__all__ = [
    "RecoveryAction",
    "RecoveryDecision",
    "TaintTracker",
    "decide_recovery",
    "reflect_message",
]


class RecoveryAction(StrEnum):
    RETRY = "retry"  # same input again -- transient faults
    REFLECT = "reflect"  # add the failure reason to the context, then retry
    RESHARD = "reshard"  # back to the splitter: the block is too big/hard
    FIX_CONSTRAINT = "fix_constraint"  # the request shape is wrong
    SWITCH_SOURCE = "switch_source"  # change worker / model / provider
    DEGRADE_SINGLE_AGENT = "degrade_single_agent"  # the split itself is wrong
    DECLINE = "decline"  # report we cannot get it -- not a failure
    ABORT = "abort"  # stop; do not retry


_ACTION_MAP = {
    BobananaAction.RESHARD_TASK: RecoveryAction.RESHARD,
    BobananaAction.FIX_CONSTRAINT: RecoveryAction.FIX_CONSTRAINT,
    BobananaAction.SWITCH_SOURCE: RecoveryAction.SWITCH_SOURCE,
    BobananaAction.ABORT: RecoveryAction.ABORT,
}


class TaintTracker:
    """Consecutive-failure counter per tool.

    A success resets the tool's streak, so a tool that fails, succeeds, then fails
    is not tainted.
    """

    def __init__(self, threshold: int = 3) -> None:
        if threshold < 1:
            raise ValueError("TaintTracker.threshold must be >= 1")
        self._threshold = threshold
        self._streaks: dict[str, int] = {}

    @property
    def threshold(self) -> int:
        return self._threshold

    def observe(self, tool: str, ok: bool) -> bool:
        """Record one call; returns True when the tool has just become tainted."""
        if ok:
            self._streaks.pop(tool, None)
            return False
        streak = self._streaks.get(tool, 0) + 1
        self._streaks[tool] = streak
        return streak == self._threshold

    def is_tainted(self, tool: str) -> bool:
        return self._streaks.get(tool, 0) >= self._threshold

    def tainted(self) -> tuple[str, ...]:
        return tuple(sorted(tool for tool, streak in self._streaks.items() if streak >= self._threshold))

    def advice(self, tool: str) -> str | None:
        """★ Taint beats a generic retry: tell the model to switch tools."""
        if not self.is_tainted(tool):
            return None
        return f"tool {tool!r} has failed {self._streaks[tool]} times in a row; try a different tool instead of retrying"


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    attempts: int
    #: Non-empty when reflecting: the text to inject into the context.
    injected: str | None = None
    tainted_tools: tuple[str, ...] = ()


def reflect_message(
    failure: str,
    *,
    detail: str = "",
    classification: str = "",
    taint_hint: str | None = None,
) -> str:
    """The context addition that turns a retry into a reflection."""
    parts = [f"The previous attempt failed: {failure}."]
    if classification:
        parts.append(f"Classification: {classification}.")
    if detail:
        parts.append(f"Observed detail: {detail}.")
    if taint_hint:
        parts.append(taint_hint)
    parts.append("Use that to change your approach; do not repeat the same input unchanged.")
    return " ".join(parts)


def decide_recovery(
    action: BobananaAction,
    *,
    attempts: int,
    max_retries: int,
    max_reflections: int,
    gate: GateVerdict | None = None,
    block_verifiable: bool = True,
    taint: TaintTracker | None = None,
    tool_name: str | None = None,
    failure: str = "",
    detail: str = "",
) -> RecoveryDecision:
    """Pick the next exit for a failure.

    Order matters: a declined gate and an unverifiable split both outrank the
    error's own action, because in those states a retry cannot help.
    """
    tainted = taint.tainted() if taint is not None else ()

    if gate is not None and gate.is_declined:
        return RecoveryDecision(
            action=RecoveryAction.DECLINE,
            reason="the worker declined; declining is a legal exit, not a retry",
            attempts=attempts,
            tainted_tools=tainted,
        )
    if not block_verifiable:
        return RecoveryDecision(
            action=RecoveryAction.DEGRADE_SINGLE_AGENT,
            reason="the block is not independently verifiable; retrying would burn the budget",
            attempts=attempts,
            tainted_tools=tainted,
        )

    hint = taint.advice(tool_name) if taint is not None and tool_name else None
    if hint is not None:
        return RecoveryDecision(
            action=RecoveryAction.REFLECT,
            reason=f"tool {tool_name!r} is tainted",
            attempts=attempts,
            injected=reflect_message(failure or "tool failure", detail=detail, taint_hint=hint),
            tainted_tools=tainted,
        )

    if action is BobananaAction.RETRY_LOCAL:
        if attempts < max_retries:
            return RecoveryDecision(
                action=RecoveryAction.RETRY,
                reason=f"transient failure; attempt {attempts + 1} of {max_retries}",
                attempts=attempts,
                tainted_tools=tainted,
            )
        if attempts - max_retries < max_reflections:
            return RecoveryDecision(
                action=RecoveryAction.REFLECT,
                reason="retries exhausted; add the failure reason and try a different approach",
                attempts=attempts,
                injected=reflect_message(failure or "the attempt failed", detail=detail),
                tainted_tools=tainted,
            )
        return RecoveryDecision(
            action=RecoveryAction.DEGRADE_SINGLE_AGENT,
            reason="retries and reflections exhausted; stop fanning out",
            attempts=attempts,
            tainted_tools=tainted,
        )

    mapped = _ACTION_MAP.get(action, RecoveryAction.ABORT)
    return RecoveryDecision(action=mapped, reason=f"classified action {action.value}", attempts=attempts, tainted_tools=tainted)
