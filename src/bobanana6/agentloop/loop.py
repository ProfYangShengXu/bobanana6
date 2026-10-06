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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from ..core.model import Candidate
from .compress import CompressionPolicy, compact
from .concurrency import ResourceGate, RouteTable, SessionRegistry
from .errors import BobananaAction, ClassifiedError, classify_error
from .events import EventLog
from .gate import GateVerdict, can_complete
from .recovery import (
    RecoveryAction,
    TaintTracker,
    decide_recovery,
    declared_feedback,
    reflect_message,
    reformat_message,
)
from .state import CheckpointStore, messages_digest, save_turn
from .tools import (
    ToolCall,
    ToolCallGuardrailController,
    ToolResult,
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


@dataclass(frozen=True)
class LoopConfig:
    max_turns: int = 6
    #: ★ An uncapped restart refunds the iteration budget forever.
    max_restarts: int = 2
    required_scope: str = "full"
    max_retries: int = 2
    max_reflections: int = 1
    taint_threshold: int = 3
    #: False when the block cannot be independently verified (the split is wrong).
    block_verifiable: bool = True

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
                )
            # CONTINUE -> another turn.

        # Budget exhausted. ★ Prefer the candidate the gate withheld over one more call.
        withheld = state.withheld
        candidate = resolve_budget_fallback(withheld)
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
            turn.state = replace(turn.state, messages=close_transcript_tail(turn.state.messages, proposal.candidate))
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
        decision = decide_recovery(
            BobananaAction.RETRY_LOCAL,
            attempts=state.attempts,
            max_retries=self._config.max_retries,
            max_reflections=self._config.max_reflections,
            gate=turn.gate,
            block_verifiable=self._config.block_verifiable,
            taint=self._taint,
            failure="verification did not pass",
            detail=turn.gate.status if turn.gate else "",
        )
        if decision.action is RecoveryAction.RETRY:
            turn.exit_reason = "retry"
            return PhaseResult.cont("retry")
        if decision.action is RecoveryAction.REFLECT:
            text = decision.injected or reflect_message("verification did not pass")
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

    def _tool_round(self, turn: _Turn) -> PhaseResult:
        proposal = turn.proposal
        if proposal is None or not proposal.tool_calls:
            return PhaseResult.fallthrough()
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
        appended = (
            assistant_tool_row(proposal),
            *(
                {"role": "tool", "tool_call_id": result.call_id, "content": result.content}
                for result in turn.results
            ),
        )
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
            has_feedback=feedback is not None,
        )
        if decision.action is RecoveryAction.RETRY:
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
            turn.state = replace(
                turn.state,
                injected=({"role": "user", "content": reformat_message(classified.reason, feedback)},),
                reflections=turn.state.reflections + 1,
            )
            turn.exit_reason = "reformat"
            return PhaseResult.cont("reformat")
        turn.exit_reason = decision.action.value
        return PhaseResult.done(decision.action.value)

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
