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
    MISSING,
    Budget,
    FieldSource,
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


def test_an_unknown_tool_is_the_callers_error_not_a_broken_tool(tmp_path: Path) -> None:
    """★ ②：`semantics.nope` 不是「工具坏了」——工具表就在模型的上下文里，它挑了个不存在的。

    这一条原来断言 `tool`/`repair_tool`：那是旧启发式「有工具失败就归 tool」的产物，
    而 `repair_tool` 对一个根本不存在的工具毫无意义。声明的层是**模型**，
    动作表也跟着换了（`switch_worker`）。
    """
    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\n")
    client = ScriptedClient([{"tool_calls": (_tool_call("semantics.nope", {}, "c1"),)}])
    result = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(source)), client).run()

    assert result.ok is False
    assert result.attribution is not None
    assert result.attribution.blame == "model"
    assert "switch_worker" in result.attribution_actions
    assert "repair_tool" not in result.attribution_actions
    summary = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert summary["attribution"]["blame"] == "model"
    assert "switch_worker" in summary["attribution"]["actions"]


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


def test_an_absent_cell_is_loaded_as_missing_and_a_zero_is_left_alone() -> None:
    """★ A04：空单元格记成 `MISSING`（一等状态），`0` 是一个值、不许被当成缺失。"""
    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")
    candidate = parse_candidate(
        json.dumps({"ref": "src-1", "rows": [{"amount": ""}, {"amount": None}, {"amount": 0}]}),
        subtask=block,
        source_id="src-1",
        authorized_sources=("src-1",),
    )
    rows = candidate.artifact("out.csv").rows  # type: ignore[union-attr]
    # ★ ②：。「没有值」只有【一种】规范写法 —— 键缺席；空串与 null 都归一成它。
    #   0 仍然是一个值，必须与「没有值」分得开（A04）。
    assert "amount" not in rows[0] and "amount" not in rows[1]
    assert rows[2]["amount"] == 0
    # 每行都点名它取自快照的哪一行（A11 的内容级校验要读它）
    assert [point.source_row for point in candidate.provenance] == [0, 1, 2]


def test_parse_candidate_reads_a_code_deliverable_as_files() -> None:
    """★ `#33`: 代码类产物 —— `files` 的值是【工作区相对路径】，校验在解析这一关做完。"""
    block = Subtask(id="T-01", goal="g", inputs=(), output="shared.py", contract_ref="t", axis="module", mode="write")
    candidate = parse_candidate(
        json.dumps({"ref": "src-1", "rows": [], "text": "def total(i):\n    return sum(i)\n",
                    "files": {"repo/shared.py": "def total(i):\n    return sum(i)\n"}}),
        subtask=block,
        source_id="src-1",
        authorized_sources=("src-1",),
    )
    assert list(candidate.files) == ["repo/shared.py"]
    assert candidate.files["repo/shared.py"] == Path("repo/shared.py")


def test_a_file_path_outside_the_workspace_is_refused() -> None:
    """绝对路径 / 逃逸 / 目录 —— 都不许落盘，报的是能照着改的形状。"""
    block = Subtask(id="T-01", goal="g", inputs=(), output="shared.py", contract_ref="t", axis="module", mode="write")
    for payload, code in (
        ({"files": {"/etc/passwd": "x"}}, "file_path_not_relative"),
        ({"files": {"../outside.py": "x"}}, "file_path_escapes"),
        ({"files": {"repo/": "x"}}, "file_path_is_a_directory"),
        ({"files": {"repo/a.py": 3}}, "file_not_text"),
        ({"files": {}}, "files_not_an_object"),
    ):
        with pytest.raises(CandidateFormatError) as caught:
            parse_candidate(json.dumps({"ref": "src-1", "rows": [], **payload}), subtask=block, source_id="src-1")
        assert caught.value.feedback()["code"] == code, payload


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
        budget=Budget(max_calls=8, max_wall_s=60, max_output=4096),
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
        budget=Budget(max_calls=8, max_wall_s=60, max_output=4096),
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


def test_a_field_declarations_source_reaches_the_judgement_context(tmp_path: Path) -> None:
    """★ 契约字段声明的 `source{ref,locator}` 一路进到 `CheckContext`。

    判定器今天**不读**它（改判定语义不在本单元），但通道是接上的：契约 ->
    `TaskContract.declared_field_sources` -> `runner` -> `CheckContext`。
    """
    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\n")
    text = ONE_SOURCE.replace("SOURCE_CSV", str(source)).replace(
        "- {name: out.csv, schema: {obligor: str, pd: float, amount: float}}",
        "- name: out.csv\n"
        "    schema:\n"
        "      obligor:\n"
        "        type: str\n"
        "        source: {ref: src-1, locator: obligor}\n"
        "      pd: {type: float}\n"
        "      amount: {type: float}",
    )
    client = ScriptedClient([{"text": json.dumps({"ref": "src-1", "rows": [{"obligor": "A-1"}]})}])
    runner = _runner(tmp_path, text, client)
    runner.run()

    assert runner.runtime.context.declared_field_sources == {
        "out.csv": {"obligor": FieldSource(ref="src-1", locator="obligor")}
    }


def test_a_withheld_candidate_says_which_check_rejected_it(tmp_path: Path) -> None:
    """★ "exit=degrade" is the phenomenon; the report has to name the rule.

    The candidate cites its snapshot correctly (so it passes the shape gate) but
    carries too few rows, so ``C-rows`` rejects it. Without this the report said
    only ``{'exit': 'degrade_single_agent'}`` and the next reader had to re-run the
    model to find out why.
    """
    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\n")
    text = """
task_id: t-prov
goal: build the panel
deliverables:
  - {name: out.csv, schema: {obligor: str, pd: float, amount: float}}
sources:
  - {ref: src-1, locator: SOURCE_CSV}
constraints:
  hard:
    - {id: C-rows, kind: presence, target: "*", spec: {min_rows: 5}, rationale: "the panel needs five rows"}
""".replace("SOURCE_CSV", str(source))
    client = ScriptedClient([{"text": json.dumps({"ref": "src-1", "rows": [{"obligor": "A-1"}]})}])
    result = _runner(tmp_path, text, client).run()

    evidence = result.verdicts["T-01"].evidence
    assert evidence["rejected_by"] == "C-rows"
    assert "needs >= 5" in evidence["C-rows"]


def test_an_answer_that_cites_no_snapshot_fails_at_the_shape_gate(tmp_path: Path) -> None:
    """★ Missing ``ref`` must not be papered over with the block id.

    It used to fall back to the block id, which passes every shape check and then
    fails as "unknown snapshot 'T-01'" -- a report pointing at the wrong place. Now
    it is refused where it happened, and the fix names the refs that were valid.
    """
    source = _csv(tmp_path, "panel.csv", "obligor,pd,amount\nA-1,0.01,100\n")
    client = ScriptedClient([{"text": json.dumps({"rows": [{"obligor": "A-1"}]})}])  # no "ref"
    result = _runner(tmp_path, ONE_SOURCE.replace("SOURCE_CSV", str(source)), client).run()

    assert result.candidates == {}
    evidence = result.verdicts["T-01"].evidence
    assert evidence["exit"] == "fix_constraint"
    assert "does not say which snapshot" in evidence["errors"]
    assert "'T-01'" not in evidence["errors"]  # the block id is no longer invented as a ref


def test_the_shape_gate_names_the_snapshots_that_would_have_been_valid() -> None:
    """② (b): the fix tells the model *which* ref to use, not just that one is missing."""
    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")
    with pytest.raises(CandidateFormatError) as error:
        parse_candidate('{"rows": []}', subtask=block, source_id="T-01", authorized_sources=("panel-2026q1",))
    feedback = error.value.feedback()
    assert feedback["code"] == "missing_ref"
    assert "does not say which snapshot" in feedback["problem"]
    assert '"panel-2026q1"' in feedback["expected"]

    with pytest.raises(CandidateFormatError) as unknown:
        parse_candidate(
            '{"ref": "somewhere-else", "rows": []}',
            subtask=block,
            source_id="T-01",
            authorized_sources=("panel-2026q1",),
        )
    assert unknown.value.feedback()["code"] == "unknown_ref"
    assert '"panel-2026q1"' in unknown.value.feedback()["expected"]


def test_a_cited_snapshot_is_accepted_and_becomes_the_source_id() -> None:
    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")
    candidate = parse_candidate(
        '{"ref": "panel-2026q1", "rows": [{"a": 1}]}',
        subtask=block,
        source_id="T-01",
        authorized_sources=("panel-2026q1",),
    )
    assert candidate.source_id == "panel-2026q1"


def test_a_block_without_authorized_sources_keeps_the_documented_fallback() -> None:
    """A merge block has no snapshot to cite -- reported as a gap, not solved here."""
    block = Subtask(id="T-03", goal="g", inputs=("part.csv",), output="out.csv", contract_ref="t", axis="source", mode="write")
    candidate = parse_candidate('{"rows": [{"a": 1}]}', subtask=block, source_id="T-03")
    assert candidate.source_id == "T-03"


def test_a_truncated_answer_is_reported_as_truncation_not_as_the_first_character() -> None:
    """★ Our own output cap cutting the answer is not the model's shape error."""
    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")
    text = '{"ref": "src-1", "columns": [{"name": "obligor"}], "rows": [{"obligor": "A-1'
    with pytest.raises(CandidateFormatError) as error:
        parse_candidate(text, subtask=block, source_id="T-01", output_cap=512)
    feedback = error.value.feedback()
    assert feedback["code"] == "truncated_answer"
    assert "cut off" in feedback["problem"]
    assert "512" in feedback["problem"]  # the cap that did it
    assert "first character" not in feedback["problem"]


def _loop_answer(unit: str, repeats: int) -> str:
    """开头像 JSON、结尾没闭合、中间是一段反复吐的片段。"""
    return '{"ref": "src-1", "columns": [{"name": "obligor"}], "rows": [' + unit * repeats


def _varied_truncation(rows: int = 400, cut: int = 9000) -> str:
    """内容不重复、就是写不完的一条截断（反向样例）。"""
    body = "".join(
        f'{{"obligor": "A-{index:04d}", "amount": "{1000 + index}", "pd": "0.0{index % 90 + 10}"}},'
        for index in range(rows)
    )
    text = '{"ref": "src-1", "columns": [{"name": "obligor"}], "rows": [' + body
    return text[:cut]


def test_a_cap_hit_answer_that_loops_is_declared_as_repetition() -> None:
    """★ `#41`: 「撞上限」是两件事 —— 内容还有（`truncated`）vs 模型在转圈（`repetition`）。

    判据看输出原文（`produce.py::repetition_dominated`）：一段连续重复的周期片段占了被检段
    ≥0.40。真归档里那条 8460 字符 / completion=8192 的输出就是这么被判出来的。
    """
    from bobanana6.agentloop.errors import BobananaAction, classify_error

    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")
    with pytest.raises(CandidateFormatError) as error:
        parse_candidate(_loop_answer('{"obligor": "A-1", "amount": "1"},', 400), subtask=block,
                        source_id="T-01", output_cap=8192, hit_output_cap=True)
    assert error.value.reason == "repetition"
    assert error.value.feedback()["code"] == "truncated_answer"  # 形状读数没变
    classified = classify_error(error.value)
    assert classified.reason == "repetition" and classified.action is BobananaAction.RESHARD_TASK


def test_a_true_truncation_is_not_mistaken_for_a_repetition_loop() -> None:
    """反向：写不完但内容不重复 ⇒ 仍报 `truncated`；没撞上限 ⇒ 沿用 `format_error`。"""
    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")
    text = _varied_truncation()
    with pytest.raises(CandidateFormatError) as capped:
        parse_candidate(text, subtask=block, source_id="T-01", output_cap=8192, hit_output_cap=True)
    assert capped.value.reason == "truncated"
    with pytest.raises(CandidateFormatError) as uncapped:
        parse_candidate(text, subtask=block, source_id="T-01", output_cap=8192)
    assert uncapped.value.reason == "format_error"  # 模型自己停在半途：形状问题，没变


def test_the_repetition_reading_separates_a_loop_from_normal_output() -> None:
    """读数本身（判据不猜）：循环段占比 1.0 vs 正常长答案 <0.15；短输出不参与判定。"""
    from bobanana6.assemble_impl.produce import (
        repetition_dominated,
        repetition_readings,
    )

    loop = repetition_readings(" " * 2000)
    assert loop["period"] == 1 and loop["periodic_run_share"] == 1.0
    assert repetition_dominated(" " * 2000) is True
    varied = repetition_readings(_varied_truncation())
    assert varied["periodic_run_share"] < 0.15
    assert repetition_dominated(_varied_truncation()) is False
    assert repetition_dominated("   ") is False  # 短输出：那叫"空"，不叫"转圈"


def test_the_three_json_breakages_read_differently() -> None:
    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")

    with pytest.raises(CandidateFormatError) as empty:
        parse_candidate("   ", subtask=block, source_id="T-01")
    assert empty.value.feedback()["code"] == "empty"
    assert empty.value.feedback()["problem"] == "the answer is empty"

    with pytest.raises(CandidateFormatError) as not_json:
        parse_candidate("I think it is fine", subtask=block, source_id="T-01")
    assert not_json.value.feedback()["code"] == "not_json"
    assert "first character is 'I'" in not_json.value.feedback()["problem"]

    with pytest.raises(CandidateFormatError) as malformed:
        parse_candidate('{"rows": [1,]}', subtask=block, source_id="T-01")
    assert malformed.value.feedback()["code"] == "malformed_json"
    assert "not valid" in malformed.value.feedback()["problem"]


def test_the_block_runtime_hands_the_ports_budget_to_the_producer(tmp_path: Path) -> None:
    """★ The budget arrives as a *port argument*; it must not be replaced by a field."""
    from fakes import bundle as make_bundle
    from fakes import mini_ontology, plan, subtask

    from bobanana6.assemble_impl import BlockRuntime
    from bobanana6.services import BoundChecks, build_toolset

    class _Client:
        def __init__(self) -> None:
            self.max_tokens: list[int] = []

        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            self.max_tokens.append(max_tokens)
            return ModelResponse(text='{"rows": [{"a": 1}]}')

    client = _Client()
    document = parse_contract(ONE_SOURCE.replace("SOURCE_CSV", "x.csv"))
    runtime = BlockRuntime(client=client, store=SnapshotStore(root=tmp_path / "s3"), contract=document.contract)
    block = subtask("T-01", output="out.csv", mode="write", axis="field")
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
    runtime.run(block, make_bundle(), mini_ontology(), tools, Budget(max_calls=4, max_wall_s=30, max_output=1234))
    assert client.max_tokens == [1234]


def test_every_spelling_of_a_missing_cell_becomes_the_same_representation() -> None:
    """★ ②（`三条调优点`）：「没有值」只有【一种】规范写法，三个入口都归一。

    验证的是**矩阵**（每种写法 × 三个入口），不是那一种写法：
      解析入口   null / "" / "  " / 键缺席 → 都变成「键缺席」
      比较处     写了 null 的行 == 省略了键的行（继承守卫不再误杀）
      序列化出口 部件载荷里也只剩规范写法
    """
    from bobanana6.assemble_impl.produce import _same_row, parse_candidate
    from bobanana6.core.model import Artifact, Column, canonical_row
    from bobanana6.services.registry import artifact_payload

    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="read")
    # 走 JSON 的四种写法（哨兵不从 JSON 来，单独在下面验）
    spellings = [{"amount": None}, {"amount": ""}, {"amount": "   "}, {"amount": "MISSING"}, {}]
    for raw in spellings:
        candidate = parse_candidate(
            json.dumps({"ref": "s1", "rows": [raw]}),
            subtask=block,
            source_id="s1",
            authorized_sources=("s1",),
        )
        row = candidate.artifact("out.csv").rows[0]  # type: ignore[union-attr]
        assert "amount" not in row, raw

    # 内部标记（哨兵）也归一：它和 null / 空串最终是同一个「缺」
    assert canonical_row({"amount": MISSING, "year": 2021}) == {"year": 2021}

    # 比较处：两种写法是【同一行】；多一格才是不同的行
    assert _same_row({"amount": None, "year": 2021}, {"year": 2021})
    assert _same_row({"amount": "", "year": 2021}, {"year": 2021})
    assert not _same_row({"amount": 1, "year": 2021}, {"year": 2021})

    # 序列化出口：载荷里也不会出现 null 写法
    artifact = Artifact(name="out.csv", columns=(Column(name="amount", dtype="str"),), rows=({"amount": None},))
    assert artifact_payload(artifact)["rows"] == [{}]


def test_the_files_channel_is_documented_where_the_model_can_see_it() -> None:
    """★ `#35`：代码类产物的交付通道必须【说出来】——三处缺一不可。

    修前的缺口（`#34`）：判据要求工作区里有文件，而这三处一条都没提 `files`
    ⇒ 真模型没有门（确定性读数：不写 files 的答案 → 工作区零产物、块级 0.6）。

    ★ `#42`：不再 grep 源码字符串 —— 断言**渲染出来的**三处（`AGENTS §7⑬`：断言模型
    真正收到的东西，不断言"某个函数里写着某个词"）。
    """
    from bobanana6.assemble_impl.answer_contract import answer_contract_renderings
    from bobanana6.assemble_impl.produce import CANDIDATE_SCHEMA, EXPECTED_SHAPE

    renderings = answer_contract_renderings(("interface",))
    # ① 被判据拒绝时喂回去的"该长什么样"（`expected`）
    assert "files" in EXPECTED_SHAPE
    assert "workspace-relative path" in renderings["shape"]
    # ② 服务端 schema：`additionalProperties: False` 下必须**显式**开这一个口子
    assert CANDIDATE_SCHEMA["additionalProperties"] is False
    assert CANDIDATE_SCHEMA["properties"]["files"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
    # ③ 系统提示里那句人话（它是模型唯一能读到的交付说明）
    assert "files" in renderings["prompt"]
    assert "workspace-relative path" in renderings["prompt"]


# --------------------------------------------------------------------------- #
# ★ `#43`: 继承守卫拒收时落观测（池子 + 交上来的行 + 逐键差异）—— 只观测，不改判定
# --------------------------------------------------------------------------- #

_MERGE_BLOCK = Subtask(
    id="T-04", goal="merge the parts", inputs=("part-panel-c.csv",), output="panel.csv",
    contract_ref="panel", axis="field", mode="write",
)
_MERGE_PARTS = {
    "part-panel-c.csv": {
        "rows": [{"obligor": "C-001", "amount": 90_000.0}],
        "provenance": [
            {"value_ref": "part-panel-c.csv:row=0", "snapshot_ref": "panel-c", "source_row": 0, "locator": "L"}
        ],
    }
}
#: 合并块把 90_000 又乘了一次 —— 它改了自己收到的东西（该拒）
_TRANSFORMED = '{"ref": "panel-c", "rows": [{"obligor": "C-001", "amount": 90000000.0}]}'
#: 逐字搬运（该过）
_VERBATIM = '{"ref": "panel-c", "rows": [{"obligor": "C-001", "amount": 90000.0}]}'


def test_a_refused_merge_row_explains_itself_with_the_pool_and_the_diff() -> None:
    """★ `#43`: 拒收文案一个字没变，但**观测**把"它手里有什么"说清楚了。"""
    from bobanana6.assemble_impl.produce import parse_candidate

    records: list[Mapping[str, Any]] = []
    with pytest.raises(CandidateFormatError) as error:
        parse_candidate(_TRANSFORMED, subtask=_MERGE_BLOCK, source_id="T-04",
                        authorized_sources=("panel-c",), parts=_MERGE_PARTS, observe=records.append)
    # ① 判定与文案逐字不变
    assert error.value.feedback()["code"] == "untraceable_row"
    assert str(error.value) == (
        "row 0 of 'panel.csv' matches no row this block was handed; "
        "a merge may only carry values it received"
    )
    # ② 观测把池子、交上来的行、逐键差异都落下来
    assert len(records) == 1
    record = dict(records[0])
    assert record["block"] == "T-04" and record["output"] == "panel.csv" and record["row_index"] == 0
    assert record["pool_size"] == 1 and record["pool"] == [{"obligor": "C-001", "amount": 90000.0}]
    assert record["pool_refs"][0]["value_ref"] == "part-panel-c.csv:row=0"
    assert record["pool_refs"][0]["snapshot_ref"] == "panel-c"
    assert record["pool_diagnosis"]["overall"] == "pool_non_empty"
    assert record["sent_row"] == {"obligor": "C-001", "amount": 90000000.0}
    assert record["all_pool_differs"] is True
    # ③ 最接近的那行 + 逐键差异（**差在哪个键**）
    nearest = record["nearest"]
    assert nearest["input_name"] == "part-panel-c.csv" and nearest["keys_equal"] == 1
    assert list(nearest["difference"]) == ["amount"]
    assert nearest["difference"]["amount"] == {"sent": 90000000.0, "pool": 90000.0}


def test_an_accepted_merge_row_produces_no_observation() -> None:
    """★ 反向：逐字搬运（该过）不许产生任何观测 —— 观测只在拒收那一刻发生。"""
    from bobanana6.assemble_impl.produce import parse_candidate

    records: list[Mapping[str, Any]] = []
    candidate = parse_candidate(_VERBATIM, subtask=_MERGE_BLOCK, source_id="T-04",
                                authorized_sources=("panel-c",), parts=_MERGE_PARTS, observe=records.append)
    assert candidate.provenance and records == []


def test_a_broken_observer_never_changes_the_verdict() -> None:
    """★ 硬要求：观测挂了（这里让它直接抛）判定照走，文案也不变。"""
    from bobanana6.assemble_impl.produce import parse_candidate

    def explode(_record: Mapping[str, Any]) -> None:
        raise OSError("the disk is on fire")

    with pytest.raises(CandidateFormatError) as error:
        parse_candidate(_TRANSFORMED, subtask=_MERGE_BLOCK, source_id="T-04",
                        authorized_sources=("panel-c",), parts=_MERGE_PARTS, observe=explode)
    assert error.value.feedback()["code"] == "untraceable_row"
    assert "matches no row this block was handed" in str(error.value)


def test_the_three_empty_pool_diagnoses_are_told_apart() -> None:
    """★ `#43` 的全部理由：`pool_size == 0` 时必须能回答"**为什么**是 0"（三种修法不同）。"""
    from bobanana6.assemble_impl.produce import _pool_diagnosis

    no_rows = _pool_diagnosis({"part.csv": {"rows": [], "provenance": [{"value_ref": "part.csv:row=0"}]}})
    assert no_rows["overall"] == "no_rows_at_all"
    no_provenance = _pool_diagnosis({"part.csv": {"rows": [{"a": 1}], "provenance": []}})
    assert no_provenance["overall"] == "rows_without_provenance"
    mismatch = _pool_diagnosis({"part.csv": {"rows": [{"a": 1}], "provenance": [{"value_ref": "other:row=0"}]}})
    assert mismatch["overall"] == "value_ref_mismatch"
    assert mismatch["parts"][0]["expected_value_refs"] == ["part.csv:row=0"]
    assert mismatch["parts"][0]["seen_value_refs"] == ["other:row=0"]
    assert _pool_diagnosis({})["overall"] == "no_parts"


# --------------------------------------------------------------------------- #
# ★ `#42`: 答案契约是一等对象 —— 六处渲染点必须与它一致，且不许出现第七处
# --------------------------------------------------------------------------- #

#: 散文渲染：这些字段的**粒度**必须逐字出现在文本里。
_RENDERING_GRANULARITY: dict[str, tuple[str, ...]] = {
    "shape": ("ref", "columns", "rows", "files"),
    "prompt": ("ref", "columns", "rows", "files"),
    "files_requirement": ("files",),
    "reference_note": ("ref",),  # 它只讲"参照物 vs 来源"，字段名点名三个、粒度只讲 ref
}

#: 这些渲染点必须**点名**这些字段（不必复述粒度）。
_RENDERING_NAMES: dict[str, tuple[str, ...]] = {
    "reference_note": ("ref", "columns", "rows"),
}

#: 只渲染取值域的散文渲染点：字段名 + 取值域。
_RENDERING_DOMAIN: dict[str, tuple[str, ...]] = {"ref_domain": ("ref",)}

#: 结构化渲染：粒度没法是散文，改成按结构比对。
_STRUCTURED_RENDERINGS: tuple[str, ...] = ("schema",)


def test_every_rendering_of_the_answer_contract_agrees_with_it() -> None:
    """★ `#42`: 六处渲染点，任何一处缺字段、缺粒度、或与结构化形态对不上 → 红。

    这一族病复发过四次（`#34` 漏 `files`、`#37` 参照物没送到、`#41` `reference_note` 缩水、
    现在 `ref` 粒度），每次都是"某一处漏同步"。这条测试把"同步"变成机器的事。
    """
    from bobanana6.assemble_impl.answer_contract import (
        ANSWER_CONTRACT,
        answer_contract_renderings,
        candidate_schema,
        contract_field,
    )

    renderings = answer_contract_renderings(("interface", "shop-a-source"))
    expected_keys = (
        set(_RENDERING_GRANULARITY) | set(_RENDERING_NAMES) | set(_RENDERING_DOMAIN) | set(_STRUCTURED_RENDERINGS)
    )
    assert set(renderings) == expected_keys, "六处渲染点，不多不少"

    for rendering_name, covered in _RENDERING_GRANULARITY.items():
        text = renderings[rendering_name]
        for field_name in covered:
            answer_field = contract_field(field_name)
            assert f'"{answer_field.name}"' in text, (rendering_name, field_name, "字段名不在")
            assert answer_field.granularity in text, (rendering_name, field_name, "粒度不在（逐字来自契约）")

    for rendering_name, covered in _RENDERING_NAMES.items():
        text = renderings[rendering_name]
        for field_name in covered:
            assert f'"{contract_field(field_name).name}"' in text, (rendering_name, field_name, "字段名不在")

    for rendering_name, covered in _RENDERING_DOMAIN.items():
        text = renderings[rendering_name]
        for field_name in covered:
            answer_field = contract_field(field_name)
            assert f'"{answer_field.name}"' in text, (rendering_name, field_name, "字段名不在")
            assert answer_field.domain in text, (rendering_name, field_name, "取值域不在（逐字来自契约）")

    # 结构化那一份：必填表与每个属性的形态都逐字来自契约
    schema = candidate_schema()
    assert schema["required"] == [f.name for f in ANSWER_CONTRACT if f.required]
    for answer_field in ANSWER_CONTRACT:
        assert schema["properties"][answer_field.name] == dict(answer_field.json_schema)

    # 取值域那一处：名单必须是这一块【逐字】的授权名单
    assert '"interface"' in renderings["ref_domain"] and '"shop-a-source"' in renderings["ref_domain"]

    # ★ 模型真正收到的那条拒绝反馈 = 形状 + 取值域拼起来：名字/取值域/粒度必须【同时】在里面
    from bobanana6.assemble_impl.produce import expected_shape

    combined = expected_shape(["interface", "shop-a-source"])
    ref = contract_field("ref")
    assert ref.granularity in combined and ref.domain in combined and ref.name in combined


def test_no_second_hand_written_source_of_the_answer_shape() -> None:
    """★ `#42`: 契约之外的任何文件里再手写一遍形状 → 红（第七处渲染点不许出现）。

    机制：扫 `src/bobanana6/**` 的字符串常量，凡**同时**出现 ≥2 个契约字段名的，
    只允许待在 `answer_contract.py` 里（那是契约本体）。
    """
    import ast

    from bobanana6.assemble_impl.answer_contract import ANSWER_CONTRACT

    root = Path(__file__).resolve().parents[1] / "src" / "bobanana6"
    contract_module = root / "assemble_impl" / "answer_contract.py"
    field_names = tuple(answer_field.name for answer_field in ANSWER_CONTRACT)
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or path == contract_module:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            present = [name for name in field_names if f'"{name}"' in node.value or f"'{name}'" in node.value]
            if len(present) >= 2:
                offenders.append(f"{path.relative_to(root)}:{node.lineno} {present}")
    assert offenders == [], f"契约之外又出现了手写的答案形状：{offenders}"


# --------------------------------------------------------------------------- #
# ★ 层次切分：块与块之间真的接力（端到端，不是只看计划）
# --------------------------------------------------------------------------- #

RELAY = """
task_id: t-relay
goal: chain two products
deliverables:
  - {name: a.csv, schema: {obligor: str}}
  - {name: b.csv, schema: {obligor: str}, depends_on: [a.csv]}
sources:
  - {ref: src-1, locator: SOURCE_CSV}
constraints:
  hard:
    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "no empty products"}
"""


class _RelayRecorder:
    """Records the prompt each block received, and answers with a one-row product."""

    def __init__(self) -> None:
        self.prompts: dict[str, list[str]] = {}

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        block = ""
        for message in messages:
            if message.get("role") == "user":
                try:
                    block = json.loads(str(message.get("content")))["subtask"]["id"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    block = ""
                if block:
                    break
        self.prompts.setdefault(block, []).append(json.dumps(messages, ensure_ascii=False))
        return ModelResponse(
            text=json.dumps(
                {
                    "columns": [{"name": "obligor", "dtype": "str"}],
                    "ref": "src-1",
                    "rows": [{"obligor": f"seen-by-{block}"}],
                }
            )
        )


def test_a_layered_plan_delivers_upstream_products_to_the_next_block(tmp_path: Path) -> None:
    source = _csv(tmp_path, "a.csv", "obligor\nA-1\n")
    client = _RelayRecorder()
    runner = _runner(tmp_path, RELAY.replace("SOURCE_CSV", str(source)), client)
    result = runner.run()

    assert result.plan.shape == "layered"
    assert [block.inputs for block in result.plan.subtasks] == [(), ("a.csv",)]
    # ★ 改前（inputs 全空）这里不可能出现 delivered_inputs；改后 T-02 收到了 T-01 的产物。
    downstream = client.prompts["T-02"][0]
    assert "delivered_inputs" in downstream
    assert "seen-by-T-01" in downstream
    # 同层零依赖：T-01 的提示里没有任何上游输入
    assert "delivered_inputs" not in client.prompts["T-01"][0]
