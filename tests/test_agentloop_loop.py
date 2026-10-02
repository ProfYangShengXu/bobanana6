"""The worker loop: phase protocol, restart cap, closing invariants, budget fallback."""

from __future__ import annotations

from bobanana6.agentloop.compress import CompressionPolicy
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
from bobanana6.agentloop.state import InMemoryCheckpointStore, load_checkpoint
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
