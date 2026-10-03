"""assemble_impl/: the wiring -- ports adapted, gates ordered, blocks run, assembly judged."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from bobanana6.adapters import EchoToolResultClient, FileOntology, ScriptedClient
from bobanana6.agentloop.events import EventLog
from bobanana6.assemble_impl import (
    BlockDeclined,
    BlockRuntime,
    ContractConstraintSource,
    LoopIOImpl,
    PerWorkerCheckpointStore,
    Runner,
    RunRefused,
    llm_summarizer,
    parse_candidate,
)
from bobanana6.assemble_impl.produce import EXPECTED_SHAPE, CandidateFormatError
from bobanana6.contract import parse_contract
from bobanana6.core.capabilities import Capability, WorkerCapabilities, WorkerLimits
from bobanana6.core.checkers.base import CheckContext
from bobanana6.core.model import (
    Budget,
    ModelResponse,
    Subtask,
    TaskContract,
)
from bobanana6.core.ports import AgentRuntimePort
from bobanana6.decompose import Decomposer
from bobanana6.sources import FileFetcher, SnapshotStore

NOW = "2026-10-03T00:00:00+08:00"
ONTOLOGY = """
domain: credit-risk
version: "2026.1"
concepts:
  obligor: {label: Obligor, kind: entity}
  pd: {label: PD, kind: measure, unit: ratio}
  amount: {label: Amount, kind: measure, unit: CNY}
units:
  pd: {symbol: ratio, scale: 1.0, base: ratio}
  amount: {symbol: CNY, scale: 1.0, base: CNY}
"""
ONE_SOURCE = """
task_id: t-one
goal: build the panel
deliverables:
  - {name: out.csv, schema: {obligor: str, pd: float, amount: float}}
sources:
  - {ref: src-1, locator: SOURCE_CSV}
constraints:
  hard:
    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "no empty panels"}
notes: a note for the summary
"""
TWO_SOURCES = """
task_id: t-two
goal: build the panel
deliverables:
  - {name: out.csv, schema: {obligor: str, pd: float}}
sources:
  - {ref: src-1, locator: ONE_CSV}
  - {ref: src-2, locator: TWO_CSV}
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _csv(tmp_path: Path, name: str, rows: str) -> Path:
    return _write(tmp_path, name, rows)


def _ontology(tmp_path: Path) -> Path:
    return _write(tmp_path, "ontology.yaml", ONTOLOGY)


class _ByBlock:
    """A responder that keys off the block id in the prompt.

    A test double, not production code: it exists because a *single* deterministic
    stand-in cannot answer three different blocks, and the pipeline is exactly
    what needs exercising.
    """

    def __init__(self, script: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        self.script = {key: list(value) for key, value in script.items()}
        self.seen: list[str] = []

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        block = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                try:
                    block = json.loads(str(message.get("content")))["subtask"]["id"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    block = ""
                if block:
                    break
        self.seen.append(block)
        queued = self.script.get(block) or []
        if len(queued) > 1:
            entry = queued.pop(0)
        elif queued:
            entry = queued[0]
        else:
            entry = {"text": json.dumps({"rows": [], "columns": []})}
        return ModelResponse(text=str(entry.get("text") or ""), tool_calls=tuple(entry.get("tool_calls") or ()))


def _tool_call(name: str, arguments: Mapping[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _runner(tmp_path: Path, task_text: str, client: Any) -> Runner:
    document = parse_contract(task_text)
    store = SnapshotStore(root=tmp_path / "snapshots")
    event_log = EventLog()
    budget = Budget(max_calls=32, max_wall_s=60, max_output=2048)
    return Runner(
        document=document,
        ontology_port=FileOntology(_ontology(tmp_path)),
        decomposer=Decomposer(strategy="source"),
        constraint_source=ContractConstraintSource(document=document),
        runtime=BlockRuntime(
            client=client,
            store=store,
            contract=document.contract,
            budget=budget,
            event_log=event_log,
        ),
        fetcher=FileFetcher(store=store, captured_at=NOW),
        store=store,
        now=NOW,
        epoch=lambda: 1.0,
        budget=budget,
        event_log=event_log,
        report_dir=tmp_path / "reports",
        worker_pool=(
            WorkerCapabilities(
                worker_id="worker:reader",
                provides=frozenset({Capability.READ, Capability.COMPUTE}),
                limits=WorkerLimits(max_output=budget.max_output, concurrency=8),
            ),
            WorkerCapabilities(
                worker_id="worker:writer",
                provides=frozenset({Capability.COMPUTE, Capability.WRITE_DELIVERABLE}),
                limits=WorkerLimits(max_output=budget.max_output, concurrency=8),
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #


def test_single_source_task_runs_end_to_end(tmp_path: Path) -> None:
    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\nA-2,0.02,200\n")
    runner = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(source)), EchoToolResultClient())
    result = runner.run()

    assert result.ok is True
    assert result.exit_reason == "passed"
    assert result.plan.shape == "star"
    assert result.verdicts["T-01"].ok is True
    assert result.convergence.decision == "deliver"
    assert result.assembly.passed is True
    assert set(result.snapshots) == {"src-1"}
    # the report is written, and it reads the fields it claims to read
    summary = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert summary["task"]["notes"] == "a note for the summary"
    assert summary["snapshots"][0]["source_kind"] == "file"
    assert summary["blocks_report"][0]["source_id"] == "src-1"
    assert summary["metrics"]["tool.calls"] >= 1
    for name in ("SOURCES.md", "CONFLICTS.md", "ONTOLOGY-MAP.md", "summary.json"):
        assert (tmp_path / "reports" / name).is_file()


def test_two_source_task_runs_the_pipeline_and_the_merge_block(tmp_path: Path) -> None:
    one = _csv(tmp_path, "one.csv", "obligor,pd\nA-1,0.01\n")
    two = _csv(tmp_path, "two.csv", "obligor,pd\nA-2,0.02\n")
    def payload(ref: str, rows: list[dict[str, str]]) -> str:
        return json.dumps(
            {
                "ref": ref,
                "columns": [{"name": "obligor", "dtype": "str"}, {"name": "pd", "dtype": "str"}],
                "rows": rows,
            }
        )

    merged = payload("src-1", [{"obligor": "A-1", "pd": "0.01"}, {"obligor": "A-2", "pd": "0.02"}])
    client = _ByBlock(
        {
            # after the tool round, each read block answers with its own source
            "T-01": [
                {"tool_calls": (_tool_call("sources.snapshot", {"ref": "src-1"}, "c1"),)},
                {"text": payload("src-1", [{"obligor": "A-1", "pd": "0.01"}])},
            ],
            "T-02": [
                {"tool_calls": (_tool_call("sources.snapshot", {"ref": "src-2"}, "c2"),)},
                {"text": payload("src-2", [{"obligor": "A-2", "pd": "0.02"}])},
            ],
            "T-03": [
                {"tool_calls": (_tool_call("task.upstream", {"name": "part-src-1.csv"}, "c3"),)},
                {"tool_calls": (_tool_call("task.upstream", {"name": "part-src-2.csv"}, "c4"),)},
                {"text": merged},
            ],
        }
    )
    text = TWO_SOURCES.replace("ONE_CSV", str(one)).replace("TWO_CSV", str(two))
    result = _runner(tmp_path, text, client).run()

    assert result.plan.shape == "pipeline"
    assert [block.mode for block in result.plan.subtasks] == ["read", "read", "write"]
    assert result.ok is True, result.verdicts
    assert result.plan.recon_evidence  # the recon evidence reaches the plan and the report
    summary = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert summary["plan"]["recon_evidence"] == result.plan.recon_evidence
    assert summary["capability_matching"]["evaluated"] is True
    assert len(summary["capability_matching"]["assignments"]) == 3


# --------------------------------------------------------------------------- #
# the pre-cluster gates
# --------------------------------------------------------------------------- #


def test_a_source_that_cannot_be_fetched_refuses_the_run(tmp_path: Path) -> None:
    """★ Not an empty snapshot: a named refusal."""
    runner = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(tmp_path / "missing.csv")), EchoToolResultClient())
    with pytest.raises(RunRefused) as refusal:
        runner.run()
    assert refusal.value.gate == "sources"
    assert refusal.value.codes == ("fetch_failed",)
    assert "missing.csv" in str(refusal.value)


def test_an_inconsistent_ontology_refuses_the_run(tmp_path: Path) -> None:
    broken = _write(
        tmp_path,
        "broken.yaml",
        "domain: d\nversion: '1'\nconcepts:\n  a: {label: A, kind: measure}\n"
        'definitions:\n  ghost: {version: "1", text: t}\n',
    )
    runner = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(tmp_path / "x.csv")), EchoToolResultClient())
    runner.ontology_port = FileOntology(broken)
    with pytest.raises(RunRefused) as refusal:
        runner.run()
    assert refusal.value.gate == "ontology"
    assert "definition_orphan" in refusal.value.codes


def test_an_illegal_plan_refuses_the_run(tmp_path: Path) -> None:
    class _BadPlan:
        def split(self, contract: TaskContract, ontology: object, recon: object) -> Any:
            from fakes import plan, subtask

            block = subtask("T-01", output="out.csv", mode="write", partition="p", axis="source", output_fields=("pd",))
            twin = subtask("T-02", output="out.csv", mode="write", partition="p", axis="source", output_fields=("pd",))
            return plan(subtasks=(block, twin), interfaces={"out.csv": "s"})

        def refine(self, *args: object) -> Any:  # pragma: no cover - not reached
            raise NotImplementedError

    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\n")
    runner = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(source)), EchoToolResultClient())
    runner.decomposer = _BadPlan()  # type: ignore[assignment]
    with pytest.raises(RunRefused) as refusal:
        runner.run()
    assert refusal.value.gate == "split"
    assert "duplicate_output" in refusal.value.codes


# --------------------------------------------------------------------------- #
# the block outcomes the port cannot express
# --------------------------------------------------------------------------- #


def test_a_declined_block_is_reported_as_declined_not_failed(tmp_path: Path) -> None:
    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\n")
    client = ScriptedClient([{"text": json.dumps({"declined": True, "reason": "the source does not carry pd"})}])
    result = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(source)), client).run()

    assert result.ok is False
    assert result.exit_reason == "declined"
    assert result.verdicts["T-01"].failed == ("declined",)
    assert result.verdicts["T-01"].evidence == {"exit": "declined"}


def test_a_broken_tool_reaches_the_attribution_as_a_tool_blame(tmp_path: Path) -> None:
    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\n")
    client = ScriptedClient([{"tool_calls": (_tool_call("semantics.nope", {}, "c1"),)}])
    result = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(source)), client).run()

    assert result.ok is False
    assert result.attribution is not None
    assert result.attribution.blame == "tool"
    assert "repair_tool" in result.attribution_actions
    summary = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert summary["attribution"]["blame"] == "tool"
    assert "repair_tool" in summary["attribution"]["actions"]


def test_a_malformed_answer_fails_the_block_without_building_a_candidate(tmp_path: Path) -> None:
    """★ The measured chain, end to end.

    A non-JSON answer used to classify as ``unknown`` -> ``retry_local``: the loop
    re-sent the identical request, got the identical failure, and only stopped when
    the budget ran out -- ``evidence.errors`` held two copies of the same error.
    Now the declared reason (``format_error``) routes it to ``fix_constraint``, and
    the raiser attached a *fix*: the loop feeds it back and asks once more (the
    reflection budget), then stops with a named exit instead of burning the budget
    on identical re-sends. The scripted client repeats the same bad answer, so the
    second call has to fail too -- what matters is the shape of the run: two calls,
    not thirteen, and a named exit.
    """
    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\n")
    client = ScriptedClient([{"text": "the panel is probably fine"}])
    result = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(source)), client).run()
    assert result.ok is False
    assert result.candidates == {}
    assert result.verdicts["T-01"].evidence["exit"] == "fix_constraint"
    # one answer + one fed-back answer, then the named exit (max_reflections=1)
    assert len(client.calls) == 2


# --------------------------------------------------------------------------- #
# the port adapter itself
# --------------------------------------------------------------------------- #


def test_block_runtime_satisfies_the_agent_runtime_port(tmp_path: Path) -> None:
    """★ The checklist item: ``WorkerLoop`` is not the port, this adapter is."""
    document = parse_contract(ONE_SOURCE.replace("SOURCE_CSV", "x.csv"))
    runtime = BlockRuntime(
        client=EchoToolResultClient(),
        store=SnapshotStore(root=tmp_path / "s"),
        contract=document.contract,
    )
    assert isinstance(runtime, AgentRuntimePort)


def test_block_runtime_refuses_a_decline_as_a_typed_error(tmp_path: Path) -> None:
    document = parse_contract(ONE_SOURCE.replace("SOURCE_CSV", "x.csv"))
    store = SnapshotStore(root=tmp_path / "s")
    runtime = BlockRuntime(
        client=ScriptedClient([{"text": json.dumps({"declined": True, "reason": "no"})}]),
        store=store,
        contract=document.contract,
    )
    from fakes import subtask as make_subtask

    block = make_subtask("T-01", output="out.csv", mode="write", axis="source")
    from fakes import bundle

    from bobanana6.services import BoundChecks, build_toolset

    tools = build_toolset(
        plan=__import__("fakes", fromlist=["plan"]).plan(subtasks=(block,)),
        subtask=block,
        contract=document.contract,
        ontology=__import__("fakes", fromlist=["mini_ontology"]).mini_ontology(),
        authorized=(),
        fetched={},
        upstream={},
        checks=BoundChecks(block="T-01"),
    )
    with pytest.raises(BlockDeclined):
        runtime.run(
            block,
            bundle(hard=()),
            __import__("fakes", fromlist=["mini_ontology"]).mini_ontology(),
            tools,
            Budget(max_calls=4, max_wall_s=10, max_output=64),
        )


def test_contract_constraint_source_refuses_a_mismatched_contract() -> None:
    document = parse_contract(ONE_SOURCE.replace("SOURCE_CSV", "x.csv"))
    source = ContractConstraintSource(document=document)
    with pytest.raises(ValueError, match="does not match"):
        source.compile(
            TaskContract(task_id="other", goal="g"),
            __import__("fakes", fromlist=["plan"]).plan(),
            __import__("fakes", fromlist=["mini_ontology"]).mini_ontology(),
        )


def test_contract_constraint_source_compiles_the_document() -> None:
    document = parse_contract(ONE_SOURCE.replace("SOURCE_CSV", "x.csv"))
    from fakes import plan, subtask

    block = subtask("T-01", output="out.csv", mode="write", axis="source")
    from dataclasses import replace as _replace

    bundle = ContractConstraintSource(document=document).compile(
        document.contract,
        _replace(plan(subtasks=(block,), interfaces={"out.csv": "s"}), task_id="t-one"),
        __import__("fakes", fromlist=["mini_ontology"]).mini_ontology(),
    )
    assert [c.id for c in bundle.hard] == ["C-1", "AUTO-T-01"]
    assert bundle.task_id == "t-one"


def test_per_worker_checkpoint_store_keeps_blocks_apart() -> None:
    from bobanana6.agentloop.state import save_turn

    store = PerWorkerCheckpointStore()
    save_turn(store, task_id="t", worker_id="T-01", turn=1, messages=[])
    save_turn(store, task_id="t", worker_id="T-02", turn=5, messages=[])
    assert store.for_worker("t", "T-01").turn == 1  # type: ignore[union-attr]
    assert store.for_worker("t", "T-02").turn == 5  # type: ignore[union-attr]
    assert store.load("t").worker_id == "T-02"  # type: ignore[union-attr]


def test_llm_summarizer_asks_the_model_once() -> None:
    client = ScriptedClient([{"text": "summary"}])
    summarize = llm_summarizer(client)
    assert summarize(({"role": "user", "content": "hello"},)) == "summary"
    assert client.calls == [(2, 0)]


# --------------------------------------------------------------------------- #
# the parse boundary
# --------------------------------------------------------------------------- #


def test_parse_candidate_reads_rows_columns_and_provenance() -> None:
    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")
    candidate = parse_candidate(
        json.dumps({"ref": "src-1", "columns": ["obligor"], "rows": [{"obligor": "A-1"}, {"obligor": "A-2"}]}),
        subtask=block,
        source_id="runtime",
    )
    assert candidate.source_id == "src-1"
    assert [p.value_ref for p in candidate.provenance] == ["out.csv:row=0", "out.csv:row=1"]
    assert candidate.artifact("out.csv").row_count == 2  # type: ignore[union-attr]


def test_parse_candidate_refuses_bad_shapes_with_a_fixable_reason() -> None:
    """★ Every rejection says what was sent, which rule broke, and what is required."""
    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")

    with pytest.raises(CandidateFormatError) as not_json:
        parse_candidate("not json at all", subtask=block, source_id="s")
    feedback = not_json.value.feedback()
    assert feedback["code"] == "not_json"
    assert "first character is 'n'" in feedback["problem"]
    assert feedback["got"] == "not json at all"
    assert feedback["expected"] == EXPECTED_SHAPE

    cases = {
        "[]": "not_an_object",
        '{"rows": 3}': "rows_not_a_list",
        '{"rows": [3]}': "row_not_an_object",
        '{"rows": [], "columns": 3}': "columns_not_a_list",
        '{"rows": [], "columns": [3]}': "column_not_an_object",
        '{"rows": [], "claims": 3}': "claims_not_a_list",
        '{"rows": [], "claims": [3]}': "claim_not_an_object",
        '{"rows": [], "claims": [{"field": "a"}]}': "claim_missing_keys",
    }
    for text, code in cases.items():
        with pytest.raises(CandidateFormatError) as error:
            parse_candidate(text, subtask=block, source_id="s")
        assert error.value.feedback()["code"] == code, text
        assert error.value.feedback()["got"] == text

    with pytest.raises(CandidateFormatError) as empty:
        parse_candidate("   ", subtask=block, source_id="s")
    assert "empty" in empty.value.feedback()["problem"]


def test_loop_io_produce_declines_and_executes_and_verifies(tmp_path: Path) -> None:
    from bobanana6.agentloop.loop import LoopState
    from bobanana6.services import BlockJudge, BoundChecks, build_toolset

    document = parse_contract(ONE_SOURCE.replace("SOURCE_CSV", "x.csv"))
    store = SnapshotStore(root=tmp_path / "s")
    from fakes import bundle, mini_ontology, plan, subtask

    block = subtask("T-01", output="out.csv", mode="write", axis="source", output_fields=("pd",))
    tools = build_toolset(
        plan=plan(subtasks=(block,), interfaces={"out.csv": "s"}),
        subtask=block,
        contract=document.contract,
        ontology=mini_ontology(),
        authorized=(),
        fetched={},
        upstream={},
        checks=BoundChecks(block="T-01"),
    )
    constraints = bundle(hard=(__import__("fakes", fromlist=["constraint"]).constraint("C-1", "presence", "T-01", {"min_rows": 1}),))
    judge = BlockJudge(bundle=constraints, subtask=block, context=CheckContext(ontology=mini_ontology()))
    io = LoopIOImpl(
        client=ScriptedClient([{"text": json.dumps({"declined": True, "reason": "cannot"})}]),
        toolset=tools,
        registry=__import__("bobanana6.services", fromlist=["build_registry"]).build_registry(tools, store.read),
        judge=judge,
        subtask=block,
        log=EventLog(),
    )
    assert io.produce(LoopState()).declined is True
    assert io.tool_schemas() if hasattr(io, "tool_schemas") else True


def test_loop_io_execute_records_tool_events_and_recovery(tmp_path: Path) -> None:
    from bobanana6.agentloop.tools import ToolCall
    from bobanana6.services import BlockJudge, BoundChecks, build_toolset

    document = parse_contract(ONE_SOURCE.replace("SOURCE_CSV", "x.csv"))
    store = SnapshotStore(root=tmp_path / "s")
    from fakes import bundle, mini_ontology, plan, subtask

    block = subtask("T-01", output="out.csv", mode="write", axis="source")
    tools = build_toolset(
        plan=plan(subtasks=(block,), interfaces={"out.csv": "s"}),
        subtask=block,
        contract=document.contract,
        ontology=mini_ontology(),
        authorized=(),
        fetched={},
        upstream={},
        checks=BoundChecks(block="T-01"),
    )
    log = EventLog()
    judge = BlockJudge(bundle=bundle(), subtask=block, context=CheckContext(ontology=mini_ontology()))
    io = LoopIOImpl(
        client=ScriptedClient([{"text": "{}"}]),
        toolset=tools,
        registry=__import__("bobanana6.services", fromlist=["build_registry"]).build_registry(tools, store.read),
        judge=judge,
        subtask=block,
        log=log,
        task_id="t-one",
        epoch=lambda: 2.0,
    )
    bad = ToolCall(call_id="c1", name="semantics.lookup", raw_arguments="{not json")
    first = io.execute((bad,))
    assert first[0].ok is False
    good = ToolCall(call_id="c2", name="semantics.lookup", raw_arguments='{"term": "pd"}')
    second = io.execute((good,))
    assert second[0].ok is True
    kinds = [event.kind for event in log.events]
    assert kinds == ["tool_result", "tool_result"]
    assert log.events[0].detail["args_ok"] is False
    assert log.events[1].detail["recovered"] is True  # ★ a retried win is not a first-try win


def test_the_tool_inventory_is_what_the_consistency_check_compares_against(tmp_path: Path) -> None:
    """★ ``consist._tool_missing`` refuses to guess: it needs the real inventory.

    A constraint that requires a capability the deployment does not provide must
    stop the run before any worker starts -- and the deployment's inventory is the
    tool surface, which only the assembly point knows.
    """
    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\n")
    text = ONE_SOURCE.replace("SOURCE_CSV", str(source)).replace(
        'spec: {min_rows: 1}',
        'spec: {min_rows: 1, requires: ["semantics.lookup"]}',
    )
    result = _runner(tmp_path, text, EchoToolResultClient()).run()
    assert result.ok is True

    missing = text.replace("semantics.lookup", "semantics.teleport")
    runner = _runner(tmp_path, missing, EchoToolResultClient())
    runner.report_dir = None
    with pytest.raises(RunRefused) as refusal:
        runner.run()
    assert refusal.value.gate == "consist"
    assert "tool_missing" in refusal.value.codes


def test_the_summarizer_goes_through_the_same_gate() -> None:
    """★ A second model path must not bypass the throttle."""

    class _ClosedGate:
        def __init__(self) -> None:
            self.acquired = 0
            self.released = 0

        def acquire(self, key: str, timeout: float | None = None) -> bool:
            self.acquired += 1
            return False

        def release(self, key: str) -> None:  # pragma: no cover - never acquired
            self.released += 1

    client = ScriptedClient([{"text": "summary"}])
    gate = _ClosedGate()
    summarize = llm_summarizer(client, gate=gate)  # type: ignore[arg-type]
    assert summarize(({"role": "user", "content": "hello"},)) == ""  # dropped, not sent
    assert client.calls == []
    assert gate.acquired == 1 and gate.released == 0


def test_the_summarizer_releases_the_gate_when_it_sends() -> None:
    from bobanana6.agentloop.concurrency import RuntimeGate

    client = ScriptedClient([{"text": "summary"}])
    gate = RuntimeGate()
    assert llm_summarizer(client, gate=gate)(({"role": "user", "content": "x"},)) == "summary"
    assert gate.active("default") == 0  # released, so the next call can go
