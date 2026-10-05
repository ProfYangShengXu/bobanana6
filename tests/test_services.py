"""services/: tool surface, call metadata filled by the registry, evidence recording."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fakes import (
    artifact,
    bundle,
    candidate,
    constraint,
    contract,
    mini_ontology,
    plan,
    subtask,
)

from bobanana6.agentloop.tools import plan_tool_segments
from bobanana6.core.checkers.base import CheckContext
from bobanana6.core.model import Budget, ModelResponse, SourceRef, Subtask
from bobanana6.services import (
    SHELL_COMMAND_CHECK,
    TOOL_NAMES,
    BlockJudge,
    BoundChecks,
    BoundSemantics,
    BoundSources,
    BoundTask,
    RecordedEvidence,
    RegisteredTool,
    ToolRegistry,
    build_registry,
    build_toolset,
    shell_command_check,
    snapshot_payload,
)
from bobanana6.sources import SnapshotStore, SourceForm

PASSES = f'{sys.executable} -c "print(1)"'


# --------------------------------------------------------------------------- #
# semantics.lookup / map / check
# --------------------------------------------------------------------------- #


def test_lookup_reports_the_concept_the_unit_and_the_definition() -> None:
    semantics = BoundSemantics(ontology=mini_ontology())
    found = semantics.lookup("credit.pd")
    assert found["found"] is True
    assert found["kind"] == "measure"
    assert found["definition_version"] == "v1"
    assert found["aliases"] == ["pd_3y", "probability_of_default"]
    assert semantics.lookup("nope")["found"] is False


def test_map_returns_the_concept_or_the_word_unmapped() -> None:
    semantics = BoundSemantics(ontology=mini_ontology())
    assert semantics.map("credit.pd") == "credit.pd"
    assert semantics.map("mystery") == "unmapped"


def test_check_accepts_a_declared_usage_and_refuses_a_wrong_unit() -> None:
    semantics = BoundSemantics(ontology=mini_ontology())
    assert semantics.check({"concept": "credit.amount", "unit": "CNY"})[0] is True
    ok, reason = semantics.check({"concept": "credit.amount", "unit": "USD"})
    assert ok is False and "declared in 'CNY'" in reason
    assert semantics.check({"concept": "ghost"})[0] is False
    assert semantics.check("credit.pd")[0] is False


def test_check_refuses_a_code_outside_its_code_system() -> None:
    semantics = BoundSemantics(ontology=mini_ontology())
    assert semantics.check({"concept": "credit.rating", "code_system": "rating_scale", "code": "AAA"})[0] is True
    assert semantics.check({"concept": "credit.rating", "code_system": "rating_scale", "code": "ZZZ"})[0] is False


# --------------------------------------------------------------------------- #
# task.*
# --------------------------------------------------------------------------- #


def _subtask(**kw: object) -> Subtask:
    return subtask("T-01", inputs=("part-1.csv",), output="out.csv", mode="write", **kw)  # type: ignore[arg-type]


def test_mysubtask_shows_only_this_block() -> None:
    view = BoundTask(subtask=_subtask(), contract_document=contract()).mysubtask()
    assert view["id"] == "T-01"
    assert view["inputs"] == ["part-1.csv"]
    assert view["mode"] == "write"


def test_upstream_refuses_an_input_the_block_never_declared() -> None:
    task = BoundTask(
        subtask=_subtask(),
        contract_document=contract(),
        upstream_parts={"part-1.csv": {"rows": 1}},
        allowed_inputs=("part-1.csv",),
    )
    assert task.upstream("part-1.csv") == {"rows": 1}
    with pytest.raises(PermissionError, match="not declared"):
        task.upstream("part-2.csv")


# --------------------------------------------------------------------------- #
# sources.snapshot binding
# --------------------------------------------------------------------------- #


def _snapshot(tmp_path: Path, *, suffix: str = ".csv", body: bytes = b"a\n1\n") -> tuple[SnapshotStore, object]:
    store = SnapshotStore(root=tmp_path / "s")
    snap = store.put(
        ref="src-1", body=body, source_kind="file", captured_at="2026-01-01T00:00:00", suffix=suffix
    )
    return store, snap


def test_snapshot_without_a_ref_binds_to_the_only_authorized_source(tmp_path: Path) -> None:
    _store, snap = _snapshot(tmp_path)
    tools = BoundSources(authorized=(SourceRef(ref="src-1", locator="x"),), fetched={"src-1": snap})  # type: ignore[arg-type]
    assert tools.snapshot("").ref == "src-1"
    assert tools.snapshot("src-1").ref == "src-1"


def test_snapshot_refuses_an_unauthorized_ref(tmp_path: Path) -> None:
    _store, snap = _snapshot(tmp_path)
    tools = BoundSources(authorized=(SourceRef(ref="src-1"),), fetched={"src-1": snap})  # type: ignore[arg-type]
    with pytest.raises(PermissionError, match="not authorized"):
        tools.snapshot("src-9")


def test_snapshot_refuses_to_guess_between_two_sources(tmp_path: Path) -> None:
    _store, snap = _snapshot(tmp_path)
    tools = BoundSources(
        authorized=(SourceRef(ref="src-1"), SourceRef(ref="src-2")),
        fetched={"src-1": snap, "src-2": snap},
    )  # type: ignore[arg-type]
    with pytest.raises(PermissionError, match="authorized sources"):
        tools.snapshot("")


def test_snapshot_reports_an_authorized_but_unfetched_source(tmp_path: Path) -> None:
    tools = BoundSources(authorized=(SourceRef(ref="src-1"),), fetched={})
    with pytest.raises(LookupError, match="not fetched"):
        tools.snapshot("src-1")


def test_snapshot_payload_parses_csv_and_json(tmp_path: Path) -> None:
    _store, snap = _snapshot(tmp_path)
    payload = json.loads(snapshot_payload("src-1", snap, b"a,b\n1,2\n3,4\n"))  # type: ignore[arg-type]
    assert [c["name"] for c in payload["columns"]] == ["a", "b"]
    assert payload["rows"] == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
    assert payload["ref"] == "src-1"

    _store2, json_snap = _snapshot(tmp_path, suffix=".json", body=b'[{"x": 1}]')
    json_payload = json.loads(snapshot_payload("src-1", json_snap, b'[{"x": 1}]'))  # type: ignore[arg-type]
    assert json_payload["rows"] == [{"x": 1}]


# --------------------------------------------------------------------------- #
# build_toolset binding rules
# --------------------------------------------------------------------------- #


def test_build_toolset_grants_snapshot_only_on_the_source_axis(tmp_path: Path) -> None:
    _store, snap = _snapshot(tmp_path)
    checks = BoundChecks(block="T-01")
    on_source = build_toolset(
        plan=plan(),
        subtask=subtask("T-01", axis="source"),
        contract=contract(),
        ontology=mini_ontology(),
        authorized=(SourceRef(ref="src-1"),),
        fetched={"src-1": snap},  # type: ignore[dict-item]
        upstream={},
        checks=checks,
    )
    off_source = build_toolset(
        plan=plan(),
        subtask=subtask("T-02", axis="field"),
        contract=contract(),
        ontology=mini_ontology(),
        authorized=(SourceRef(ref="src-1"),),
        fetched={"src-1": snap},  # type: ignore[dict-item]
        upstream={},
        checks=checks,
    )
    assert on_source.sources is not None
    assert "sources.snapshot" in {spec.name for spec in on_source.specs()}
    assert off_source.sources is None
    assert "sources.snapshot" not in {spec.name for spec in off_source.specs()}


def test_build_toolset_binds_inputs_to_the_subtask() -> None:
    toolset = build_toolset(
        plan=plan(),
        subtask=_subtask(),
        contract=contract(),
        ontology=mini_ontology(),
        authorized=(),
        fetched={},
        upstream={},
        checks=BoundChecks(block="T-01"),
    )
    assert toolset.allowed_inputs == ("part-1.csv",)
    assert toolset.allows_upstream("part-1.csv")
    assert not toolset.allows_upstream("part-2.csv")


def test_assemble_dryrun_reports_only_this_blocks_violations() -> None:
    from bobanana6.core.split import SplitReport, SplitViolation

    report = SplitReport(
        violations=(
            SplitViolation(check="iface_match", detail="mismatch", subtask_ids=("T-01",)),
            SplitViolation(check="merge_cheap", detail="expensive", subtask_ids=("T-09",)),
        )
    )
    checks = BoundChecks(block="T-01", split_report=report)
    result = checks.assemble_dryrun()
    assert result["ok"] is False
    assert result["codes"] == ["iface_match"]
    assert BoundChecks(block="T-01").assemble_dryrun()["ok"] is None


# --------------------------------------------------------------------------- #
# registry: effect/targets are filled here, not by the caller
# --------------------------------------------------------------------------- #


def _registry() -> ToolRegistry:
    return ToolRegistry(
        tools=(
            RegisteredTool(
                name="store.write",
                parameters=("path",),
                handler=lambda args: f"wrote {args['path']}",
                effect="write",
                target_args=("path",),
            ),
            RegisteredTool(
                name="store.read",
                parameters=("path",),
                handler=lambda args: json.dumps({"path": args["path"]}),
                effect="read",
                target_args=("path",),
            ),
        )
    )


def _raw(call_id: str, name: str, arguments: str) -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def test_registry_fills_effect_and_targets_from_the_tool_declaration() -> None:
    calls = _registry().calls([_raw("c1", "store.write", '{"path": "out.csv"}')])
    assert calls[0].effect == "write"
    assert calls[0].targets == ("out.csv",)


def test_write_calls_become_a_barrier_because_the_registry_says_so() -> None:
    """★ The model cannot downgrade a write to a read by claiming it is one."""
    calls = _registry().calls(
        [
            _raw("c1", "store.read", '{"path": "a"}'),
            _raw("c2", "store.read", '{"path": "b"}'),
            _raw("c3", "store.write", '{"path": "c"}'),
            _raw("c4", "store.read", '{"path": "d"}'),
        ]
    )
    segments = plan_tool_segments(calls)
    assert [kind for kind, _ in segments] == ["parallel", "sequential", "sequential"]
    assert [call.call_id for call in segments[2][1]] == ["c4"]


def test_malformed_call_still_gets_a_result() -> None:
    registry = _registry()
    calls = registry.calls([{"id": "c1", "function": {"name": "store.read", "arguments": "{not json"}}])
    assert calls[0].arguments == {}
    results = registry.execute(calls)
    assert len(results) == 1 and results[0].ok is False  # ★ never a dangling tool call


def test_a_call_carrying_only_parsed_arguments_is_not_mistaken_for_bad_json() -> None:
    """★ ③：只给 `arguments`（一个合法的 JSON 对象）也必须被正确解析。

    旧回退 `raw_arguments or arguments` 把 Mapping 喂给只做 `json.loads` 的解析器，
    于是一个完全合法的调用被报成「参数不是合法 JSON」—— 错误信息把人指向错的方向。
    """
    from bobanana6.agentloop.tools import ToolCall

    registry = _registry()
    call = ToolCall(call_id="c1", name="store.read", arguments={"path": "a"})
    results = registry.execute((call,))
    assert results[0].ok is True, results[0]
    assert "a" in results[0].content


def test_the_part_payload_carries_the_columns_unit() -> None:
    """★ 真模型跑测抓到的另一半：生产者声明的单位必须**跟着部件走**，
    否则消费者看不到它，「确立/没确立」在交接处就丢了。"""
    from bobanana6.core.model import Artifact, Column
    from bobanana6.services import artifact_payload

    payload = artifact_payload(
        Artifact(name="part.csv", columns=(Column(name="amount", dtype="str", unit="CNY"),), rows=({"amount": 1},))
    )
    assert payload["columns"][0]["unit"] == "CNY"


def test_unknown_tool_gets_a_named_failure() -> None:
    registry = _registry()
    results = registry.execute(registry.calls([_raw("c1", "nope", "{}")]))
    assert results[0].ok is False and results[0].error_type == "unknown_tool"


def test_a_raising_handler_is_a_visible_failure() -> None:
    def explode(args: object) -> str:
        raise RuntimeError("tool is broken")

    registry = ToolRegistry(tools=(RegisteredTool(name="bad", parameters=(), handler=explode),))
    results = registry.execute(registry.calls([_raw("c1", "bad", "{}")]))
    assert results[0].ok is False
    assert results[0].error_type == "RuntimeError"
    assert "tool is broken" in results[0].content


def test_duplicate_tool_names_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate tool names"):
        ToolRegistry(
            tools=(
                RegisteredTool(name="x", parameters=(), handler=lambda args: ""),
                RegisteredTool(name="x", parameters=(), handler=lambda args: ""),
            )
        )


# --------------------------------------------------------------------------- #
# evidence: the gate's independent input
# --------------------------------------------------------------------------- #


def test_shell_command_check_records_evidence_and_passes() -> None:
    evidence = RecordedEvidence()
    check = shell_command_check(evidence, lambda: 100.0)
    outcome = check(constraint(kind="custom", spec={"check": SHELL_COMMAND_CHECK, "command": PASSES}), candidate(), CheckContext())
    assert outcome.ok is True and outcome.cause == ""
    assert evidence.last() is not None
    assert evidence.last().exit_code == 0  # type: ignore[union-attr]
    assert evidence.last().created_at == 100.0  # type: ignore[union-attr]
    assert "exited 0" in outcome.facts[0]


def test_shell_command_check_fails_on_a_non_zero_exit() -> None:
    evidence = RecordedEvidence()
    check = shell_command_check(evidence, lambda: 1.0)
    command = f'{sys.executable} -c "import sys; sys.exit(3)"'
    outcome = check(constraint(kind="custom", spec={"check": SHELL_COMMAND_CHECK, "command": command}), candidate(), CheckContext())
    assert outcome.ok is False
    assert "exited 3" in outcome.facts[0]
    # ★ `#44`: 断言失败不是环境问题 —— 不许声明 cause（那会把它记成 tool）
    assert outcome.cause == ""
    assert "could not" not in " ".join(outcome.facts)


def test_shell_command_check_refuses_an_unattributable_exit() -> None:
    """★ A backgrounded command donates nothing: its exit status is not evidence."""
    evidence = RecordedEvidence()
    check = shell_command_check(evidence, lambda: 1.0)
    command = f'{sys.executable} -c "print(1)" &'
    outcome = check(constraint(kind="custom", spec={"check": SHELL_COMMAND_CHECK, "command": command}), candidate(), CheckContext())
    assert outcome.ok is False
    assert any("not attributable" in fact for fact in outcome.facts)


def test_shell_command_check_needs_a_command() -> None:
    check = shell_command_check(RecordedEvidence(), lambda: 1.0)
    outcome = check(constraint(kind="custom", spec={}), candidate(), CheckContext())
    assert outcome.ok is False and "spec.command" in outcome.facts[0]


# --------------------------------------------------------------------------- #
# ★ `#44`: 判据命令的**执行环境** —— 派活前预检，以及"跑不起来"与"产出不合格"的分开
# --------------------------------------------------------------------------- #


def _shell_constraint(cid: str, command: str) -> object:
    return constraint(cid, "custom", "*", {"check": SHELL_COMMAND_CHECK, "command": command})


def test_judge_environment_preflight_names_a_missing_interpreter(tmp_path: Path) -> None:
    """解释器不存在 ⇒ 具名问题（`interpreter_missing`），不是"产出不合格"。"""
    from bobanana6.services import judge_environment_problems

    problems = judge_environment_problems(
        (_shell_constraint("C-1", "cd . && python3-does-not-exist-42 -c \"print(1)\""),), cwd=tmp_path
    )
    assert len(problems) == 1, problems
    code, detail = problems[0]
    assert code == "C-1:interpreter_missing" and "python3-does-not-exist-42" in detail


def test_judge_environment_preflight_names_a_module_that_will_not_import(tmp_path: Path) -> None:
    """★ 这正是 `壳问题三连` §2 那条：`python3 -m pytest` 在没装 pytest 的解释器下必然拒。"""
    from bobanana6.services import judge_environment_problems

    problems = judge_environment_problems(
        (_shell_constraint("C-1", f'{sys.executable} -m definitely_not_installed_module_42 -q'),), cwd=tmp_path
    )
    assert len(problems) == 1, problems
    code, detail = problems[0]
    assert code == "C-1:module_missing" and "definitely_not_installed_module_42" in detail


def test_judge_environment_preflight_is_silent_on_a_command_that_can_run(tmp_path: Path) -> None:
    """★ 反向：能跑的命令一个字节都不许多说（否则正常题会被拒）。"""
    from bobanana6.services import judge_environment_problems

    good = _shell_constraint("C-1", f'{sys.executable} -c "print(1)"')
    assert judge_environment_problems((good,), cwd=tmp_path) == ()


def test_a_runtime_created_workspace_directory_is_not_a_reason_to_refuse(tmp_path: Path) -> None:
    """★ `#46`: `_run/workspace` 是**壳在运行期自己创建**的目录（第一次写产物才落盘）。

    门在预检时要求它已经存在，就会拦死所有"从没跑过"的新题，并且只给"历史上跑过、目录残留"
    的题放行 —— 这是拿运行期状态当判据。相对目标不判存在性。
    """
    from bobanana6.services import judge_environment_problems

    fresh = _shell_constraint(
        "C-2", f'cd _run/workspace/input && {sys.executable} -m pytest tests/test_x.py -q'
    )
    assert judge_environment_problems((fresh,), cwd=tmp_path) == ()


def test_an_absolute_directory_that_does_not_exist_is_still_refused(tmp_path: Path) -> None:
    """★ 另一半：作者**事先写下**的绝对路径不存在，就该拒（没人替他创建）。"""
    from bobanana6.services import judge_environment_problems

    missing = tmp_path / "no-such-absolute-dir"
    problems = judge_environment_problems(
        (_shell_constraint("C-2", f'cd {missing} && {sys.executable} -c "print(1)"'),), cwd=tmp_path
    )
    assert [code for code, _detail in problems] == ["C-2:cwd_missing"], problems


def test_a_missing_relative_interpreter_under_a_visible_cwd_is_still_refused(tmp_path: Path) -> None:
    """★ `#46` 不许把解释器检查一起放松：相对解释器在**看得见的** cwd 下缺了，照样拒。"""
    from bobanana6.services import judge_environment_problems

    problems = judge_environment_problems(
        (_shell_constraint("C-2", 'cd . && ./definitely-missing-binary-42 -q'),), cwd=tmp_path
    )
    assert [code for code, _detail in problems] == ["C-2:interpreter_missing"], problems
    # 而藏在运行期目录里的相对解释器，此刻无从判断 ⇒ 不拒
    hidden = _shell_constraint("C-3", 'cd _run/workspace && ./run.sh -q')
    assert judge_environment_problems((hidden,), cwd=tmp_path) == ()


def test_a_module_probe_still_runs_when_the_cwd_is_created_at_run_time(tmp_path: Path) -> None:
    """★ `#46`: 丢进运行期目录不等于把"点名的模块导不进来"这条预检也丢掉
    （第三方包能不能 import 不取决于工作区）。"""
    from bobanana6.services import judge_environment_problems

    problems = judge_environment_problems(
        (_shell_constraint("C-2", f'cd _run/workspace && {sys.executable} -m definitely_not_installed_module_42'),),
        cwd=tmp_path,
    )
    assert [code for code, _detail in problems] == ["C-2:module_missing"], problems


def test_a_failed_assertion_is_not_mistaken_for_a_broken_environment() -> None:
    """★ `#44` 的分水岭：断言失败里出现 "No such file or directory" **不算**环境问题
    （那可能正是产出没写出文件）。只有结构化证据（126/127 / 点名的模块导不进来）才算。"""
    from bobanana6.services import command_unrunnable

    assertion = (
        "tests/test_a.py::test_x FAILED\n"
        "FileNotFoundError: [Errno 2] No such file or directory: 'inventory.py'\n"
        "1 failed, 2 passed"
    )
    assert command_unrunnable(f'{sys.executable} -m minitest x', assertion, 1) == ""
    # 点名 pytest 却没装 ⇒ 环境问题
    missing = "/usr/bin/python3: No module named pytest"
    assert command_unrunnable("python3 -m pytest tests/x.py", missing, 1) != ""
    # 点名的模块是别的（测试里 import 不到某个产物模块）⇒ 不是环境问题
    other = "ModuleNotFoundError: No module named 'inventory'"
    assert command_unrunnable("python3 -m pytest tests/x.py", other, 1) == ""
    # POSIX 的 127
    assert command_unrunnable("nope", "nope: command not found", 127) != ""


def test_the_judge_declares_the_tool_layer_when_its_command_cannot_run() -> None:
    """★ 走到判据那一步才发现"命令自己跑不起来"时，outcome 要声明 `cause="tool"`。"""
    evidence = RecordedEvidence()
    check = shell_command_check(evidence, lambda: 1.0)
    # `sh -c` 里点了一个不存在的命令 ⇒ shell 用 POSIX 的 127 说"找不到"（预检看不到引号里面）
    unrunnable = _shell_constraint("C-1", 'sh -c "definitely-missing-binary-42 -q"')
    outcome = check(unrunnable, candidate(), CheckContext())  # type: ignore[arg-type]
    assert outcome.ok is False and outcome.cause == "tool", outcome
    assert any("could not run the command" in fact for fact in outcome.facts), outcome.facts


def _judge(bundle_) -> BlockJudge:
    block = subtask("T-01", output="out.csv", mode="write")
    return BlockJudge(bundle=bundle_, subtask=block, context=CheckContext(ontology=mini_ontology()))


def test_verify_falls_back_to_the_kernel_when_no_command_ran() -> None:
    passing = bundle(hard=(constraint("C-1", "presence", "T-01", {"min_rows": 1}),))
    verdict = _judge(passing).verify(candidate(artifacts=(artifact(rows=({"a": 1},)),)))
    assert verdict.status == "passed" and verdict.scope == "full"


def test_verify_reports_failure_when_a_constraint_fails() -> None:
    failing = bundle(hard=(constraint("C-1", "presence", "T-01", {"min_rows": 5}),))
    verdict = _judge(failing).verify(candidate(artifacts=(artifact(rows=({"a": 1},)),)))
    assert verdict.status == "failed"


def test_verify_uses_the_recorded_command_evidence() -> None:
    evidence = RecordedEvidence()
    check = shell_command_check(evidence, lambda: 10.0)
    failing = bundle(
        hard=(
            constraint("C-1", "presence", "T-01", {"min_rows": 1}),
            constraint("C-2", "custom", "T-01", {"check": SHELL_COMMAND_CHECK, "command": PASSES}),
        )
    )
    judge = BlockJudge(
        bundle=failing,
        subtask=subtask("T-01", output="out.csv", mode="write"),
        context=CheckContext(ontology=mini_ontology(), custom_checks={SHELL_COMMAND_CHECK: check}),
        evidence=evidence,
    )
    verdict = judge.verify(candidate(artifacts=(artifact(rows=({"a": 1},)),)))
    assert verdict.status == "passed"
    assert judge.evidence.last().created_at == 10.0  # type: ignore[union-attr]

    judge.last_edit_at = 20.0  # ★ checked, then changed
    assert judge.verify(candidate(artifacts=(artifact(rows=({"a": 1},)),))).status == "stale"


# --------------------------------------------------------------------------- #
# ★ structured output, layer 1+2: the schema the model is asked for
# --------------------------------------------------------------------------- #


def test_candidate_response_format_carries_the_answer_schema() -> None:
    from bobanana6.assemble_impl import (
        EXPECTED_SHAPE,
        candidate_response_format,
    )

    fmt = candidate_response_format()
    assert fmt["type"] == "json_schema"
    schema = fmt["json_schema"]["schema"]
    # ★ the schema must say exactly what the parser enforces: `ref` is not optional,
    #   and there is no alternative spelling it could pick instead
    assert schema["required"] == ["ref", "columns", "rows"]
    assert "source_id" not in schema["properties"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["rows"]["items"]["type"] == "object"
    # the text fed back on a rejection describes the same contract
    assert "rows" in EXPECTED_SHAPE and "columns" in EXPECTED_SHAPE


def test_every_rejection_carries_a_fix_that_names_the_same_shape() -> None:
    from bobanana6.assemble_impl import EXPECTED_SHAPE
    from bobanana6.assemble_impl.produce import CandidateFormatError, parse_candidate

    block = Subtask(id="T-01", goal="g", inputs=(), output="out.csv", contract_ref="t", axis="source", mode="write")
    with pytest.raises(CandidateFormatError) as error:
        parse_candidate("nope", subtask=block, source_id="s")
    assert error.value.feedback()["expected"] == EXPECTED_SHAPE


# --------------------------------------------------------------------------- #
# ★ capability negotiation: a service that ignores response_format must not be fatal
# --------------------------------------------------------------------------- #


def _produce_io(
    tmp_path: Path,
    client: object,
    *,
    contract_document: object | None = None,
    judged: object | None = None,
    block: object | None = None,
) -> object:
    from bobanana6.assemble_impl import LoopIOImpl, ResponseFormatSupport
    from bobanana6.services import (
        BlockJudge,
        BoundChecks,
        build_registry,
        build_toolset,
    )

    block = block or subtask("T-01", output="out.csv", mode="write", axis="source")
    contract_document = contract_document or contract()
    tools = build_toolset(
        plan=plan(subtasks=(block,), interfaces={block.output: "s"}),
        subtask=block,
        contract=contract_document,
        ontology=mini_ontology(),
        authorized=(),
        fetched={},
        upstream={},
        checks=BoundChecks(block="T-01"),
    )
    store = SnapshotStore(root=tmp_path / "s")
    return LoopIOImpl(
        client=client,  # type: ignore[arg-type]
        toolset=tools,
        registry=build_registry(tools, store.read),
        judge=BlockJudge(bundle=judged or bundle(), subtask=block, context=CheckContext(ontology=mini_ontology())),
        subtask=block,
        budget=Budget(max_calls=8, max_wall_s=60, max_output=4096),
        response_format=ResponseFormatSupport(),
    )


class _SchemaRefusingClient:
    """Refuses the schema once, then answers -- like a service without support."""

    def __init__(self, *, status: int = 400, message: str = "response_format is not supported") -> None:
        self.calls: list[dict] = []
        self.status = status
        self.message = message

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        from bobanana6.adapters import LLMError

        self.calls.append(dict(kw))
        if "response_format" in kw:
            raise LLMError(f"model call failed with HTTP {self.status}: {self.message}", status_code=self.status)
        return ModelResponse(text='{"rows": [{"obn": "A-1"}]}')


# --------------------------------------------------------------------------- #
# ★ `#44` ①: 块级 goal 丢掉的那一层 —— 本任务的性质与验收要求（从契约派生）
# --------------------------------------------------------------------------- #


def _contract_for_a_fix() -> object:
    from bobanana6.core.model import DeliverableSpec, SourceRef, TaskContract

    return TaskContract(
        task_id="inventory-fix",
        goal="修好 input/inventory.py 里的缺陷，让 input/tests/test_inventory.py 全部通过；不许修改测试文件",
        deliverables=(DeliverableSpec(name="input/inventory.py", required=True),),
        sources=(SourceRef(ref="inventory-source", locator="input/inventory.py"),),
    )


def _fix_bundle() -> object:
    return bundle(
        hard=(
            constraint("C-tests-pass", "custom", "input/inventory.py",
                       {"check": SHELL_COMMAND_CHECK, "command": "python3 -m pytest x -q"},
                       rationale="全部测试必须通过（起点是红的）"),
            constraint("C-tests-frozen", "frozen_files", "*", {"sources": ["test-inventory"]},
                       rationale="不许改测试"),
        )
    )


def test_the_task_nature_is_derived_from_the_contract_not_from_the_goal_text() -> None:
    """★ `#44` ①: 「这是一次修复 / 测试必须全过 / 测试不许改」三件事都要能到达块级提示。

    派生规则：交付物路径 == 某个来源的 locator ⇒ `fix`；`shell_command` 的 rationale ⇒ 验收要求；
    `frozen_files` 点名的载体 ⇒ 不许改。
    """
    from bobanana6.assemble_impl.produce import task_nature

    block = subtask("T-03", output="input/inventory.py", inputs=("part-inventory-source.csv",), mode="write")
    nature = task_nature(_contract_for_a_fix(), _fix_bundle(), block)  # type: ignore[arg-type]
    assert nature["kind"] == "fix"
    assert nature["acceptance"] == ["全部测试必须通过（起点是红的）"]
    assert nature["frozen"] == ["test-inventory"]


def test_a_report_class_block_gets_no_task_nature_at_all() -> None:
    """★ 反向：面板类题（交付物不是来源、判据里没有命令、没有冻结载体）⇒ 提示一个字不变。"""
    from bobanana6.assemble_impl.produce import task_nature

    block = subtask("T-04", output="panel.csv", inputs=("part-panel-a.csv",), mode="write")
    judging = bundle(hard=(constraint("C-rows", "presence", "panel.csv", {"min_rows": 1}),))
    assert task_nature(contract(deliverables=("panel.csv",)), judging, block) == {}  # type: ignore[arg-type]


def test_the_task_nature_reaches_the_briefing_the_model_receives(tmp_path: Path) -> None:
    """★ AGENTS §7⑬：断言模型真正收到的那条 brief（不是"某个函数返回了字典"）。"""
    io = _produce_io(tmp_path, _SchemaRefusingClient(), contract_document=_contract_for_a_fix(), judged=_fix_bundle(), block=subtask("T-03", output="input/inventory.py", inputs=("part.csv",), mode="write"))
    brief = json.loads(io.prompt(_state())[1]["content"])  # type: ignore[attr-defined]
    assert brief["subtask"]["goal"].startswith("produce") or brief["subtask"]["goal"]
    assert brief["task"] == {
        "kind": "fix",
        "acceptance": ["全部测试必须通过（起点是红的）"],
        "frozen": ["test-inventory"],
    }


def _state():  # type: ignore[no-untyped-def]
    from bobanana6.agentloop.loop import LoopState

    return LoopState()


def test_the_first_call_is_free_form_and_later_calls_ask_for_the_shape(tmp_path: Path) -> None:
    """★ Forcing a schema on turn 1 would stop the model from ever calling a tool."""
    from bobanana6.assemble_impl import LoopIOImpl

    client = _SchemaRefusingClient()
    io: LoopIOImpl = _produce_io(tmp_path, client)  # type: ignore[assignment]
    assert io.wants_structured_output() is False
    io.produce(_state())
    assert io.calls == 1
    assert "response_format" not in client.calls[0]

    io.produce(_state())
    assert io.calls == 2  # two logical produces...
    # ...but three wire calls: the schema attempt, the refusal, the retry without it
    assert [("response_format" in kw) for kw in client.calls] == [False, True, False]
    assert io.response_format.refused is True
    assert io.response_format.enabled is False
    assert io.wants_structured_output() is False  # remembered, not re-probed


def test_a_service_that_refuses_the_schema_degrades_instead_of_failing(tmp_path: Path) -> None:
    client = _SchemaRefusingClient()
    io = _produce_io(tmp_path, client)
    io.produce(_state())  # turn 1: no schema
    proposal = io.produce(_state())  # turn 2: schema refused, retried without it
    assert proposal.candidate is not None
    assert [("response_format" in kw) for kw in client.calls] == [False, True, False]


def test_a_refusal_is_learned_once_for_the_whole_run(tmp_path: Path) -> None:
    from bobanana6.assemble_impl import ResponseFormatSupport

    support = ResponseFormatSupport()
    first = _produce_io(tmp_path, _SchemaRefusingClient())
    first.response_format = support
    first.produce(_state())
    first.produce(_state())
    assert support.refused is True

    other = _SchemaRefusingClient()
    second = _produce_io(tmp_path / "second", other)
    second.response_format = support  # same run, next block
    second.produce(_state())
    second.produce(_state())
    assert [("response_format" in kw) for kw in other.calls] == [False, False]


def test_a_transport_error_is_not_read_as_a_capability_answer(tmp_path: Path) -> None:
    from bobanana6.adapters import LLMConnectionError
    from bobanana6.assemble_impl import rejects_response_format

    assert rejects_response_format(LLMConnectionError("did not reach")) is False
    assert rejects_response_format(RuntimeError("no status at all")) is False
    assert rejects_response_format(_StatusError(500, "response_format is not supported")) is False
    assert rejects_response_format(_StatusError(400, "response_format is not supported")) is True
    assert rejects_response_format(_StatusError(400, "invalid api key")) is False


def test_a_refused_schema_does_not_swallow_other_failures(tmp_path: Path) -> None:
    client = _SchemaRefusingClient(status=429, message="rate limit exceeded")
    io = _produce_io(tmp_path, client)
    io.produce(_state())
    with pytest.raises(Exception, match="429"):
        io.produce(_state())
    assert io.response_format.enabled is True  # a 429 says nothing about the schema


class _StatusError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status


# --------------------------------------------------------------------------- #
# ★ the declared output allowance is what caps the model call
# --------------------------------------------------------------------------- #


class _RecordingComplete:
    def __init__(self) -> None:
        self.max_tokens: list[int] = []

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        self.max_tokens.append(max_tokens)
        return ModelResponse(text='{"rows": [{"a": 1}]}')


def test_the_model_call_uses_the_declared_output_allowance(tmp_path: Path) -> None:
    """★ Declaring 8192 and spending 512 means the declaration has no consequence."""
    client = _RecordingComplete()
    io = _produce_io(tmp_path, client)
    io.budget = Budget(max_calls=8, max_wall_s=60, max_output=777)
    io.produce(_state())
    assert client.max_tokens == [777]

    io.budget = Budget(max_calls=8, max_wall_s=60, max_output=99)  # the declaration moved
    io.produce(_state())
    assert client.max_tokens == [777, 99]


def test_an_allowance_in_another_unit_is_refused_not_reinterpreted(tmp_path: Path) -> None:
    """★ ``max_tokens`` is counted in tokens; a byte allowance has no conversion."""
    from bobanana6.assemble_impl import OutputLimitUnitError

    client = _RecordingComplete()
    io = _produce_io(tmp_path, client)
    io.budget = Budget(max_calls=8, max_wall_s=60, max_output=4096, max_output_unit="bytes")
    with pytest.raises(OutputLimitUnitError, match="declared in 'bytes'"):
        io.produce(_state())
    assert client.max_tokens == []  # nothing was sent

    from bobanana6.agentloop.errors import BobananaAction, classify_error

    # declared, so the loop can route it: the request shape has to change
    assert classify_error(OutputLimitUnitError("x")).action is BobananaAction.FIX_CONSTRAINT


def test_a_zero_allowance_is_refused(tmp_path: Path) -> None:
    from bobanana6.assemble_impl import OutputLimitUnitError, model_output_cap

    with pytest.raises(OutputLimitUnitError, match="positive cap"):
        model_output_cap(Budget(max_calls=1, max_wall_s=1, max_output=0))


# --------------------------------------------------------------------------- #
# ★ #32：投递载荷按声明的形式规整（块看到的那一份）
# --------------------------------------------------------------------------- #


def test_a_declared_carrier_form_is_normalized_before_the_block_sees_it(tmp_path: Path) -> None:
    body = "def f():\r\n    # 归一\r\n".encode("gbk")
    _store, snap = _snapshot(tmp_path, suffix=".txt", body=body)

    payload = json.loads(
        snapshot_payload("src-1", snap, body, SourceForm(encoding="gbk", eol="lf"))  # type: ignore[arg-type]
    )
    text = payload["rows"][0]["text"]

    assert "归一" in text and "\ufffd" not in text and "\r" not in text
    assert payload["source_form"] == {
        "encoding": "gbk",
        "assumed": False,
        "replacement_chars": 0,
        "eol": "lf",
        "eol_normalized": True,
    }


def test_an_undeclared_garbled_carrier_carries_a_spoken_assumption(tmp_path: Path) -> None:
    body = "café".encode("latin-1")
    _store, snap = _snapshot(tmp_path, suffix=".txt", body=body)

    payload = json.loads(snapshot_payload("src-1", snap, body))  # type: ignore[arg-type]

    assert "\ufffd" in payload["rows"][0]["text"]
    assert payload["source_form"]["assumed"] is True
    assert payload["source_form"]["replacement_chars"] == 1


def test_an_undeclared_clean_carrier_payload_is_unchanged(tmp_path: Path) -> None:
    """★★ 反向（载荷层）：没声明 + 干净 ⇒ 载荷里连 `source_form` 这个键都不出现。"""
    _store, snap = _snapshot(tmp_path)

    payload = json.loads(snapshot_payload("src-1", snap, b"a,b\n1,2\n"))  # type: ignore[arg-type]

    assert "source_form" not in payload
    assert payload["rows"] == [{"a": "1", "b": "2"}]


# --------------------------------------------------------------------------- #
# ★ `#37` 提出、`#39` 用真模型四臂对照改写了形态：判据 `spec` 点名的**冻结载体 ref**
#   要在派活前送到块手上 —— 但**只给名字 + 指路，不塞原文**。
#   实测依据（`.bobanana6/b39/报告.md` §5）：同一窗口里，不给指路的那一臂 `sources.snapshot`
#   取回 **0** 次；给"名字 + 指路"的那一臂取回 **29** 次（三跑）且 T-01 3/3；
#   而"塞全文 + 与候选同形（`{columns, rows}`）"那一臂 7/7 次把参照物原文抄进 `text` 列。
# --------------------------------------------------------------------------- #


def _reference_io(
    tmp_path: Path,
    *,
    spec: Mapping[str, Any],
    target: str = "*",
    authorized: tuple[str, ...] = ("interface",),
    body: str = "def total(items):\n    return sum(items)\n",
) -> Any:
    from bobanana6.assemble_impl import LoopIOImpl, ResponseFormatSupport
    from bobanana6.core.model import Column, Constraint, SnapshotBody
    from bobanana6.services import build_registry

    block = subtask("T-02", output="input/repo/shared.py", mode="write", axis="module")
    tools = build_toolset(
        plan=plan(subtasks=(block,), interfaces={"input/repo/shared.py": "text"}),
        subtask=block,
        contract=contract(deliverables=("input/repo/shared.py",)),
        ontology=mini_ontology(),
        authorized=tuple(SourceRef(ref=ref, locator=f"input/repo/{ref}.py") for ref in authorized),
        fetched={},
        upstream={},
        checks=BoundChecks(block="T-02"),
    )
    frozen = SnapshotBody(
        ref="interface",
        columns=(Column(name="text", dtype="str"),),
        rows=({"text": body},),
    )
    judged = bundle(
        hard=(
            Constraint(
                id="C-tests-frozen",
                kind="frozen_files",
                target=target,
                spec=dict(spec),
                rationale="frozen carriers are compared verbatim",
            ),
        )
    )
    return LoopIOImpl(
        client=_SchemaRefusingClient(),
        toolset=tools,
        registry=build_registry(tools, SnapshotStore(root=tmp_path / "s").read),
        judge=BlockJudge(
            bundle=judged,
            subtask=block,
            context=CheckContext(ontology=mini_ontology(), snapshot_bodies={"interface": frozen}),
        ),
        subtask=block,
        budget=Budget(max_calls=8, max_wall_s=60, max_output=4096),
        response_format=ResponseFormatSupport(),
    )


def _answer_shaped(payload: Any) -> bool:
    """载荷里任何一层出现 `{"columns": …, "rows": …}` 就算"长得像候选"。"""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "columns" in node and "rows" in node:
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def test_a_judge_named_reference_reaches_the_messages_as_a_name_and_a_pointer(tmp_path: Path) -> None:
    """★ `#39` 的形态：判据点名的 ref 到块手上（名字 + 取法），**原文不塞进来**。"""
    io = _reference_io(tmp_path, spec={"sources": ["interface"]})

    delivered = io._delivered_inputs()
    assert delivered["reference_index"] == ["interface"]
    assert "sources.snapshot" in delivered["reference_note"]
    assert "columns" in delivered["reference_note"]  # 说清答案形状
    # ★ AGENTS.md §7⑬：断言模型真正收到的那串 messages；并且**载荷里不许有答案形状**
    prompt = json.dumps(io.prompt(_state()), ensure_ascii=False)
    assert "reference_index" in prompt and "interface" in prompt
    assert not _answer_shaped(delivered), delivered
    assert "def total(items)" not in prompt  # 原文没有塞进来


def test_a_reference_the_block_may_not_read_is_not_delivered(tmp_path: Path) -> None:
    """反向：没授权的 ref 即便被判据点名也不送 —— 前置投递不放宽授权。"""
    io = _reference_io(tmp_path, spec={"sources": ["secret-1"]})

    assert "reference_index" not in io._delivered_inputs()


def test_a_reference_named_for_another_block_is_not_delivered(tmp_path: Path) -> None:
    """反向：判据点的是别人的产物 ⇒ 不是这一块的参照物。"""
    io = _reference_io(tmp_path, spec={"sources": ["interface"]}, target="other/thing.py")

    assert "reference_index" not in io._delivered_inputs()


def test_the_reference_delivery_is_names_only_no_matter_how_big_the_carrier_is(tmp_path: Path) -> None:
    """只给名字 ⇒ 载体再大，投递也还是那一串名字（原来那条 120k 预算退路因此不再需要）。"""
    io = _reference_io(tmp_path, spec={"sources": ["interface"]}, body="x" * 200_000)

    delivered = io._delivered_inputs()
    assert delivered["reference_index"] == ["interface"]
    assert len(json.dumps(delivered, ensure_ascii=False)) < 500


def test_named_source_refs_reads_nested_specs_and_only_keeps_authorized_names() -> None:
    from bobanana6.assemble_impl.produce import named_source_refs

    spec = {"sources": ["interface", "nope"], "nested": {"deeper": ["test-shop-a"]}}
    assert named_source_refs(spec, ("interface", "test-shop-a")) == ("interface", "test-shop-a")
    # 去重 + 只认整串相等（`interface` 出现在长文本里不算点名）
    assert named_source_refs({"a": ["interface", "interface"], "b": "see interface.py"}, ("interface",)) == ("interface",)
    assert named_source_refs({"a": "interface"}, ()) == ()


def _merge_io(tmp_path: Path, answer: str, observe_dir: Path | None) -> Any:
    """一个真的 `LoopIOImpl`：合并块、手里有一件上游部件、可开观测目录（`#43`）。"""
    from bobanana6.assemble_impl import LoopIOImpl, ResponseFormatSupport
    from bobanana6.services import build_registry

    block = subtask("T-04", output="panel.csv", inputs=("part-panel-c.csv",), mode="write", axis="field")
    parts = {
        "part-panel-c.csv": {
            "name": "part-panel-c.csv",
            "columns": [{"name": "obligor", "dtype": "str"}, {"name": "amount", "dtype": "str"}],
            "rows": [{"obligor": "C-001", "amount": 90_000.0}],
            "provenance": [
                {"value_ref": "part-panel-c.csv:row=0", "snapshot_ref": "panel-c", "source_row": 0, "locator": "L"}
            ],
        }
    }
    tools = build_toolset(
        plan=plan(subtasks=(block,), interfaces={"panel.csv": "s"}),
        subtask=block,
        contract=contract(deliverables=("panel.csv",)),
        ontology=mini_ontology(),
        authorized=(SourceRef(ref="panel-c", locator="input/panel-c.csv"),),
        fetched={},
        upstream=parts,
        checks=BoundChecks(block="T-04"),
        # ★ 合并块的 `axis` 不在只读来源那一档 ⇒ 它引用的是**上游部件带来的**快照
        #   （`inherited_sources`）—— 真实 runner 就是这么给的。
        inherited_sources=("panel-c",),
    )
    return LoopIOImpl(
        client=_AnsweringClient(answer),
        toolset=tools,
        registry=build_registry(tools, SnapshotStore(root=tmp_path / "s").read),
        judge=BlockJudge(bundle=bundle(), subtask=block, context=CheckContext(ontology=mini_ontology())),
        subtask=block,
        budget=Budget(max_calls=8, max_wall_s=60, max_output=4096),
        response_format=ResponseFormatSupport(),
        observe_dir=observe_dir,
    )


class _AnsweringClient:
    """每次都答同一份文本（够用来把产出路径走到底）。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        return ModelResponse(text=self.answer)


def test_the_inheritance_guard_writes_its_observation_only_when_it_refuses(tmp_path: Path) -> None:
    """★ `#43` 的端到端：真走 `LoopIOImpl.produce` —— 拒收落盘、通过不落盘。"""
    from bobanana6.assemble_impl.produce import CandidateFormatError

    refused_dir = tmp_path / "refused"
    io = _merge_io(tmp_path, '{"ref": "panel-c", "rows": [{"obligor": "C-001", "amount": 90000000.0}]}',
                   refused_dir)
    with pytest.raises(CandidateFormatError):
        io.produce(_state())
    written = sorted(path.name for path in refused_dir.glob("*.json"))
    assert written == ["guard-pool-T-04-1.json"], written
    record = json.loads((refused_dir / written[0]).read_text(encoding="utf-8"))
    assert record["pool_size"] == 1 and record["output"] == "panel.csv"
    assert record["nearest"]["difference"] == {"amount": {"sent": 90000000.0, "pool": 90000.0}}
    assert record["pool_diagnosis"]["overall"] == "pool_non_empty"

    # ★ 反向：逐字搬运（该过）——同一个观测目录里一个文件都不许多
    accepted_dir = tmp_path / "accepted"
    ok_io = _merge_io(tmp_path, '{"ref": "panel-c", "rows": [{"obligor": "C-001", "amount": 90000.0}]}',
                      accepted_dir)
    candidate = ok_io.produce(_state()).candidate
    assert candidate is not None and candidate.provenance
    assert list(accepted_dir.glob("*")) == []


def test_the_system_prompt_is_rendered_from_the_answer_contract(tmp_path: Path) -> None:
    """★ `#42`: 断言**模型真正收到的那串 messages** 里带着契约的字段名与粒度。

    （不 grep 源码：渲染器存在但没人调，是 `AGENTS §7⑬` 记过的那类"写下来又没人读"。）
    """
    from bobanana6.assemble_impl.answer_contract import ANSWER_CONTRACT

    io = _produce_io(tmp_path, _SchemaRefusingClient())
    system = next(message["content"] for message in io.prompt(_state()) if message["role"] == "system")
    for answer_field in ANSWER_CONTRACT:
        assert f'"{answer_field.name}"' in system, answer_field.name
        assert answer_field.granularity in system, answer_field.name


# --------------------------------------------------------------------------- #
# ★ #39: 工具体检 —— 那个 bug 只在【运行期】现形（字段遮蔽方法）
#   `BoundTask` 的 dataclass 字段 `contract` 遮蔽了同名方法 `contract()`（实例 `__dict__`
#   优先于类体的非数据描述符）⇒ `task.contract` 这个工具**永远**抛
#   `TypeError: 'TaskContract' object is not callable`。静态读代码看不出来，逐个调一次才看得见。
# --------------------------------------------------------------------------- #

#: 每个工具的"像样的"参数（模型会发的那种）。
_PLAUSIBLE_ARGS: dict[str, dict[str, Any]] = {
    "semantics.lookup": {"term": "credit.pd"},
    "semantics.map": {"field": "pd_3y"},
    "semantics.check": {"usage": {"concept": "credit.pd"}},
    "task.mysubtask": {},
    "task.upstream": {"name": "part-1"},
    "task.upstream_all": {},
    "task.contract": {},
    "check.local": {},
    "check.provenance": {},
    "check.assemble_dryrun": {},
    "sources.snapshot": {"ref": "src-1"},
}


def _full_toolset(tmp_path: Path) -> tuple[object, object]:
    """一个真的装配出来的工具面（所有服务都在），用来逐个调。"""
    _store, snap = _snapshot(tmp_path)
    block = subtask("T-01", output="out.csv", inputs=("part-1",), mode="write")
    tools = build_toolset(
        plan=plan(subtasks=(block,), interfaces={"out.csv": "s"}),
        subtask=block,
        contract=contract(),
        ontology=mini_ontology(),
        authorized=(SourceRef(ref="src-1", locator="input/a.csv"),),
        fetched={"src-1": snap},
        upstream={"part-1": {"name": "part-1", "columns": [], "rows": []}},
        checks=BoundChecks(block="T-01"),
    )
    return tools, build_registry(tools, _store.read)


def test_every_served_tool_can_actually_be_called(tmp_path: Path) -> None:
    """★ #39 的回归守卫：任何一个工具被调用都不许抛 `TypeError`。"""
    _tools, registry = _full_toolset(tmp_path)
    results = {}
    for name in registry.names():
        call = registry.calls([_raw(f"c-{name}", name, json.dumps(_PLAUSIBLE_ARGS.get(name, {})))])[0]
        results[name] = registry.execute((call,))[0]
    assert [name for name, result in results.items() if result.error_type == "TypeError"] == []
    # 那个 bug 的正面读数：`task.contract` 交的是合同本身（不是"调用一个对象"）
    payload = json.loads(results["task.contract"].content)
    assert payload["task_id"] == "task-1" and payload["goal"]


def test_the_served_tool_list_matches_the_declared_inventory() -> None:
    """★ #39: 注册表服务 11 个工具，`TOOL_NAMES` 只列了 10 个（漏 `task.upstream_all`）——
    而 `TOOL_NAMES` 是 `runner` 喂给 `consist._tool_missing` 的"部署能服务什么"清单
    ⇒ 一条 `spec.requires: [task.upstream_all]` 的约束会被误报成能力不可用。"""
    from bobanana6.core.ports import _BASE_TOOL_SPECS

    assert set(TOOL_NAMES) == {spec.name for spec in _BASE_TOOL_SPECS}
    assert "task.upstream_all" in TOOL_NAMES
