"""services/: tool surface, call metadata filled by the registry, evidence recording."""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
    BlockJudge,
    BoundChecks,
    BoundSemantics,
    BoundSources,
    BoundTask,
    RecordedEvidence,
    RegisteredTool,
    ToolRegistry,
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
    view = BoundTask(subtask=_subtask(), contract=contract()).mysubtask()
    assert view["id"] == "T-01"
    assert view["inputs"] == ["part-1.csv"]
    assert view["mode"] == "write"


def test_upstream_refuses_an_input_the_block_never_declared() -> None:
    task = BoundTask(
        subtask=_subtask(),
        contract=contract(),
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
    passed, facts = check(constraint(kind="custom", spec={"check": SHELL_COMMAND_CHECK, "command": PASSES}), candidate(), CheckContext())
    assert passed is True
    assert evidence.last() is not None
    assert evidence.last().exit_code == 0  # type: ignore[union-attr]
    assert evidence.last().created_at == 100.0  # type: ignore[union-attr]
    assert "exited 0" in facts[0]


def test_shell_command_check_fails_on_a_non_zero_exit() -> None:
    evidence = RecordedEvidence()
    check = shell_command_check(evidence, lambda: 1.0)
    command = f'{sys.executable} -c "import sys; sys.exit(3)"'
    passed, facts = check(constraint(kind="custom", spec={"check": SHELL_COMMAND_CHECK, "command": command}), candidate(), CheckContext())
    assert passed is False
    assert "exited 3" in facts[0]


def test_shell_command_check_refuses_an_unattributable_exit() -> None:
    """★ A backgrounded command donates nothing: its exit status is not evidence."""
    evidence = RecordedEvidence()
    check = shell_command_check(evidence, lambda: 1.0)
    command = f'{sys.executable} -c "print(1)" &'
    passed, facts = check(constraint(kind="custom", spec={"check": SHELL_COMMAND_CHECK, "command": command}), candidate(), CheckContext())
    assert passed is False
    assert any("not attributable" in fact for fact in facts)


def test_shell_command_check_needs_a_command() -> None:
    check = shell_command_check(RecordedEvidence(), lambda: 1.0)
    passed, facts = check(constraint(kind="custom", spec={}), candidate(), CheckContext())
    assert passed is False and "spec.command" in facts[0]


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


def _produce_io(tmp_path: Path, client: object) -> object:
    from bobanana6.assemble_impl import LoopIOImpl, ResponseFormatSupport
    from bobanana6.services import (
        BlockJudge,
        BoundChecks,
        build_registry,
        build_toolset,
    )

    block = subtask("T-01", output="out.csv", mode="write", axis="source")
    tools = build_toolset(
        plan=plan(subtasks=(block,), interfaces={"out.csv": "s"}),
        subtask=block,
        contract=contract(),
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
        judge=BlockJudge(bundle=bundle(), subtask=block, context=CheckContext(ontology=mini_ontology())),
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
