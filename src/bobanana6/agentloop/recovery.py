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

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import SERVER_SIDE_REASONS, BobananaAction
from .gate import GateVerdict

__all__ = [
    "DECLARED_FEEDBACK_METHOD",
    "REQUIRED_FEEDBACK_KEYS",
    "RecoveryAction",
    "RecoveryDecision",
    "TaintTracker",
    "decide_recovery",
    "declared_feedback",
    "gate_failure_detail",
    "reflect_message",
    "reformat_message",
]

#: The method an exception exposes to describe **how to fix** a rejected answer.
#: Same shape as ``errors.DECLARED_REASON_ATTRIBUTE``: whoever produced the failure
#: supplies the semantics, the consumer only renders them.
DECLARED_FEEDBACK_METHOD = "feedback"

#: What a feedback mapping must carry: a machine code, the problem in words, and
#: the shape that would be accepted. ``got`` (what was actually sent) is optional.
REQUIRED_FEEDBACK_KEYS: tuple[str, ...] = ("code", "problem", "expected")

#: How much of a rejected answer is echoed back. The point is for the model to
#: recognise its own output, not to replay the transcript (lec9:125).
GOT_LIMIT = 600


class RecoveryAction(StrEnum):
    RETRY = "retry"  # same input again -- transient faults
    REFLECT = "reflect"  # add the failure reason to the context, then retry
    RESHARD = "reshard"  # back to the splitter: the block is too big/hard
    FIX_CONSTRAINT = "fix_constraint"  # the *constraint/request* is wrong -- not the answer
    #: The *answer's* shape is wrong and the model can fix it if it is told how.
    #: ★ A different direction from FIX_CONSTRAINT on purpose: one changes the
    #: constraint, the other changes the answer. Feeding a model a "your constraint
    #: is too strict" message (or a human a "the model misbehaved" report) is the
    #: failure mode that comes from letting one name cover both.
    REFORMAT = "reformat"
    SWITCH_SOURCE = "switch_source"  # change worker / model / provider
    DEGRADE_SINGLE_AGENT = "degrade_single_agent"  # the split itself is wrong
    #: ★★ 交给**外部**再试一次 —— 重试与反思都试过、壳手里再没有新东西可换时的出口。
    #: 与 `DEGRADE_SINGLE_AGENT` 的区别：那个是"认输，换单 agent 收摊"，这个是
    #: "把做不到 + 原因 + 证据交出去，换一个脑子看一遍"。
    #: ★ 只在**有端口**时才可能被选中：没有实作时它退化成原来的降级（既有读数一字不变）。
    ESCALATE = "escalate"
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


def gate_failure_detail(gate: GateVerdict | None) -> str:
    """What a gate verdict actually objects to, one rule per line (``""`` when none).

    A verdict that names its violations gets each rule on its own line so several
    stay readable. A verdict that names none produces nothing here: its status
    travels as ``classification``, and a bare status is not a fact -- feeding it
    as one would turn a transient retry into a reflection for no information.
    """
    if gate is None or not gate.violations:
        return ""
    return "\n".join(f"- {violation}" for violation in gate.violations)


def declared_feedback(error: Any) -> Mapping[str, Any] | None:
    """The *fix instructions* the raiser attached to a rejected answer, or ``None``.

    This is deliberately **not** the exception's message. ``state.errors`` keeps the
    prose a human reads; the feedback mapping is what the model reads, and the two
    must not be the same string (the prose carries a traceback, the fix does not).
    A raiser that does not provide feedback gets the old behaviour -- the block
    stops and hands the problem to the caller.
    """
    method = getattr(error, DECLARED_FEEDBACK_METHOD, None)
    if not callable(method):
        return None
    try:
        payload = method()
    except Exception:  # noqa: BLE001 - a broken feedback must not mask the real failure
        return None
    if not isinstance(payload, Mapping):
        return None
    for key in REQUIRED_FEEDBACK_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    return payload


def reformat_message(
    failure: str,
    feedback: Mapping[str, Any],
    *,
    got_limit: int = GOT_LIMIT,
) -> str:
    """Render structured feedback for a rejected answer.

    lec9:125 -- *error code + readable reason + actionable advice*, and the raw
    traceback never enters the context. lec9:214 -- the point is that the model can
    act on it, not that it knows it failed.
    """
    got = str(feedback.get("got") or "").strip()
    lines = [
        f"[{failure}] the previous answer was not accepted.",
        f"code: {feedback['code']}",
        f"problem: {feedback['problem']}",
    ]
    if got:
        snippet = got if len(got) <= got_limit else got[:got_limit] + "…[truncated]"
        lines.append(f"what you sent: {snippet}")
    lines.append(f"required shape: {feedback['expected']}")
    lines.append("Send only that JSON object, corrected.")
    return "\n".join(lines)


def decide_recovery(
    action: BobananaAction,
    *,
    attempts: int,
    max_retries: int,
    max_reflections: int,
    gate: GateVerdict | None = None,
    block_verifiable: bool = True,  # blame: fail-open — 与 LoopConfig.block_verifiable 同源同风险：省略即假定可判定
    taint: TaintTracker | None = None,
    tool_name: str | None = None,
    failure: str = "",
    detail: str = "",
    classification: str = "",
    reflections: int = 0,
    has_feedback: bool = False,
    can_escalate: bool = False,  # blame: fail-closed — 省略即"没有外部出口"⇒ 退化成原来的降级（既有读数一字不变）
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
            injected=reflect_message(
                failure or "tool failure", detail=detail, classification=classification, taint_hint=hint
            ),
            tainted_tools=tainted,
        )

    if action is BobananaAction.RETRY_LOCAL:
        # ★★ 服务端类的失败（503 忙 / 5xx / 限速 / 网络超时）：**不走"拿着事实反思"那条**。
        #   为什么：反思的前提是"模型能改"。服务端忙它改不了 —— 重发的是一模一样的东西，
        #   把报错文本灌进上下文只是白占位置，而**它真正需要的是等一等再打**。
        #   实测（qwen4b14）：T-01/T-02 被 503 打挂，走的正是下面 `detail.strip()` 那支
        #   ⇒ 注入了原因、**没退避**，两次瞬间用完 ⇒ 块被判死。
        #   ★ 判据是**错误身份**（`FailoverReason`），不是措辞猜测。
        server_side = str(failure or "") in SERVER_SIDE_REASONS
        # ★ retry != reflect (module docstring): a retry resends the same input for
        #   a transient fault; a reflection hands the failure reason back for a
        #   capability shortfall. When the rejection carries concrete facts (a gate
        #   verdict's violations), it is *already* the capability shortfall, so
        #   reflect on the first rejection instead of resending the same input.
        #   The attempt budget is unchanged -- `max_retries + max_reflections`
        #   rejections before degrading -- only which of them carry the reason.
        can_reflect = reflections < max_reflections
        if attempts >= max_retries + max_reflections:
            # ★★ 重试与反思都用完了 ⇒ 壳手里**没有新东西可换**了（实测 qwen4b14：
            #    三跑连挂、失败读数逐字相同）。有外部出口就先交给它 ——
            #    "换个脑子看一遍"是壳自己产不出来的那一件；没有才认输降级。
            if can_escalate:
                return RecoveryDecision(
                    action=RecoveryAction.ESCALATE,
                    reason="retries and reflections are spent; hand it to the outside",
                    attempts=attempts,
                    tainted_tools=tainted,
                )
            return RecoveryDecision(
                action=RecoveryAction.DEGRADE_SINGLE_AGENT,
                reason="retries and reflections exhausted; stop fanning out",
                attempts=attempts,
                tainted_tools=tainted,
            )
        if detail.strip() and can_reflect and not server_side:
            return RecoveryDecision(
                action=RecoveryAction.REFLECT,
                reason="the rejection carries concrete facts; reflect on them instead of resending",
                attempts=attempts,
                injected=reflect_message(failure or "the attempt failed", detail=detail, classification=classification),
                tainted_tools=tainted,
            )
        if attempts < max_retries:
            return RecoveryDecision(
                action=RecoveryAction.RETRY,
                reason=f"transient failure; attempt {attempts + 1} of {max_retries}",
                attempts=attempts,
                tainted_tools=tainted,
            )
        if can_reflect and not server_side:
            # ★★ `not server_side` 这一半是**补堵**的：上面那个 REFLECT 出口堵住了，
            #   这个出口原本还漏着 —— 实测（`tests/test_escalation.py`）：
            #   `attempts=4, max_retries=4, max_reflections=1` 时，503 走到这里
            #   ⇒ 还是"注入原因 + 不退避"，与上面那个出口同一个病。
            #   服务端忙**没有可反思的东西**，给它一条"换个办法"的提示只会白占上下文；
            #   它该落到下面那条 RETRY（生产接线点会退避）。
            return RecoveryDecision(
                action=RecoveryAction.REFLECT,
                reason="retries exhausted; add the failure reason and try a different approach",
                attempts=attempts,
                injected=reflect_message(failure or "the attempt failed", detail=detail, classification=classification),
                tainted_tools=tainted,
            )
        return RecoveryDecision(
            action=RecoveryAction.RETRY,
            reason="the reflection budget is spent; one more attempt before degrading",
            attempts=attempts,
            tainted_tools=tainted,
        )

    if action is BobananaAction.FIX_CONSTRAINT and has_feedback:
        # ★ The answer's shape was wrong *and* the raiser said how to fix it: feed
        #   that back and let the model answer again. Bounded by the reflection
        #   budget -- no new counter, and the defaults stay as they are.
        if attempts <= max_reflections:
            return RecoveryDecision(
                action=RecoveryAction.REFORMAT,
                reason="the answer's shape was wrong; feed the fix back and ask again",
                attempts=attempts,
                tainted_tools=tainted,
            )
        return RecoveryDecision(
            action=RecoveryAction.FIX_CONSTRAINT,
            reason="the reformat budget is exhausted; the request shape has to change outside this block",
            attempts=attempts,
            tainted_tools=tainted,
        )

    mapped = _ACTION_MAP.get(action, RecoveryAction.ABORT)
    return RecoveryDecision(action=mapped, reason=f"classified action {action.value}", attempts=attempts, tainted_tools=tainted)
