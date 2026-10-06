"""The worker loop: phase protocol, restart cap, closing invariants, budget fallback."""

from __future__ import annotations

import json
import threading
import time

from bobanana6.agentloop.compress import CompressionPolicy
from bobanana6.agentloop.concurrency import GateConfig, RouteTable, RuntimeGate
from bobanana6.agentloop.gate import GateVerdict
from bobanana6.agentloop.loop import (
    LoopConfig,
    Phase,
    PhaseAction,
    PhaseResult,
    TurnProposal,
    WorkerLoop,
    assemble_messages,
    close_transcript_tail,
    common_prefix_length,
    continuation_overlap_length,
    guarded,
    join_truncated_parts,
    resolve_budget_fallback,
    run_phase_chain,
    transcript_tail_problem,
)
from bobanana6.agentloop.state import (
    InMemoryCheckpointStore,
    load_checkpoint,
    messages_digest,
    resume,
)
from bobanana6.agentloop.tools import ToolCall, ToolResult
from bobanana6.core.model import Candidate


def candidate(hint: str = "answer") -> Candidate:
    return Candidate(subtask_id="T-01", source_id="src-1", hint=hint)


def read(call_id: str) -> ToolCall:
    return ToolCall(call_id=call_id, name="read", arguments={"path": call_id})


def write(call_id: str) -> ToolCall:
    return ToolCall(call_id=call_id, name="write_file", arguments={"path": call_id}, effect="write")


class FakeIO:
    def __init__(self, proposals, gates=None, *, fail_produce: bool = False) -> None:
        self._proposals = list(proposals)
        self._gates = list(gates or [])
        self.fail_produce = fail_produce
        self.states = []
        self.executed: list[tuple[ToolCall, ...]] = []
        self.verify_calls = 0

    def produce(self, state):
        if self.fail_produce:
            raise RuntimeError("producer exploded")
        self.states.append(state)
        return self._proposals.pop(0) if self._proposals else TurnProposal(text="done")

    def execute(self, calls):
        self.executed.append(tuple(calls))
        return tuple(ToolResult(call_id=call.call_id, ok=True, content=f"r:{call.call_id}") for call in calls)

    def verify(self, _candidate):
        self.verify_calls += 1
        return self._gates.pop(0) if self._gates else GateVerdict("passed", "full")


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


def test_a_passing_candidate_is_returned_and_the_tail_is_closed() -> None:
    io = FakeIO([TurnProposal(candidate=candidate())])
    result = WorkerLoop(io).run(task_id="t", worker_id="w")
    assert result.exit_reason == "passed"
    assert result.candidate is not None
    assert result.declined is False


def test_declined_is_a_terminal_legal_exit() -> None:
    io = FakeIO([TurnProposal(declined=True)])
    result = WorkerLoop(io).run(task_id="t", worker_id="w")
    assert result.declined is True
    assert result.exit_reason == "declined"
    assert result.candidate is None
    assert io.verify_calls == 0


def test_budget_fallback_returns_the_candidate_the_gate_withheld() -> None:
    """★ Do not make one more fallible call when the budget is gone."""
    io = FakeIO(
        [TurnProposal(candidate=candidate("v1")), TurnProposal(candidate=candidate("v2"))],
        gates=[GateVerdict("failed", "full"), GateVerdict("failed", "full")],
    )
    loop = WorkerLoop(io, config=LoopConfig(max_turns=2, max_retries=9, max_reflections=9))
    result = loop.run(task_id="t", worker_id="w")
    assert result.exit_reason == "budget_exhausted_withheld_candidate"
    assert result.candidate is not None


def test_budget_exhausted_without_a_withheld_candidate_reports_none() -> None:
    io = FakeIO([TurnProposal(text="thinking")], gates=[])
    result = WorkerLoop(io, config=LoopConfig(max_turns=1)).run(task_id="t", worker_id="w")
    assert result.candidate is None
    assert result.exit_reason in {"budget_exhausted", "text_finish"}


def test_a_restart_is_capped() -> None:
    policy = CompressionPolicy(context_length=40, max_tokens=2, pct=0.5, keep_recent=0)
    messages = tuple({"role": "user", "content": f"m{i} " + "x" * 200} for i in range(20))
    io = FakeIO([TurnProposal(text="done")])
    loop = WorkerLoop(io, config=LoopConfig(max_turns=5, max_restarts=1), compression=policy)
    result = loop.run(task_id="t", worker_id="w", messages=messages)
    assert result.exit_reason == "restart_cap"


def test_a_failed_turn_is_persisted_before_the_retry() -> None:
    """★ A failed turn must already be on disk when the next turn starts.

    If it is only written at the end of the run, a crash between the failure and
    the retry loses the very turn that most needs resuming.
    """
    store = InMemoryCheckpointStore()
    seen: list = []

    class IO(FakeIO):
        def produce(self, state):
            seen.append(load_checkpoint(store, "task-1"))
            if len(seen) == 1:
                raise RuntimeError("boom")
            return TurnProposal(candidate=candidate())

    loop = WorkerLoop(IO([]), config=LoopConfig(max_retries=3, max_reflections=3), store=store)
    result = loop.run(task_id="task-1", worker_id="w")

    assert result.errors  # the failure was collected, not swallowed
    assert result.exit_reason == "passed"
    assert seen[0] is None  # nothing persisted before the first turn
    assert seen[1] is not None and seen[1].status == "failed"


def test_tool_round_batches_reads_and_writes_results_in_order() -> None:
    io = FakeIO([TurnProposal(tool_calls=(read("1"), read("2"))), TurnProposal(text="done")])
    result = WorkerLoop(io).run(task_id="t", worker_id="w")
    assert result.exit_reason == "text_finish"
    assert io.executed == [(read("1"), read("2"))]
    appended = [row["tool_call_id"] for row in io.states[-1].messages if row.get("role") == "tool"]
    assert appended == ["1", "2"]


def test_a_write_breaks_the_parallel_batch() -> None:
    io = FakeIO([TurnProposal(tool_calls=(read("1"), write("w"), read("2"))), TurnProposal(text="done")])
    WorkerLoop(io).run(task_id="t", worker_id="w")
    assert all(len(batch) == 1 for batch in io.executed)


def test_errors_are_collected_not_swallowed() -> None:
    errors: list[str] = []
    assert guarded("step", lambda: (_ for _ in ()).throw(RuntimeError("boom")), errors) is None
    assert errors == ["step: RuntimeError: boom"]
    assert guarded("ok", lambda: 7, errors) == 7


# --------------------------------------------------------------------------- #
# pure helpers / invariants
# --------------------------------------------------------------------------- #


def test_close_transcript_tail_appends_a_matching_row() -> None:
    closed = close_transcript_tail(({"role": "tool", "content": "r"},), candidate())
    assert closed[-1]["role"] == "assistant"
    assert closed[-1]["candidate"] is True
    # Idempotent: an already-closed tail is not duplicated.
    assert close_transcript_tail(closed, candidate()) == closed


def test_close_transcript_tail_is_a_noop_without_a_candidate() -> None:
    messages = ({"role": "tool", "content": "r"},)
    assert close_transcript_tail(messages, None) == messages


def test_resolve_budget_fallback_prefers_the_withheld_candidate() -> None:
    held = candidate("held")
    assert resolve_budget_fallback(held) is held
    assert resolve_budget_fallback(None) is None
    assert resolve_budget_fallback(None, lambda: held) is held


def test_assemble_messages_keeps_the_stable_prefix_in_front() -> None:
    stable = ({"role": "system", "content": "protocol"}, {"role": "user", "content": "contract"})
    first = assemble_messages(stable, ({"role": "user", "content": "subtask A"},))
    second = assemble_messages(stable, ({"role": "user", "content": "subtask B"},))
    shared = common_prefix_length(first, second)
    assert shared == len(stable)
    assert shared >= len(first) * 0.5


def test_continuation_overlap_dedupes_a_stream_seam() -> None:
    seam = "HELLO WORLD " * 4  # long enough to clear the 32-char confidence floor
    previous = "a" * 40 + seam
    continuation = seam + "b" * 40
    overlap = continuation_overlap_length(previous, continuation)
    assert overlap >= len(seam)
    joined = join_truncated_parts([(previous, True), (continuation, False)])
    assert joined.count(seam) == 1


def test_continuation_overlap_ignores_short_fragments() -> None:
    assert continuation_overlap_length("ab", "ab") == 0


def test_join_truncated_parts_inserts_a_break_when_needed() -> None:
    joined = join_truncated_parts([("hello", True), ("world", False)])
    assert joined == "hello\nworld"


def test_run_phase_chain_honours_the_action_protocol() -> None:
    seen: list[str] = []

    def phase(name: str, result: PhaseResult):
        def handler() -> PhaseResult:
            seen.append(name)
            return result

        return handler

    outcome = run_phase_chain(
        {
            Phase.BEGIN: phase("begin", PhaseResult.fallthrough()),
            Phase.PREPARE: phase("prepare", PhaseResult.cont("again")),
            Phase.ASSEMBLE: phase("assemble", PhaseResult.fallthrough()),
        }
    )
    assert outcome.action is PhaseAction.CONTINUE
    assert seen == ["begin", "prepare"]  # nothing after a non-fallthrough action


# --------------------------------------------------------------------------- #
# concurrency is on the loop's real path (架构设计 §7.6)
# --------------------------------------------------------------------------- #


class SpyGate:
    """Records the throttle key while delegating to a real gate."""

    def __init__(self, inner: RuntimeGate) -> None:
        self._inner = inner
        self.acquired: list[str] = []

    def acquire(self, key, timeout=None):
        self.acquired.append(key)
        return self._inner.acquire(key, 0.0)

    def release(self, key):
        self._inner.release(key)

    def single_flight(self, key):
        return self._inner.single_flight(key)


def _wait_for_waiters(gate: RuntimeGate, key: str, count: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while gate.waiters(key) < count and time.monotonic() < deadline:
        time.sleep(0.001)
    assert gate.waiters(key) >= count, "no waiter registered on the in-flight request"


def test_the_loop_throttles_before_the_model_call() -> None:
    """★ Over budget => the request is never sent, and the turn never started."""
    io = FakeIO([TurnProposal(candidate=candidate())])
    gate = RuntimeGate(GateConfig(max_concurrent=1))
    assert gate.acquire("cheap") is True  # occupy the endpoint
    store = InMemoryCheckpointStore()
    loop = WorkerLoop(io, gate=gate, routes=RouteTable({"task-1": "cheap"}), store=store)

    result = loop.run(task_id="task-1", worker_id="w")

    assert result.exit_reason == "throttled"
    assert io.states == []  # ★ the request was not sent
    checkpoint = load_checkpoint(store, "task-1")
    assert checkpoint is not None and checkpoint.status == "never_started"


def test_the_loop_routes_through_the_route_table() -> None:
    io = FakeIO([TurnProposal(candidate=candidate())])
    gate = SpyGate(RuntimeGate())
    loop = WorkerLoop(io, gate=gate, routes=RouteTable({"task-1": "cheap"}))

    loop.run(task_id="task-1", worker_id="w")

    assert gate.acquired == ["cheap"], "the loop must not pick a model on its own"


def test_the_loop_dedupes_identical_in_flight_requests() -> None:
    gate = RuntimeGate(GateConfig(max_concurrent=4))
    calls: list[int] = []
    in_flight = threading.Event()
    release = threading.Event()

    class IO(FakeIO):
        def produce(self, state):
            calls.append(1)
            in_flight.set()
            release.wait(3.0)
            return TurnProposal(candidate=candidate())

    messages = ({"role": "user", "content": "hi"},)
    request_key = f"default:task-1:{messages_digest(messages)}"
    results: list = []

    def run_once() -> None:
        results.append(WorkerLoop(IO([]), gate=gate).run(task_id="task-1", worker_id="w", messages=messages))

    first = threading.Thread(target=run_once)
    first.start()
    assert in_flight.wait(2.0)
    second = threading.Thread(target=run_once)
    second.start()
    _wait_for_waiters(gate, request_key, 1)
    release.set()
    first.join(3.0)
    second.join(3.0)

    assert len(calls) == 1  # ★ the duplicate never fired a model call
    assert len(results) == 2


def test_completed_actions_are_persisted_and_not_replayed() -> None:
    """★ The three-state checkpoint is only useful if done_actions is filled in."""
    store = InMemoryCheckpointStore()

    class IO(FakeIO):
        def produce(self, state):
            self.states.append(state)
            if len(self.states) == 1:
                return TurnProposal(tool_calls=(read("1"), read("2")))
            raise RuntimeError("boom")

    loop = WorkerLoop(IO([]), config=LoopConfig(max_retries=0, max_reflections=0), store=store)
    result = loop.run(task_id="task-1", worker_id="w")

    assert result.exit_reason not in {"passed", "declined"}
    checkpoint = load_checkpoint(store, "task-1")
    assert checkpoint is not None
    assert checkpoint.status == "failed"
    assert checkpoint.done_actions == ("tool:1", "tool:2")
    assert resume(checkpoint).completed_actions == ("tool:1", "tool:2")


def test_a_local_retry_is_a_continuation_and_never_a_terminal_exit() -> None:
    """★ `#30` 的看门狗：重试（`retry` / `retry_after_error`）**不留终态**。

    验收的九条出口里有 `retry_local`，但采集图里它今天到不了 —— 不是因为没重试
    （这一跑就重试了一次），而是因为重试是 `PhaseResult.cont`：下一轮会把
    `exit_reason` 覆盖掉，预算用尽时是 `budget_exhausted`。

    ★ 如果哪天运行期把"发生过一次重试"记进终态（或新加一个字段），**这条会红**。
    那时要做的不是删它，而是把新信号接进 `harness._exits_from_run`。
    """
    io = FakeIO(
        [TurnProposal(candidate=candidate()), TurnProposal(candidate=candidate())],
        gates=[GateVerdict("failed", "full"), GateVerdict("passed", "full")],
    )
    result = WorkerLoop(io, config=LoopConfig(max_turns=4, max_retries=3)).run(task_id="t", worker_id="w")

    assert io.verify_calls == 2  # 确实重试了一次（不是没走到）
    assert result.exit_reason == "passed"
    assert result.exit_reason not in {"retry", "retry_after_error"}


def test_exhausted_retries_land_on_degrade_single_agent_never_on_retry() -> None:
    """★ 同一个看门狗的另一半：重试预算用尽 → `degrade_single_agent`，也不是 `retry`。"""
    io = FakeIO(
        [TurnProposal(candidate=candidate()) for _ in range(8)],
        gates=[GateVerdict("failed", "full") for _ in range(8)],
    )
    loop = WorkerLoop(io, config=LoopConfig(max_turns=8, max_retries=2, max_reflections=1))
    result = loop.run(task_id="t", worker_id="w")

    assert io.verify_calls >= 3, io.verify_calls
    assert result.exit_reason == "degrade_single_agent", result.exit_reason


def test_the_compression_callback_is_the_injected_summarizer() -> None:
    """★ Wiring point: ``summarize`` is injected, so the loop never builds one.

    Without it, compression can only drop messages; the dropped span is never
    summarized. The loop must not know how to reach a model to summarize, so the
    callback has to arrive from outside.
    """
    # Sized so exactly one compaction happens: after the first drop the
    # transcript is under the threshold again, and the next turn reaches produce.
    policy = CompressionPolicy(context_length=200, max_tokens=2, pct=0.5, keep_recent=0)
    messages = tuple({"role": "user", "content": f"msg-{i:02d}-" + "x" * 16} for i in range(12))
    dropped_spans: list[tuple[dict, ...]] = []

    def summarize(dropped: tuple[dict, ...]) -> str:
        dropped_spans.append(tuple(dropped))
        return "SUMMARY-OF-DROPPED"

    seen: list[tuple[dict, ...]] = []

    class IO(FakeIO):
        def produce(self, state):  # type: ignore[no-untyped-def]
            seen.append(state.messages)
            return super().produce(state)

    loop = WorkerLoop(IO([TurnProposal(text="done")]), config=LoopConfig(max_turns=3), compression=policy, summarize=summarize)
    loop.run(task_id="t", worker_id="w", messages=messages)

    assert dropped_spans, "the summarizer was never called; compression only dropped"
    assert any(
        "SUMMARY-OF-DROPPED" in str(message.get("content", "")) for message in seen[-1]
    ), "the summary the callback returned never reached the transcript"


# --------------------------------------------------------------------------- #
# ★ lec9:237 layer 3 -- a rejected answer is fed back, not blindly re-sent
# --------------------------------------------------------------------------- #


class AnswerShapeError(Exception):
    """A format failure that carries *how to fix it* (the generic contract)."""

    reason = "format_error"

    def __init__(self, problem: str, got: str = "", code: str = "not_json") -> None:
        super().__init__(problem)
        self._feedback = {"code": code, "problem": problem, "got": got, "expected": '{"rows": [...]}'}

    def feedback(self) -> dict:
        return dict(self._feedback)


class BareShapeError(Exception):
    """A format failure with nothing to tell the model -- the old behaviour."""

    reason = "format_error"


class ChainedIO(FakeIO):
    """Fails with the queued faults, then answers."""

    def __init__(self, faults: list[BaseException]) -> None:
        super().__init__([TurnProposal(candidate=candidate("answer"))])
        self.faults = list(faults)
        self.seen: list[tuple] = []

    def produce(self, state):  # type: ignore[no-untyped-def]
        self.seen.append(tuple(state.messages))
        if self.faults:
            raise self.faults.pop(0)
        return super().produce(state)


def test_a_rejected_answer_with_a_fix_is_fed_back_and_answered_again() -> None:
    io = ChainedIO([AnswerShapeError("the first character is 'n', not '{'", got="I think it is fine")])
    result = WorkerLoop(io, config=LoopConfig(max_turns=4)).run(task_id="t", worker_id="w")

    assert result.candidate is not None  # the second answer was accepted
    assert len(io.seen) == 2
    injected = str(io.seen[1][-1].get("content", ""))
    assert "code: not_json" in injected  # ① an error code
    assert "problem: the first character is 'n'" in injected  # ② a readable reason
    assert "what you sent: I think it is fine" in injected  # ③ what it actually output
    assert 'required shape: {"rows": [...]}' in injected  # ④ what to output instead
    assert "Traceback" not in injected and "AnswerShapeError" not in injected  # ★ no traceback


def test_a_long_answer_is_truncated_in_the_feedback() -> None:
    io = ChainedIO([AnswerShapeError("not JSON", got="x" * 5000)])
    WorkerLoop(io, config=LoopConfig(max_turns=4)).run(task_id="t", worker_id="w")
    injected = str(io.seen[1][-1].get("content", ""))
    assert "…[truncated]" in injected
    assert len(injected) < 2000  # the transcript is not replayed back


def test_the_reformat_round_is_bounded_by_the_existing_budget() -> None:
    """★ One feedback round by default (max_reflections=1), then a named exit."""
    io = ChainedIO([AnswerShapeError("bad"), AnswerShapeError("bad"), AnswerShapeError("bad")])
    result = WorkerLoop(io, config=LoopConfig(max_turns=6)).run(task_id="t", worker_id="w")
    assert result.exit_reason == "fix_constraint"
    assert len(io.seen) == 2  # answer + one reformat; the third fault is never reached


def test_a_format_failure_without_a_fix_still_stops_immediately() -> None:
    """No feedback means no way to tell the model what to change -- hand it over."""
    io = ChainedIO([BareShapeError("the answer is not JSON")])
    result = WorkerLoop(io, config=LoopConfig(max_turns=4)).run(task_id="t", worker_id="w")
    assert result.exit_reason == "fix_constraint"
    assert len(io.seen) == 1


def test_the_prose_stays_in_state_errors_while_the_fix_goes_to_the_model() -> None:
    """★ Two representations, not one string reused twice."""
    io = ChainedIO([AnswerShapeError("the first character is 'n', not '{'", got="nope")])
    result = WorkerLoop(io, config=LoopConfig(max_turns=4)).run(task_id="t", worker_id="w")
    prose = "; ".join(result.errors)
    injected = str(io.seen[1][-1].get("content", ""))
    assert "AnswerShapeError" in prose  # the human log keeps the class name
    assert "AnswerShapeError" not in injected  # the model-facing text does not


# --------------------------------------------------------------------------- #
# ★ 工具轮的消息序列：assistant(tool_calls) 必须在它那批 tool 结果之前
#   （缺了这一条，严格端点直接 400；宽松端点放行时模型也看不到自己调过什么）
# --------------------------------------------------------------------------- #


def _round(io: FakeIO) -> list[dict]:
    """The transcript right after the first tool round."""
    for state in io.states:
        roles = [row.get("role") for row in state.messages]
        if roles[:1] == ["assistant"]:
            return list(state.messages)
    raise AssertionError("no tool round ran")


def test_the_tool_round_appends_the_assistant_row_before_its_results() -> None:
    """★ 断言的是【送出去的最后一条 assistant 消息】带 tool_calls，且其后每条 tool 的 id 都在里面。"""
    from bobanana6.agentloop.compress import message_sequence_problem

    io = FakeIO([TurnProposal(tool_calls=(read("a"), read("b")), text="looking"), TurnProposal(text="done")])
    WorkerLoop(io).run(task_id="t", worker_id="w")
    transcript = _round(io)

    assert transcript[0]["role"] == "assistant" and transcript[0]["content"] == "looking"
    declared = [call["id"] for call in transcript[0]["tool_calls"]]
    assert declared == ["a", "b"]
    assert [(row["role"], row["tool_call_id"]) for row in transcript[1:]] == [("tool", "a"), ("tool", "b")]
    # 顺序读：assistant 声明的名字/参数也在（模型能看到自己调过什么）
    assert transcript[0]["tool_calls"][0]["function"]["name"] == "read"
    assert json.loads(transcript[0]["tool_calls"][0]["function"]["arguments"]) == {"path": "a"}
    assert message_sequence_problem(transcript) == ""
    # ★ 反向：没有 assistant 行的旧形态**必须**被同一个检查判为不合规（这就是那个 400）
    assert message_sequence_problem(transcript[1:]) != ""


def test_a_write_barrier_still_pairs_every_result_with_one_assistant_row() -> None:
    """多段执行（写是屏障）之后，三条结果仍与同一条 assistant 的 id 对上、顺序一致。"""
    from bobanana6.agentloop.compress import message_sequence_problem

    io = FakeIO([TurnProposal(tool_calls=(read("a"), write("w"), read("b"))), TurnProposal(text="done")])
    WorkerLoop(io).run(task_id="t", worker_id="w")
    transcript = _round(io)

    assert all(len(batch) == 1 for batch in io.executed)  # 屏障语义没被改坏
    assert [call["id"] for call in transcript[0]["tool_calls"]] == ["a", "w", "b"]
    assert [row["tool_call_id"] for row in transcript[1:]] == ["a", "w", "b"]
    assert message_sequence_problem(transcript) == ""


def test_a_call_whose_execution_produced_no_result_still_gets_a_pair() -> None:
    """★ 没有结果的调用（被守卫拦下 / 执行没回）也必须有一条 tool 行 ——

    `order_results` 会给每个声明的调用合成一条；assistant 行声明的是**全部**调用，
    所以"声明了但没结果"与"有结果但没声明"两边都不会出现（`AGENTS §7①`）。
    """
    from bobanana6.agentloop.compress import message_sequence_problem

    class SilentIO(FakeIO):
        def execute(self, calls):  # type: ignore[no-untyped-def]
            self.executed.append(tuple(calls))
            return ()

    io = SilentIO([TurnProposal(tool_calls=(read("a"), read("b"))), TurnProposal(text="done")])
    WorkerLoop(io).run(task_id="t", worker_id="w")
    transcript = _round(io)
    assert [row["tool_call_id"] for row in transcript[1:]] == ["a", "b"]
    assert transcript[1]["content"]  # 合成结果不是空的（模型要能看到"为什么没有内容"）
    assert message_sequence_problem(transcript) == ""


def test_every_appended_round_is_a_pair_when_the_model_calls_twice() -> None:
    """两轮工具调用 ⇒ 两对 assistant(tool_calls) + 它们的结果，交错也合规。"""
    from bobanana6.agentloop.compress import message_sequence_problem

    io = FakeIO(
        [
            TurnProposal(tool_calls=(read("a1"), read("a2"))),
            TurnProposal(tool_calls=(read("b1"),)),
            TurnProposal(text="done"),
        ]
    )
    WorkerLoop(io).run(task_id="t", worker_id="w")
    final = io.states[-1].messages
    assert [row.get("role") for row in final] == ["assistant", "tool", "tool", "assistant", "tool"]
    assert message_sequence_problem(final) == ""


def test_an_empty_tool_call_list_appends_nothing() -> None:
    """空 tool_calls 不是工具轮：不许凭空插一条 assistant 行。"""
    io = FakeIO([TurnProposal(tool_calls=()), TurnProposal(text="done")])
    WorkerLoop(io).run(task_id="t", worker_id="w")
    assert io.states[-1].messages == ()


# --------------------------------------------------------------------------- #
# ★ 每个 tool_call_id 恰好一条 tool 行（不少：补错误结果；不多：重复 id 也不重复答）
# --------------------------------------------------------------------------- #


def test_every_declared_id_gets_exactly_one_tool_row() -> None:
    """★ 断言"不少不多"：声明的每个 id 在紧随的 tool 行里**恰好出现一次**。"""
    from bobanana6.agentloop.compress import message_sequence_problem

    io = FakeIO([TurnProposal(tool_calls=(read("a"), read("b"), read("c"))), TurnProposal(text="done")])
    WorkerLoop(io).run(task_id="t", worker_id="w")
    transcript = _round(io)
    declared = [call["id"] for call in transcript[0]["tool_calls"]]
    answered = [row["tool_call_id"] for row in transcript[1:]]
    assert sorted(declared) == answered
    assert len(answered) == len(set(answered))
    assert message_sequence_problem(transcript) == ""


def test_a_duplicate_tool_row_is_a_named_sequence_problem() -> None:
    """「不能多」的那一半：同一个 id 回两次 ⇒ 具名（不是"没声明"）。"""
    from bobanana6.agentloop.compress import message_sequence_problem

    transcript = (
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "r"},
        {"role": "tool", "tool_call_id": "a", "content": "r again"},
    )
    assert "was already answered by an earlier tool row" in message_sequence_problem(transcript)


def test_a_failed_tool_call_still_yields_a_row_with_an_explanation() -> None:
    """★「不少」的那一半：执行失败 / 没有结果时补一条**说清为什么**的 tool 行。"""
    from bobanana6.agentloop.compress import message_sequence_problem

    class FailingIO(FakeIO):
        def execute(self, calls):  # type: ignore[no-untyped-def]
            self.executed.append(tuple(calls))
            return tuple(ToolResult(call_id=c.call_id, ok=False, content="boom: the tool raised") for c in calls)

    io = FailingIO([TurnProposal(tool_calls=(read("a"), read("b"))), TurnProposal(text="done")])
    WorkerLoop(io).run(task_id="t", worker_id="w")
    transcript = _round(io)
    assert [row["tool_call_id"] for row in transcript[1:]] == ["a", "b"]
    assert all("boom" in row["content"] for row in transcript[1:])
    assert message_sequence_problem(transcript) == ""


# --------------------------------------------------------------------------- #
# ★ Action 契约：一轮里有一条调用的声明不可用（截断/坏 JSON）⇒ 整批都不执行
#   （Pi 对 stopReason="length" 整批失败；Hermes 对一批里 JSON 坏的给每条回 Skipped）
# --------------------------------------------------------------------------- #


def _declared(call_id: str, raw: str, *, effect: str = "read") -> ToolCall:
    return ToolCall(call_id=call_id, name="read", arguments={}, raw_arguments=raw, effect=effect)


def test_a_truncated_declaration_holds_back_the_whole_batch() -> None:
    """★ 反向读数的正面：坏的那条**不能**让兄弟静默跑掉，但每条仍要各得一行。"""
    from bobanana6.agentloop.compress import message_sequence_problem

    good = _declared("ok", '{"path": "good"}')
    cut = _declared("cut", '{"path": "data/fi')  # 截断（JSON 不完整）
    io = FakeIO([TurnProposal(tool_calls=(good, cut)), TurnProposal(text="done")])
    WorkerLoop(io).run(task_id="t", worker_id="w")
    transcript = _round(io)

    assert io.executed == []  # ★ 一个都没执行（改前：[(good, cut)]，good 被跑了）
    assert [row["tool_call_id"] for row in transcript[1:]] == ["ok", "cut"]
    assert all("usable JSON object" in row["content"] for row in transcript[1:])
    assert message_sequence_problem(transcript) == ""


def test_a_truncated_batch_with_a_write_executes_nothing() -> None:
    """换块类型：写调用也在同一批里 ⇒ 写也不能落地（静默执行写最危险）。"""
    write_ok = ToolCall(
        call_id="w",
        name="write_file",
        arguments={"path": "out.csv"},
        raw_arguments='{"path": "out.csv"}',
        effect="write",
    )
    cut = _declared("cut", "{broken")
    io = FakeIO([TurnProposal(tool_calls=(write_ok, cut)), TurnProposal(text="done")])
    WorkerLoop(io).run(task_id="t", worker_id="w")
    assert io.executed == []


def test_a_usable_batch_still_executes_every_call() -> None:
    """★ 正向（不许误伤）：全好的批按原样执行，规模 1 / 2 / 5 都不变。"""
    for count in (1, 2, 5):
        calls = tuple(_declared(f"c{i}", '{"path": "x"}') for i in range(count))
        io = FakeIO([TurnProposal(tool_calls=calls), TurnProposal(text="done")])
        WorkerLoop(io).run(task_id="t", worker_id="w")
        executed = [call.call_id for batch in io.executed for call in batch]
        assert executed == [f"c{i}" for i in range(count)]


# --------------------------------------------------------------------------- #
# ★ 收尾不变式：交付候选的**每一条**路径都要闭合 transcript 尾巴
#   （`close_transcript_tail` 原来只在 `passed` 那条分支上跑）
# --------------------------------------------------------------------------- #


def test_transcript_tail_problem_names_an_open_tail() -> None:
    assert transcript_tail_problem(()) == ""
    closed = ({"role": "tool", "content": "r"}, {"role": "assistant", "content": "a", "candidate": True})
    assert transcript_tail_problem(closed) == ""
    assert "tool row" in transcript_tail_problem(({"role": "tool", "content": "r"},))
    assert "unanswered" in transcript_tail_problem(({"role": "assistant", "tool_calls": [{"id": "a"}]},))
    assert "without a closing" in transcript_tail_problem(({"role": "user", "content": "u"},))


def test_the_budget_exit_closes_the_tail_it_delivers() -> None:
    """★ turn 1 是工具轮（尾巴是 tool 行），turn 2/3 的候选都被拒 ⇒ 预算出口交付 withheld。

    改前：`result.messages` 停在 tool 行（交付了候选却没有收尾行）。
    改后：收尾行由 `run` 在**唯一交付点**补上。
    """
    io = FakeIO(
        [
            TurnProposal(tool_calls=(read("a"),)),
            TurnProposal(candidate=candidate("v1")),
            TurnProposal(candidate=candidate("v2")),
        ],
        gates=[GateVerdict("failed", "full"), GateVerdict("failed", "full")],
    )
    loop = WorkerLoop(io, config=LoopConfig(max_turns=3, max_retries=9, max_reflections=9))
    result = loop.run(task_id="t", worker_id="w")

    assert result.exit_reason == "budget_exhausted_withheld_candidate"
    assert result.candidate is not None
    assert [row["role"] for row in result.messages] == ["assistant", "tool", "assistant"]
    assert result.messages[-1].get("candidate") is True
    assert transcript_tail_problem(result.messages) == ""


def test_the_passing_exit_still_closes_the_tail_once() -> None:
    """★ 反向：通过出口**不许**因为搬家而补两条收尾行（幂等）。"""
    io = FakeIO([TurnProposal(candidate=candidate())])
    result = WorkerLoop(io).run(task_id="t", worker_id="w")
    assert result.exit_reason == "passed"
    assert [row.get("candidate") for row in result.messages] == [True]
    assert transcript_tail_problem(result.messages) == ""


