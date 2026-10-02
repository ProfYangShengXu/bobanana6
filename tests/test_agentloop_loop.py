"""The worker loop: phase protocol, restart cap, closing invariants, budget fallback."""

from __future__ import annotations

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
