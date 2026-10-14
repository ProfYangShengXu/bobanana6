"""The worker turn loop and its four closing invariants.

The phase/verdict protocol is forked from Hermes' ``_LoopState`` / ``_run_phase``
(FORK-NOTES §A): every phase returns an action, and the action's meaning is fixed:

    fallthrough -> continue to the next phase
    continue    -> start another turn
    break       -> stop without a deliverable (budget / cap reached)
    return      -> finished

Four closing invariants are copied because each one is a real incident:

  1. ``close_transcript_tail`` -- "delivering a result implies a matching row in
     the transcript". Without it a recovery path can leave the durable turn ending
     on a tool row, and the next turn replays an unanswered backlog.
  2. ``guarded`` -- each fallible step is independently guarded and errors are
     collected. One bad candidate must not take the whole assembly down.
  3. ordering -- recovery actions run *before* anything that can raise.
  4. ``resolve_budget_fallback`` -- when the budget runs out, hand back the
     candidate the gate withheld rather than making one more fallible call.

Also here: the restart cap (an uncapped restart refunds the budget forever) and
the KMP stream-continuation dedup for truncated output.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from ..core.model import Candidate, EscalationOutcome, EscalationRequest
from ..core.ports import EscalationPort
from .compress import CompressionPolicy, compact
from .concurrency import ResourceGate, RouteTable, SessionRegistry
from .errors import (
    SERVER_SIDE_REASONS as _SERVER_SIDE_REASONS,
)
from .errors import (
    BobananaAction,
    ClassifiedError,
    classify_error,
)
from .events import EventLog
from .gate import GateVerdict, can_complete
from .recovery import (
    RecoveryAction,
    TaintTracker,
    decide_recovery,
    declared_feedback,
    gate_failure_detail,
    reflect_message,
    reformat_message,
)
from .state import CheckpointStore, messages_digest, save_turn
from .tool_output import ToolOutputPolicy, page_tool_output
from .tools import (
    ToolCall,
    ToolCallGuardrailController,
    ToolResult,
    declared_arguments_problem,
    invalid_arguments_result,
    order_results,
    plan_tool_segments,
)

__all__ = [
    "LoopConfig",
    "LoopIO",
    "LoopResult",
    "LoopState",
    "Phase",
    "PhaseAction",
    "PhaseResult",
    "TurnProposal",
    "WorkerLoop",
    "assemble_messages",
    "assistant_tool_row",
    "close_transcript_tail",
    "common_prefix_length",
    "continuation_overlap_length",
    "guarded",
    "join_truncated_parts",
    "resolve_budget_fallback",
    "run_phase_chain",
    "transcript_tail_problem",
]

T = TypeVar("T")


class PhaseAction(StrEnum):
    FALLTHROUGH = "fallthrough"
    CONTINUE = "continue"
    BREAK = "break"
    RETURN = "return"


class Phase(StrEnum):
    BEGIN = "begin"
    PREPARE = "prepare"
    ASSEMBLE = "assemble"
    GATE = "gate"
    RETRY = "retry"
    RESTART = "restart"
    NORMALIZE = "normalize"
    TOOL_ROUND = "tool_round"
    TEXT_FINISH = "text_finish"


PHASE_ORDER: tuple[Phase, ...] = (
    Phase.BEGIN,
    Phase.PREPARE,
    Phase.ASSEMBLE,
    Phase.GATE,
    Phase.RETRY,
    Phase.RESTART,
    Phase.NORMALIZE,
    Phase.TOOL_ROUND,
    Phase.TEXT_FINISH,
)


@dataclass(frozen=True)
class PhaseResult:
    action: PhaseAction
    detail: str = ""

    @classmethod
    def fallthrough(cls) -> PhaseResult:
        return cls(PhaseAction.FALLTHROUGH)

    @classmethod
    def cont(cls, detail: str = "") -> PhaseResult:
        return cls(PhaseAction.CONTINUE, detail=detail)

    @classmethod
    def stop(cls, detail: str = "") -> PhaseResult:
        return cls(PhaseAction.BREAK, detail=detail)

    @classmethod
    def done(cls, detail: str = "") -> PhaseResult:
        return cls(PhaseAction.RETURN, detail=detail)


def run_phase_chain(handlers: Mapping[Phase, Callable[[], PhaseResult]]) -> PhaseResult:
    """Run phases in the canonical order, honouring the action protocol."""
    for phase in PHASE_ORDER:
        handler = handlers.get(phase)
        if handler is None:
            continue
        result = handler()
        if result.action is not PhaseAction.FALLTHROUGH:
            return result
    return PhaseResult.fallthrough()


# --------------------------------------------------------------------------- #
# Stream-continuation dedup (KMP)
# --------------------------------------------------------------------------- #

_MIN_CONTINUATION_OVERLAP = 32


def continuation_overlap_length(previous: str, continuation: str, *, min_overlap: int = _MIN_CONTINUATION_OVERLAP) -> int:
    """The longest prefix of ``continuation`` that repeats a suffix of ``previous``."""
    if len(previous) < min_overlap or len(continuation) < min_overlap:
        return 0

    prefix_lengths = [0] * len(continuation)
    matched = 0
    for index in range(1, len(continuation)):
        while matched and continuation[index] != continuation[matched]:
            matched = prefix_lengths[matched - 1]
        if continuation[index] == continuation[matched]:
            matched += 1
            prefix_lengths[index] = matched

    matched = 0
    last_index = len(previous) - 1
    for index, char in enumerate(previous):
        while matched and char != continuation[matched]:
            matched = prefix_lengths[matched - 1]
        if char == continuation[matched]:
            matched += 1
            if matched == len(continuation):
                if index == last_index:
                    return matched
                matched = prefix_lengths[matched - 1]
    return matched if matched >= min_overlap else 0


def join_truncated_parts(parts: Sequence[tuple[str, bool]]) -> str:
    """Join continuation fragments, deduping only interrupted-stream seams."""
    joined = ""
    previous_was_partial = False
    for part, is_partial in parts:
        if previous_was_partial and joined and part:
            overlap = continuation_overlap_length(joined[-len(part) :], part)
            part = part[overlap:]
        if joined and not joined[-1].isspace() and part and not part[0].isspace():
            joined += "\n"
        joined += part
        previous_was_partial = is_partial
    return joined


# --------------------------------------------------------------------------- #
# Closing invariants
# --------------------------------------------------------------------------- #


def close_transcript_tail(
    messages: Sequence[Mapping[str, Any]],
    candidate: Candidate | None,
) -> tuple[Mapping[str, Any], ...]:
    """★ Delivering a candidate implies a matching row in the transcript.

    Recovery paths can return a candidate without appending the closing row; if
    that is persisted as-is, the next turn replays a user-only backlog and the
    model re-answers everything.
    """
    rows = list(messages)
    if candidate is None:
        return tuple(rows)
    tail = rows[-1] if rows else None
    if isinstance(tail, Mapping) and tail.get("role") == "assistant" and tail.get("candidate") is True:
        return tuple(rows)
    rows.append({"role": "assistant", "content": candidate.hint, "candidate": True})
    return tuple(rows)


def transcript_tail_problem(messages: Sequence[Mapping[str, Any]]) -> str:
    """Why a transcript that handed back a candidate is not closed (``""`` = it is).

    ``close_transcript_tail`` is the repair; this is the check. It is meaningful
    only at **delivery**: mid-loop a transcript may legally end on a ``tool`` row,
    and that is exactly the state a recovery exit must not persist while claiming
    the candidate is the answer.

    * ending on a ``tool`` row: the delivered answer has no closing row, so a
      durable tail keeps the tool round as the last thing the model said;
    * ending on an unanswered ``assistant(tool_calls)`` row: a candidate cannot
      also be an open tool round (the sequence checker names this one too).
    """
    if not messages:
        return ""
    tail = messages[-1]
    if tail.get("role") == "assistant" and tail.get("candidate") is True:
        return ""
    if tail.get("role") == "tool":
        return "the transcript ends on a tool row; the delivered candidate has no closing assistant row"
    if tail.get("role") == "assistant" and tail.get("tool_calls"):
        return "the transcript ends on an unanswered assistant tool-call row"
    return "the transcript ends without a closing assistant row for the delivered candidate"


def guarded(label: str, fn: Callable[[], T], errors: list[str]) -> T | None:
    """Run one step; collect its failure instead of losing the response.

    Note the list is mutated: the caller owns the error accumulator, which is the
    single place failures from every step end up.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - collected, not swallowed
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return None


def _merge_actions(existing: tuple[str, ...], new: tuple[str, ...]) -> tuple[str, ...]:
    """Append newly completed action ids, preserving order and dropping repeats."""
    merged = list(existing)
    for action in new:
        if action not in merged:
            merged.append(action)
    return tuple(merged)


def resolve_budget_fallback(
    withheld: Candidate | None,
    supplier: Callable[[], Candidate | None] | None = None,
) -> Candidate | None:
    """★ Prefer the candidate verification withheld over another fallible call."""
    if withheld is not None:
        return withheld
    return supplier() if supplier is not None else None


def assemble_messages(
    stable_prefix: Sequence[Mapping[str, Any]],
    dynamic_tail: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Stable content first, dynamic content last -- prefix-cache friendly."""
    return (*stable_prefix, *dynamic_tail)


def common_prefix_length(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]) -> int:
    length = 0
    for a, b in zip(left, right):
        if a != b:
            break
        length += 1
    return length


# --------------------------------------------------------------------------- #
# Loop types
# --------------------------------------------------------------------------- #


#: ★★ 服务端类的失败 —— 把原因告诉模型**没用**：它重发的是一模一样的东西，服务器还是忙。
#: 这类只退避，不回灌原因；其余（模型自己能改的）才告诉它。
#: ★ 判据与 `recovery.decide_recovery` 同源（都在 `errors.SERVER_SIDE_REASONS`），
#:   两处必须用同一份 —— 否则会出现"那边送去反思、这边按退避办"的错位（真踩过）。


@dataclass(frozen=True)
class LoopConfig:
    max_turns: int = 6  # blame: fail-closed — 省略即给一个**有上限**的回合数，方向保守
    #: ★ An uncapped restart refunds the iteration budget forever.
    max_restarts: int = 2  # blame: fail-closed — 省略即上限 2（注释已写明"无上限的重启会永久退款预算"）
    required_scope: str = "full"  # blame: fail-closed — 省略即要求**最严**的验证范围
    #: ★★ 失败上限：**1 次正式 + 3 次重试**（用户口径："重试失败三次后求助"）。
    #: ★ 读数口径：`decide_recovery` 的闸是 `attempts < max_retries`，而 `attempts` 数的是
    #:   **总失败次数**（含第一次）⇒ 要给"3 次重试"，这里必须是 **4**。
    #:   （上一版是 2 = 1 正式 + 1 重试，与用户口径差两次。）
    #: ★ 另一层：**重试不计入回合预算**（`run()` 里那一轮会还回去），所以这个计数器
    #:   是重试**唯一**的闸 —— 它必须自己收紧。
    max_retries: int = 4  # blame: fail-closed — 省略即 1 正式 + 3 重试（重试不吃回合预算，闸只在这里）
    max_reflections: int = 1  # blame: fail-closed — 省略即上限 1
    #: ★★ 求助上限：求助是**贵**动作（它等的是外部，不是自己的重试），
    #: 所以只给一次 —— 拿回来的建议再不对，也不该无限打扰外面。
    max_escalations: int = 1  # blame: fail-closed — 省略即只求助一次
    #: ★★ 退避时长（秒）：服务端忙（503/529/busy）、5xx、限速、网络超时 —— **等一等再打**。
    #: 实测（qwen4b14）：503 的分类与动作都对（`OVERLOADED → RETRY_LOCAL`），但重试那一支
    #: **立刻重发** —— 两次瞬间用完，块被判死；而"服务太忙"恰好是**等一会儿就好**的那一类。
    #: ★ 类里默认 **0**（不睡）：单测/离线路径不该被真睡的秒数拖住（既有读数一字不变）；
    #:   **生产值由装配点给**（`assemble_impl/runtime.py` 传 60，沿用 429 那条"分钟级"口径）。
    retry_backoff_s: float = 0.0  # blame: whitelist — 0 = 不睡（单测/离线）；生产在装配点显式给 60
    #: 可注入的睡眠（与 `adapters/rate_limit.py` 同一写法）：测试用假 sleeper，不真睡。
    sleeper: Callable[[float], None] = field(default=time.sleep, compare=False, repr=False)  # blame: whitelist — 只为可测注入（假 sleeper），生产用默认
    taint_threshold: int = 3  # blame: fail-closed — 省略即 3 次就 taint
    #: False when the block cannot be independently verified (the split is wrong).
    block_verifiable: bool = True  # blame: fail-open — 省略即假定"这个块可判定"；真正的不可判定块会被当成"验证失败"而不是"没法验"

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError("LoopConfig.max_turns must be >= 1")
        if self.max_restarts < 0 or self.max_retries < 0 or self.max_reflections < 0:
            raise ValueError("loop caps must be >= 0")


@dataclass(frozen=True)
class TurnProposal:
    candidate: Candidate | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    text: str = ""
    declined: bool = False
    detail: str = ""
    #: ★ 模型把工具调用写成了正文文本时，这里放一句要交给**下一轮**的告知。
    #:   非空 ⇒ `_text_finish` 把模型正文原样记一行、再把这句话作为 `user` 行挂在
    #:   下一轮请求的**尾部**（与 REFLECT 同一机制，不伪装成 tool 返回）。
    notice: str = ""  # blame: fail-closed — 省略即"没有要告知的"，正常轮次一个键都不加
    #: ★ 端点要求原样带回的 assistant 字段（思考模型的 `reasoning_content`）。循环不解释它，
    #: 只把它存进自己建的那条 assistant 行；端点没给就是空 —— 不许凭空造一个空值。
    carry_back: Mapping[str, Any] = field(default_factory=dict)


def assistant_tool_row(proposal: TurnProposal) -> dict[str, Any]:
    """The assistant row that must precede this round's tool results.

    ★ One invariant, one place: the rows the loop appends are
    ``assistant(tool_calls=[id…]) → tool(id₁) → tool(id₂) → …``. The ids come from
    ``proposal.tool_calls`` -- the same values the results carry -- so the pairing
    cannot drift (the registry normalizes a missing model id into ``call-<n>``, and
    reconstructing from the raw response would not).

    ``arguments`` is emitted as the text the caller sent when we have it (the model's
    exact bytes), falling back to a serialization of the parsed mapping so a call
    built without raw text is still replayable.

    ``carry_back`` rides along verbatim: a thinking-mode endpoint refuses this row
    without its reasoning text, and only the endpoint knows which fields those are.
    """
    row: dict[str, Any] = {
        "role": "assistant",
        "content": proposal.text or "",
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.raw_arguments or json.dumps(dict(call.arguments), ensure_ascii=False),
                },
            }
            for call in proposal.tool_calls
        ],
    }
    # 端点的字段跟着协议字段走；同名不许覆盖（那是壳自己的行形状）。
    row.update({key: value for key, value in proposal.carry_back.items() if key not in row})
    return row


def answer_row(text: str) -> dict[str, Any]:
    """The assistant row that records an answer the loop refused.

    ★ 与 `assistant_tool_row` 同一条不变式，只是管的是**终局那一步**：
    **模型说过的每一句，都要在它自己的 transcript 里有一行** —— 只许压缩，
    不许按需挑选（`架构设计.md` 的上下文完整性不变式）。

    为什么需要它（实测 13f `b90`）：被拒的答案原来**只在错误提示里留一句被截断的
    引文**（`what you sent: …` 末尾是 `[truncated]`，正好截在内容开始的地方）。
    于是下一轮它**看不见自己上一遍填了什么**，只能从零重编 —— 第一遍它填了
    `ref: "submission"`（合法），第二遍却交了个 `ref: ""`、`files: {路径: ""}`、
    `rows: [{""：""}]` 的**空壳**。

    ReAct 的读法：工具轮是 Thought → Action → Observation，每一轮都建了行；
    **终局回答不带工具调用**，它不是 Action，就是"我交卷了"本身 —— 壳原来
    恰恰没为它建行，于是轨迹在非终局处完整、**断在终局**。
    """
    return {"role": "assistant", "content": text}


class LoopIO(Protocol):
    """The loop's collaborators. All injected, so tests never need a model."""

    def produce(self, state: LoopState) -> TurnProposal: ...

    def execute(self, calls: tuple[ToolCall, ...]) -> tuple[ToolResult, ...]: ...

    def verify(self, candidate: Candidate) -> GateVerdict: ...


@dataclass(frozen=True)
class LoopState:
    turn: int = 0
    restarts: int = 0
    attempts: int = 0
    reflections: int = 0
    calls: int = 0
    messages: tuple[Mapping[str, Any], ...] = ()
    withheld: Candidate | None = None
    #: Stable ids (not indices) of the actions this turn already completed, so a
    #: resumed run does not replay them (AGENTS §7①②).
    done_actions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    exit_reason: str = "unknown"
    tainted: tuple[str, ...] = ()
    #: ★ Recovery text waiting to be merged into the transcript. It lives in the
    #: state, not in the per-turn scratch space: a failure is classified *after*
    #: the ASSEMBLE phase has run, and the next turn builds a fresh scratch. Held
    #: per turn it was written and never read -- the reflection never reached the
    #: model. ASSEMBLE (the single assembly point) merges it and clears it.
    injected: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class LoopResult:
    candidate: Candidate | None
    gate: GateVerdict | None
    exit_reason: str
    turns: int
    restarts: int
    errors: tuple[str, ...]
    declined: bool = False
    #: The transcript the run ended on, after the closing row was applied. Exposed
    #: so the loop's own sequence/tail invariants can be asserted from outside
    #: instead of only through a digest.
    messages: tuple[Mapping[str, Any], ...] = ()


@dataclass
class _Turn:
    """Per-turn scratch space. The public state stays frozen."""

    state: LoopState
    task_id: str
    worker_id: str
    route_key: str = ""
    proposal: TurnProposal | None = None
    gate: GateVerdict | None = None
    results: tuple[ToolResult, ...] = ()
    final_candidate: Candidate | None = None
    declined: bool = False
    throttled: bool = False
    route: str | None = None
    exit_reason: str = "unknown"


class WorkerLoop:
    """Drive one worker through turns until a result, a decline, or the budget ends."""

    def __init__(
        self,
        io: LoopIO,
        *,
        config: LoopConfig | None = None,
        store: CheckpointStore | None = None,
        gate: ResourceGate | None = None,
        routes: RouteTable | None = None,
        sessions: SessionRegistry | None = None,
        log: EventLog | None = None,
        taint: TaintTracker | None = None,
        compression: CompressionPolicy | None = None,
        summarize: Callable[[tuple[Mapping[str, Any], ...]], str] | None = None,
        guardrails: ToolCallGuardrailController | None = None,
        classify: Callable[[BaseException], ClassifiedError] = classify_error,
        tool_output: ToolOutputPolicy | None = None,  # blame: fail-closed — 省略即用出厂策略（4000/800/800）；生产装配点显式从 env 读
        spill_tool_output: Callable[[str, str], str] | None = None,  # blame: fail-open — 省略即"没有落盘点 ⇒ 不换页"，把全文交给模型（数据不丢、上下文可能变大）
        escalation: EscalationPort | None = None,  # blame: fail-closed — 省略即"没有外部出口"⇒ 退化成原来的降级（既有读数一字不变）
    ) -> None:
        self._io = io
        self._config = config or LoopConfig()
        self._store = store
        self._gate = gate
        self._routes = routes
        self._sessions = sessions
        self._log = log
        self._taint = taint or TaintTracker(self._config.taint_threshold)
        self._compression = compression
        # ★ Injected, not constructed here: summarizing is a model call, and the
        #   loop must not know how to reach a model beyond the ``LoopIO`` it was given.
        self._summarize = summarize
        self._guardrails = guardrails or ToolCallGuardrailController()
        self._classify = classify
        # ★ The tool-result page rule: the policy (threshold/head/tail) is a
        #   parameter with a plain default here -- the *production* wiring reads it
        #   from the environment (``ToolOutputPolicy`` ``from_env``, called once in
        #   ``assemble_impl/runtime.py``). Reading env inside the loop would make
        #   unit tests depend on ambient state.
        self._tool_output = tool_output or ToolOutputPolicy()
        # ★ Where an over-threshold return's full text goes. ``None`` = this
        #   deployment has nowhere to put it, and a page that cannot be read back
        #   is destruction, not paging -- so nothing is paged (see
        #   ``agentloop/tool_output.py``). ``(call_id, full_text) -> workspace-relative
        #   path``, or ``""`` when the write failed.
        self._spill_tool_output = spill_tool_output
        # ★★ 外部出口（`EscalationPort`）：`None` = 这个部署没有出口，**行为与改前逐字相同**。
        #   为什么放在这里注入：循环不该知道对面是谁、怎么送达 —— 它只知道
        #   "我手里没有新东西可换了，把这件事交出去"。
        #   ★ 计数放在实例上：一个 `WorkerLoop` 只驱动一个块（`runtime.py` 每块新建一个）。
        self._escalation = escalation
        self._escalations = 0

    @property
    def config(self) -> LoopConfig:
        return self._config

    def run(
        self,
        *,
        task_id: str,
        worker_id: str,
        messages: Sequence[Mapping[str, Any]] = (),
        route_key: str | None = None,
    ) -> LoopResult:
        if not task_id or not worker_id:
            raise ValueError("task_id and worker_id must not be empty")
        if self._sessions is not None:
            self._sessions.session(worker_id)  # isolate this worker's mutable state
        state = LoopState(messages=tuple(messages))
        last_gate: GateVerdict | None = None
        routing_key = route_key or task_id

        while state.turn < self._config.max_turns:
            state = replace(state, turn=state.turn + 1)
            turn = _Turn(state=state, task_id=task_id, worker_id=worker_id, route_key=routing_key)
            outcome = run_phase_chain(
                {
                    Phase.BEGIN: PhaseResult.fallthrough,
                    Phase.PREPARE: lambda turn=turn: self._prepare(turn),
                    Phase.ASSEMBLE: lambda turn=turn: self._assemble(turn),
                    Phase.GATE: lambda turn=turn: self._gate_phase(turn),
                    Phase.RETRY: lambda turn=turn: self._retry(turn),
                    Phase.RESTART: lambda turn=turn: self._restart(turn),
                    Phase.NORMALIZE: PhaseResult.fallthrough,
                    Phase.TOOL_ROUND: lambda turn=turn: self._tool_round(turn),
                    Phase.TEXT_FINISH: lambda turn=turn: self._text_finish(turn),
                }
            )
            state = turn.state
            last_gate = turn.gate or last_gate
            if outcome.action is PhaseAction.RETURN:
                # ★ Single point: **any** path that hands back a candidate closes the
                #   tail. Closing it only on the `passed` branch left the recovery
                #   exits (a declined gate verdict) persisting an open tail.
                state = replace(state, messages=close_transcript_tail(state.messages, turn.final_candidate))
                turn.state = state
                status = "ok" if (turn.final_candidate is not None or turn.declined) else "failed"
                self._persist(turn, status=status)
                if self._log is not None:
                    self._log.record(kind="checkpoint", task_id=task_id, worker_id=worker_id, at=float(state.turn), ok=status == "ok")
                return LoopResult(
                    candidate=turn.final_candidate,
                    gate=turn.gate or last_gate,
                    exit_reason=turn.exit_reason,
                    turns=state.turn,
                    restarts=state.restarts,
                    errors=state.errors,
                    declined=turn.declined,
                    messages=state.messages,
                )
            if outcome.action is PhaseAction.BREAK:
                # A turn that was throttled never started; anything else failed.
                self._persist(turn, status="never_started" if turn.throttled else "failed")
                return LoopResult(
                    candidate=None,
                    gate=turn.gate or last_gate,
                    exit_reason=outcome.detail or turn.exit_reason,
                    turns=state.turn,
                    restarts=state.restarts,
                    errors=state.errors,
                    messages=state.messages,
                )
            # CONTINUE -> another turn.

        # Budget exhausted. ★ Prefer the candidate the gate withheld over one more call.
        withheld = state.withheld
        candidate = resolve_budget_fallback(withheld)
        # ★ The budget exit delivers a candidate too, so it closes the tail at the
        #   same single point as the RETURN path above.
        if candidate is not None:
            state = replace(state, messages=close_transcript_tail(state.messages, candidate))
        self._persist(
            _Turn(state=state, task_id=task_id, worker_id=worker_id),
            status="failed",
        )
        return LoopResult(
            candidate=candidate,
            gate=last_gate,
            exit_reason="budget_exhausted_withheld_candidate" if candidate is not None else "budget_exhausted",
            turns=state.turn,
            restarts=state.restarts,
            errors=state.errors,
            messages=state.messages,
        )

    # -- phases ------------------------------------------------------------ #

    def _prepare(self, turn: _Turn) -> PhaseResult:
        # ★ reset_for_turn runs at exactly one place, once per turn.
        self._guardrails.reset_for_turn()
        if self._compression is None or not turn.state.messages:
            return PhaseResult.fallthrough()
        result = compact(turn.state.messages, self._compression, summarize=self._summarize)
        if not (result.committed and result.dropped):
            return PhaseResult.fallthrough()
        if turn.state.restarts >= self._config.max_restarts:
            turn.exit_reason = "restart_cap"
            return PhaseResult.stop("restart_cap")
        turn.state = replace(turn.state, messages=result.messages, restarts=turn.state.restarts + 1)
        return PhaseResult.cont("compressed")

    def _assemble(self, turn: _Turn) -> PhaseResult:
        # Stable rows stay at the front; injected recovery text goes to the tail.
        # ★ This is the single place recovery text enters the transcript, and it
        #   consumes what the *previous* turn queued.
        if not turn.state.injected:
            return PhaseResult.fallthrough()
        turn.state = replace(
            turn.state,
            messages=assemble_messages(turn.state.messages, turn.state.injected),
            injected=(),
        )
        return PhaseResult.fallthrough()

    def _gate_phase(self, turn: _Turn) -> PhaseResult:
        if turn.proposal is None:
            proposal, exc = self._attempt(turn, "produce", lambda: self._call_model(turn))
            turn.proposal = proposal
            if turn.throttled:
                # ★ Over budget: the request was never sent, so the turn never ran.
                turn.exit_reason = "throttled"
                return PhaseResult.stop("throttled")
            if exc is not None:
                return self._classify_and_recover(turn, exc)
            if proposal is None:
                return self._classify_and_recover(turn, RuntimeError("produce returned no proposal"))
        proposal = turn.proposal
        if proposal.notice:
            # ★ 在这里消费，不在 `_text_finish`（只读审计 #133）：`_retry` 排在 `_text_finish`
            #   之前，`withheld` 非空时它必返回 RETRY/REFLECT/DEGRADE ⇒ `_text_finish` 根本轮不到，
            #   告知会被丢掉、永远到不了模型。
            #   ★ 模型正文**原样**记一行（它说过的每句都要有行）；告知是 `user` 行（不伪装成 tool）。
            turn.state = replace(
                turn.state,
                messages=(*turn.state.messages, answer_row(proposal.text)),
                injected=({"role": "user", "content": proposal.notice},),
            )
            turn.exit_reason = "text_tool_call"
            return PhaseResult.cont("text_tool_call")
        if proposal.declined:
            turn.gate = GateVerdict(status="declined", scope=self._config.required_scope)
            turn.declined = True
            turn.exit_reason = "declined"
            return PhaseResult.done("declined")
        if proposal.candidate is None:
            return PhaseResult.fallthrough()
        gate, exc = self._attempt(turn, "verify", lambda: self._io.verify(proposal.candidate))
        if exc is not None:
            return self._classify_and_recover(turn, exc)
        turn.gate = gate
        if can_complete(gate, self._config.required_scope):
            # ★★ 过门即终结。曾经在这里加过一道"必须模型自己声明 `done=true`"的条件
            #   （想让"交答案"和"我干完了"分开），**实测证明它一次都没起到设计作用**：
            #   `ansrow` 轮它写了 `done` ⇒ 交了真内容、过；`bigfile` 轮它没写 ⇒ 交的
            #   **也是真内容**，却因为"没表态"而不终结，一路烧到 `restart_cap` 判死。
            #   ⇒ 两次实验里它没抓到过一个占位符，只抓死过一个好答案。撤掉。
            #   （要治"填了形状就交卷"，该加的是**内容层判据**，不是完成标记。）
            turn.final_candidate = proposal.candidate
            turn.exit_reason = "passed"
            return PhaseResult.done("passed")
        # Withheld: keep it for the budget fallback, then try to fix it.
        turn.state = replace(turn.state, withheld=proposal.candidate, attempts=turn.state.attempts + 1)
        return PhaseResult.fallthrough()

    def _call_model(self, turn: _Turn) -> TurnProposal | None:
        """The single model call, routed and throttled *before* it is sent.

        The loop's only model call goes through the gate here, so the limiter in
        :mod:`bobanana6.agentloop.concurrency` is on the real path and not a dead
        dependency. Two distinct keys, matching 架构设计 §7.6:

          * the **route** keys the proactive throttle (the limit belongs to the
            model endpoint);
          * the **request** key (route + task + content digest) keys single-flight,
            so a re-run of the *same* request waits instead of firing a duplicate.
        """
        if self._gate is None:
            return self._io.produce(turn.state)
        route = self._routes.route(turn.route_key) if self._routes is not None else "default"
        turn.route = route
        if not self._gate.acquire(route):
            turn.throttled = True
            return None
        try:
            request_key = f"{route}:{turn.task_id}:{messages_digest(turn.state.messages)}"
            return self._gate.single_flight(request_key)(lambda: self._io.produce(turn.state))
        finally:
            self._gate.release(route)

    def _retry(self, turn: _Turn) -> PhaseResult:
        state = turn.state
        if state.withheld is None:
            return PhaseResult.fallthrough()
        detail = gate_failure_detail(turn.gate)
        classification = turn.gate.status if turn.gate else ""
        decision = decide_recovery(
            BobananaAction.RETRY_LOCAL,
            attempts=state.attempts,
            max_retries=self._config.max_retries,
            max_reflections=self._config.max_reflections,
            gate=turn.gate,
            block_verifiable=self._config.block_verifiable,
            taint=self._taint,
            failure="verification did not pass",
            detail=detail,
            classification=classification,
            reflections=state.reflections,
        )
        if decision.action is RecoveryAction.RETRY:
            turn.exit_reason = "retry"
            return PhaseResult.cont("retry")
        if decision.action is RecoveryAction.REFLECT:
            text = decision.injected or reflect_message(
                "verification did not pass", detail=detail, classification=classification
            )
            turn.state = replace(
                state,
                injected=({"role": "user", "content": text},),
                reflections=state.reflections + 1,
            )
            turn.exit_reason = "reflect"
            return PhaseResult.cont("reflect")
        if decision.action is RecoveryAction.DEGRADE_SINGLE_AGENT:
            turn.exit_reason = RecoveryAction.DEGRADE_SINGLE_AGENT.value
            return PhaseResult.stop("degrade_single_agent")
        turn.final_candidate = state.withheld
        turn.exit_reason = decision.action.value
        return PhaseResult.done(decision.action.value)

    def _restart(self, turn: _Turn) -> PhaseResult:
        if turn.state.restarts > self._config.max_restarts:
            turn.exit_reason = "restart_cap"
            return PhaseResult.stop("restart_cap")
        return PhaseResult.fallthrough()

    def _page_tool_result(self, turn: _Turn, result: ToolResult, tool: str) -> str:
        """★ The single door: cap one tool result on its way into the transcript.

        Under the threshold the content is returned byte-for-byte -- no spill, no
        extra text, no event. Over it, the full text is written out first and a
        head + tail page (with the omitted size and the location named) is
        returned. If the full text cannot be written, the content is returned
        whole: paging must never destroy what it cannot page back.
        """
        text = result.content
        if not self._tool_output.pages(len(text)):
            return text
        where = ""
        failure = ""
        if self._spill_tool_output is not None:
            try:
                where = self._spill_tool_output(result.call_id, text)
            except Exception as exc:  # noqa: BLE001 - a failed spill must not fail the turn
                # ★ 落盘是辅助，不是前置门（与 `shell.run` 的同一条守卫一致）：
                #   它坏掉时"不换页"，绝不把 turn 带走，也绝不留下取不回的指针。
                failure = f"{type(exc).__name__}: {exc}"
        if not where:
            self._note_truncate(
                turn,
                tool,
                ok=False,
                detail={
                    "chars": len(text),
                    "reason": failure or "the full text could not be written, so nothing was dropped",
                },
            )
            return text
        page = page_tool_output(text, self._tool_output, full_path=where)
        self._note_truncate(
            turn,
            tool,
            ok=True,
            detail={
                "full_path": where,
                "total_chars": page.total_chars,
                "total_lines": page.total_lines,
                "head_chars": page.head_chars,
                "tail_chars": page.tail_chars,
                "omitted_chars": page.omitted_chars,
            },
        )
        return page.body

    def _note_truncate(self, turn: _Turn, tool: str, *, ok: bool, detail: dict[str, Any]) -> None:
        """The event that answers "was it paged, and where is the original".

        ``truncate`` was in the vocabulary with no producer; this is its producer.
        A failed spill is recorded too (``ok=False``) instead of staying silent.
        """
        if self._log is None:
            return
        self._log.record(
            kind="truncate",
            task_id=turn.task_id,
            worker_id=turn.worker_id,
            at=float(turn.state.turn),
            ok=ok,
            detail={"tool": tool, **detail},
        )

    def _tool_round(self, turn: _Turn) -> PhaseResult:
        proposal = turn.proposal
        if proposal is None or not proposal.tool_calls:
            return PhaseResult.fallthrough()
        # ★ Action 契约：一次工具轮要么全部执行、要么一个都不执行。声明的参数只要
        #   有一个不可解析（模型输出被截断 / 吐了坏 JSON），整批就不可执行 —— 同一次
        #   截断可能让**别的**调用看起来完整、其实已经缺了一截，而它照样会被跑。Pi 对
        #   `stopReason="length"` 的消息就是整批失败；Hermes 对一批里 JSON 坏的调用
        #   也是给每条回 "Skipped: other tool call in this response had invalid JSON"。
        #   被拦下的调用**仍然各得一条 tool 行**（顺序/数量不变，模型能自纠）。
        unusable = declared_arguments_problem(proposal.tool_calls)
        if unusable:
            turn.results = tuple(
                invalid_arguments_result(call.call_id, unusable) for call in proposal.tool_calls
            )
        else:
            segments = plan_tool_segments(proposal.tool_calls)
            results: list[ToolResult] = []
            for kind, calls in segments:
                allowed: list[ToolCall] = []
                for call in calls:
                    blocked = self._guardrails.before_call(call)
                    if blocked is not None:
                        results.append(blocked)
                        self._guardrails.observe(call, blocked)
                        continue
                    allowed.append(call)
                if not allowed:
                    continue
                produced: tuple[ToolResult, ...] = ()
                if kind == "parallel":
                    batch, _exc = self._attempt(turn, "execute", lambda allowed=tuple(allowed): self._io.execute(allowed))
                    produced = tuple(batch or ())
                else:
                    collected: list[ToolResult] = []
                    for call in allowed:
                        one, _exc = self._attempt(turn, "execute", lambda call=call: self._io.execute((call,)))
                        collected.extend(one or ())
                    produced = tuple(collected)
                ordered = order_results(tuple(allowed), produced)
                results.extend(ordered)
                for call, result in zip(allowed, ordered):
                    self._guardrails.observe(call, result)
                    self._taint.observe(call.name, result.ok)
            turn.results = order_results(proposal.tool_calls, results)
        completed = tuple(f"tool:{call.call_id}" for call in proposal.tool_calls)
        turn.state = replace(
            turn.state,
            calls=turn.state.calls + len(proposal.tool_calls),
            done_actions=_merge_actions(turn.state.done_actions, completed),
        )
        # ★ 顺序是协议的硬要求（不是风格）：一次工具轮必须落成
        #   `assistant(tool_calls=[id…]) → tool(id₁) → tool(id₂) → …`。
        #   只追加 tool 行会让请求直接 400（"Messages with role 'tool' must be a
        #   response to a preceding message with 'tool_calls'"），而且——即使宽松端点
        #   放行——模型看到的历史里也**没有它自己刚才调了什么**：每个 tool 行都成了
        #   没有出处的 JSON 块。
        #   id 一律取自 `proposal.tool_calls`（注册表归一化过、缺 id 时会补 `call-<n>`），
        #   所以 assistant 声明的 id 与下面每一条 tool 的 `tool_call_id` **必然**对得上。
        # ★★ 工具返回进上下文之前的那【唯一】一道门就是这里：执行出来的、被 guardrail
        #   拦下的、参数不可解析而合成的 tool 行全部经过这一处 ⇒ 「超阈值 ⇒ 头尾 + 指针」
        #   只写一遍（`agentloop/tool_output.py`）。放回各工具里就是每个工具一份，必然漂移。
        names = {call.call_id: call.name for call in proposal.tool_calls}
        tool_rows = tuple(
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": self._page_tool_result(turn, result, names.get(result.call_id, "")),
            }
            for result in turn.results
        )
        appended = (assistant_tool_row(proposal), *tool_rows)
        turn.state = replace(turn.state, messages=(*turn.state.messages, *appended), tainted=self._taint.tainted())
        turn.exit_reason = "tool_round"
        return PhaseResult.cont("tool_round")

    def _text_finish(self, turn: _Turn) -> PhaseResult:
        proposal = turn.proposal
        if proposal is None or proposal.candidate is not None or proposal.tool_calls:
            return PhaseResult.fallthrough()
        if proposal.text:
            turn.exit_reason = "text_finish"
            return PhaseResult.done("text_finish")
        turn.exit_reason = "no_output"
        return PhaseResult.stop("no_output")

    def _attempt(self, turn: _Turn, label: str, fn: Callable[[], T]) -> tuple[T | None, Exception | None]:
        """Run one fallible step and keep its exception for classification.

        This is the in-loop form of :func:`guarded`: the error is collected *and*
        handed on, because a failure that cannot be classified cannot be recovered
        from correctly.
        """
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001 - collected, then classified
            turn.state = replace(turn.state, errors=(*turn.state.errors, f"{label}: {type(exc).__name__}: {exc}"))
            return None, exc

    def _classify_and_recover(self, turn: _Turn, exc: BaseException) -> PhaseResult:
        classified = self._classify(exc)
        turn.state = replace(turn.state, exit_reason=classified.reason, attempts=turn.state.attempts + 1)
        # A failed turn is the form that most needs resuming -- persist it even when
        # the loop is about to retry it.
        self._persist(turn, status="failed")
        # ★ Whether the fix is *sayable* is what separates "the answer's shape was
        #   wrong, tell the model how" from "the constraint/request is wrong, stop
        #   and hand it to the caller". The raiser knows; nothing is inferred here.
        feedback = declared_feedback(exc)
        decision = decide_recovery(
            classified.action,
            attempts=turn.state.attempts,
            max_retries=self._config.max_retries,
            max_reflections=self._config.max_reflections,
            block_verifiable=self._config.block_verifiable,
            taint=self._taint,
            failure=classified.reason,
            detail=classified.detail,
            reflections=turn.state.reflections,
            has_feedback=feedback is not None,
            # ★ 有出口、且还没用过 ⇒ 重试与反思都用完时改走"交给外部"。
            can_escalate=self._escalation is not None and self._escalations < self._config.max_escalations,
        )
        if decision.action is RecoveryAction.RETRY:
            # ★★ 重试【不计入回合预算】—— 把这一轮还回去。
            #   为什么：`run()` 的 `while state.turn < max_turns` 每轮都 +1，而重试走的正是
            #   "再来一轮" ⇒ 它吃的是**同一个预算**（`runtime.py:359` 把 `max_turns` 接到了
            #   `budget.max_calls`，也就是 BB6_MAX_CALLS 那个总闸）。用户口径：重试的闸是
            #   `max_retries`（它自己数），不该再从回合预算里扣。
            #   ★ 不会失控：`attempts` 只增不减，`decide_recovery` 用它卡住重试次数。
            turn.state = replace(turn.state, turn=max(0, turn.state.turn - 1))
            reason = str(classified.reason)
            if reason in _SERVER_SIDE_REASONS:
                # ★ 服务端忙 / 网络抖：**等一等再打**。立刻重发是往火上浇油 ——
                #   实测（qwen4b14）两次重试瞬间用完，块被判死，而这类恰好是等一会儿就好的。
                if self._config.retry_backoff_s > 0:
                    self._config.sleeper(self._config.retry_backoff_s)
            else:
                # ★ 模型自己能改的失败：**把原因回灌给它** —— 不说，它只会原样再交一次。
                turn.state = replace(
                    turn.state,
                    injected=(
                        {
                            "role": "user",
                            "content": reflect_message(reason, detail=classified.detail),
                        },
                    ),
                )
            turn.exit_reason = "retry_after_error"
            return PhaseResult.cont("retry_after_error")
        if decision.action is RecoveryAction.REFLECT:
            text = decision.injected or reflect_message(classified.reason, detail=classified.detail)
            turn.state = replace(
                turn.state,
                injected=({"role": "user", "content": text},),
                reflections=turn.state.reflections + 1,
            )
            turn.exit_reason = "reflect_after_error"
            return PhaseResult.cont("reflect_after_error")
        if decision.action is RecoveryAction.REFORMAT:
            # ★ The structured fix, not the prose: lec9:125 wants code + reason +
            #   advice, and the prose (which is what state.errors keeps) is not that.
            assert feedback is not None  # guaranteed by decide_recovery
            # ★★ 先把它自己交的那份**记成它自己的一轮**，再发反馈（`answer_row`）。
            #   只发反馈 = 它看不见自己写了什么，重填只能从零编 —— 实测交了个空壳。
            rows: list[dict[str, Any]] = []
            sent = str(feedback.get("got") or "")
            if sent.strip():
                rows.append(answer_row(sent))
            rows.append({"role": "user", "content": reformat_message(classified.reason, feedback)})
            turn.state = replace(
                turn.state,
                injected=tuple(rows),
                reflections=turn.state.reflections + 1,
            )
            turn.exit_reason = "reformat"
            return PhaseResult.cont("reformat")
        if decision.action is RecoveryAction.ESCALATE:
            # ★★ 交给外部：把"做不到 + 原因 + 证据"送出去，**等它答复**。
            #   为什么等而不是弃权：对面若是只会被动应答的一方（人 / 另一个 agent），
            #   不等就等于把答复扔了 —— 它不会自己回头来找我们。
            #   ★ 阻塞的边界由实作定（连不上要立刻返回，不能傻等）；总闸仍是整跑的墙钟。
            outcome = self._ask_outside(turn, classified)
            if outcome.answered and outcome.advice.strip():
                self._escalations += 1
                # ★ 求助这一轮也【不计入回合预算】—— 与重试同类：
                #   花的是"再试一次"的额度，不是"正式产出"的额度。
                turn.state = replace(
                    turn.state,
                    turn=max(0, turn.state.turn - 1),
                    injected=({"role": "user", "content": outcome.advice},),
                )
                turn.exit_reason = "escalated"
                return PhaseResult.cont("escalated")
            # ★ 没拿到答复 ⇒ **落回原来的降级出口**：交不出去不该让这一块更糟。
            turn.exit_reason = "escalate_unanswered"
            return PhaseResult.done(RecoveryAction.DEGRADE_SINGLE_AGENT.value)
        turn.exit_reason = decision.action.value
        return PhaseResult.done(decision.action.value)

    def _ask_outside(self, turn: _Turn, classified: ClassifiedError) -> EscalationOutcome:
        """把这一块的"做不到 + 原因 + 证据"交出去，拿回答复。

        ★ 端口抛异常**不许杀掉整跑** —— 交不出去是"没拿到答复"，不是崩溃。
          与 `_publish_disk_artifact` 同一条纪律：兜底路径上的 IO 风险要**就地收口**
          （那一处的原话：它一抛，杀掉的不是这一块而是整个 run）。
        """
        escalation = self._escalation
        if escalation is None:
            return EscalationOutcome(answered=False, detail="this deployment has no outside exit")
        request = EscalationRequest(
            task_id=turn.task_id,
            block=turn.worker_id,
            reason=str(classified.reason),
            attempts=turn.state.attempts,
            detail=str(classified.detail or ""),
        )
        try:
            return escalation.escalate(request)
        except Exception as exc:  # noqa: BLE001 - 端口是实现方的代码；坏在这里不该杀整跑
            return EscalationOutcome(answered=False, detail=f"{type(exc).__name__}: {exc}")

    # -- bookkeeping ------------------------------------------------------- #

    def _persist(self, turn: _Turn, *, status: str) -> None:
        if self._store is None:
            return
        save_turn(
            self._store,
            task_id=turn.task_id,
            worker_id=turn.worker_id,
            turn=turn.state.turn,
            messages=turn.state.messages,
            done_actions=turn.state.done_actions,
            status=status,
        )
