"""验收集：判分器结构、多块 pipeline 的机制、反例集（`实现规格.md §14`）。

★ 这一层判的是**机制**，不是这一次的输出质量（§14.4）。所以断言落在
「切出几块 / 跨块依赖有没有被声明 / 装配检查有没有生效 / 溯源链能不能走」
这些确定性事实上，而不是「答得像不像」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from acceptance import EXIT_KINDS, AcceptanceReport, load_baseline, run_task, smoke_gate
from bobanana6.contract import compile_bundle, parse_contract_file
from bobanana6.core.model import ModelResponse, Recon
from bobanana6.core.split import check_split
from bobanana6.decompose import Decomposer
from bobanana6.ontology import load_ontology_file

ACCEPTANCE = Path(__file__).resolve().parents[1] / "acceptance"


def _plan_for(task_dir: Path, *, strategy: str = "source", task_file: str = "task.yaml"):
    """切一道题，返回 (contract, ontology, plan, bundle)。不跑模型。"""
    document = parse_contract_file(task_dir / task_file)
    ontology = load_ontology_file(task_dir / "ontology.yaml").freeze()
    recon = Recon(source_ids=tuple(source.ref for source in document.contract.sources))
    plan = Decomposer(strategy=strategy).split(document.contract, ontology, recon)
    bundle = compile_bundle(document, plan)
    return document, ontology, plan, bundle


# --------------------------------------------------------------------------- #
# §14.3 三条硬纪律
# --------------------------------------------------------------------------- #


#: ★ 判据不是「字段必须叫别的名字」，而是「不许出现把三口径合成一个数的字段」。
#: 所以按**词**扫（`overall_score` → {overall, score}），而不是按精确名字扫：
#: 只查 `overall` / `score` 会漏掉 `overall_score`（变异 U01 就是这么漏的）。
#: ★ 也不能按子串扫 —— `degrade_single` 里就含 `grade`。
FORBIDDEN_TOTAL_WORDS = frozenset({"overall", "score", "total", "reward", "grade", "weighted", "aggregate"})


def _words(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z]+", text.lower()))


def test_report_has_no_single_score() -> None:
    """★ 禁单一总分 —— 三个维度会朝不同方向动，总分掩盖它。"""
    offenders = [name for name in AcceptanceReport.__dataclass_fields__ if _words(name) & FORBIDDEN_TOTAL_WORDS]
    assert offenders == [], f"a single synthesized number crept in: {offenders}"
    report = AcceptanceReport(
        block_pass_rate=1.0, assembly_passed=True, attribution_accuracy=0.0, coverage=dict.fromkeys(EXIT_KINDS, "hit")
    )
    rendered = report.render()
    assert not (_words(rendered) & FORBIDDEN_TOTAL_WORDS), rendered


def test_report_declares_uncovered_exits() -> None:
    """★ 没被走到的出口 = 未验证的代码 —— 必须显式声明。"""
    coverage = {kind: ("hit" if kind in ("deliver", "retry_local") else "not_hit") for kind in EXIT_KINDS}
    report = AcceptanceReport(
        block_pass_rate=0.5, assembly_passed=False, attribution_accuracy=0.0, coverage=coverage
    )
    assert not report.coverage_is_complete()
    assert "decline" in report.uncovered()
    assert "decline" in report.render()  # ★ 渲染里必须带「本次没测到什么」
    assert "not hit" in report.render()


def test_a_report_without_coverage_is_refused() -> None:
    """覆盖声明不是可选的：缺一条就构造不出来。"""
    with pytest.raises(ValueError, match="must declare every exit"):
        AcceptanceReport(block_pass_rate=0.0, assembly_passed=False, attribution_accuracy=0.0, coverage={})


def test_smoke_uses_count_not_boolean_reward() -> None:
    """★ 用通过数对比容差，不用 reward 二值。"""
    baseline = load_baseline()
    assert isinstance(baseline, dict) and baseline
    for task, row in baseline.items():
        assert isinstance(row["min_pass_count"], int), task
        assert "reward" not in row and "pass" not in row, task
    assert smoke_gate(5, 5) is True
    assert smoke_gate(4, 5) is False


def test_the_baseline_loader_refuses_a_reward(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"t": {"pass": True}}), encoding="utf-8")
    with pytest.raises(TypeError, match="min_pass_count"):
        load_baseline(bad)


# --------------------------------------------------------------------------- #
# §14.2 A01 —— 跨块依赖必须被【声明】，不是被硬切掉
# --------------------------------------------------------------------------- #


def test_the_multi_block_task_really_cuts_into_several_blocks() -> None:
    """★ 单块题考不到的四条，第一条：切分器真的扇出了。"""
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    _document, ontology, plan, bundle = _plan_for(task_dir)

    assert plan.shape == "pipeline"
    assert len(plan.subtasks) == 4
    assert [block.mode for block in plan.subtasks] == ["read", "read", "read", "write"]
    assert check_split(plan, _document.contract, bundle, ontology, frozen_hash=ontology.content_hash()).ok


def test_the_cross_block_dependency_is_declared_not_cut() -> None:
    """★ A01：合并不块只看得见自己那一份，它【声明了】要消费哪些块。"""
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    document, ontology, plan, bundle = _plan_for(task_dir)
    merge = plan.subtasks[-1]

    assert merge.inputs == ("part-panel-a.csv", "part-panel-b.csv", "part-panel-c.csv")
    # 消费者声明的期望与生产者的接口一字不差 —— 这就是 iface_match 检查的东西
    assert dict(merge.expects) == {name: plan.interfaces[name] for name in merge.inputs}
    report = check_split(plan, document.contract, bundle, ontology, frozen_hash=ontology.content_hash())
    assert "iface_match" not in report.codes()
    assert "no_orphan" not in report.codes()


def test_each_block_is_independently_verifiable() -> None:
    """★ 多块之后每一块仍然各自可判定（不是「整块才判得了」）。"""
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    document, ontology, plan, bundle = _plan_for(task_dir)
    report = check_split(plan, document.contract, bundle, ontology, frozen_hash=ontology.content_hash())
    assert "verifiable" not in report.codes()
    for block in plan.subtasks:
        assert any(constraint.target in ("*", block.id, block.output) for constraint in bundle.hard), block.id


def test_the_isolation_rule_holds_the_run_only_gets_input() -> None:
    """★ §14.1：真值目录从不交给运行 —— 不隔离就分不开「没答对」和「偷看了」。"""
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task_dir, client=_ScriptedBlocks(), env={})
    assert run.inputs_given, "the harness reported no inputs at all"
    assert all(path.startswith("input/") for path in run.inputs_given)
    assert all("truth" not in path for path in run.inputs_given)


# --------------------------------------------------------------------------- #
# 一个会真跑完 4 块的脚本化模型（机制用；真模型那一侧见交付报告）
# --------------------------------------------------------------------------- #


class _ScriptedBlocks:
    """一份脚本化的多块模型：读块取自己的快照，合并块逐个拉上游再合并。

    它用来把**机制**钉住（切了几块 / 依赖有没有被声明 / 装配会不会报），
    真模型那一侧见交付报告。
    """

    def __init__(self, *, break_one_read: bool = False) -> None:
        self.break_one_read = break_one_read
        self.calls: list[str] = []

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        block = _block_of(messages)
        part = _output_of(messages)  # e.g. "part-panel-a.csv" / "panel.csv"
        self.calls.append(block)
        offered = _tool_names(tools)

        if part == "panel.csv":
            fetched = _upstream_payloads(messages)
            pending = [name for name in _expected_inputs() if name not in fetched]
            if pending:
                return ModelResponse(
                    tool_calls=(_tool_call(f"u{len(fetched)}", "task.upstream", {"name": pending[0]}),)
                )
            rows = [row for payload in fetched.values() for row in payload.get("rows", []) if isinstance(row, dict)]
            # 一个称职的合并按主体去重（真值就是这么定的：A-003 与 B-002 各出现两次）
            deduped: dict[str, dict] = {}
            for row in rows:
                deduped.setdefault(str(row.get("obligor")), row)
            merged = {"ref": "panel-a", "rows": list(deduped.values()), "columns": _columns_of(fetched)}
            return ModelResponse(text=json.dumps(merged))

        snapshot = _snapshot_payload(messages)
        if snapshot is None:  # nothing fetched yet: read this block's own source
            ref = part[len("part-") : -len(".csv")] if part.startswith("part-") else "panel-a"
            tool = "sources.snapshot" if "sources.snapshot" in offered else offered[0]
            return ModelResponse(tool_calls=(_tool_call(f"s{len(self.calls)}", tool, {"ref": ref}),))
        rows = snapshot.get("rows", [])
        if self.break_one_read and block == "T-01":
            rows = []
        return ModelResponse(
            text=json.dumps({"ref": snapshot.get("ref", ""), "rows": rows, "columns": snapshot.get("columns", [])})
        )


def _briefing(messages):  # type: ignore[no-untyped-def]
    for message in reversed(messages):
        if message.get("role") != "user":  # ★ tool results are JSON too -- skip them
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith("{"):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue
    return {}


def _block_of(messages) -> str:  # type: ignore[no-untyped-def]
    return str(_briefing(messages).get("subtask", {}).get("id", ""))


def _output_of(messages) -> str:  # type: ignore[no-untyped-def]
    return str(_briefing(messages).get("subtask", {}).get("output", ""))


def _expected_inputs() -> tuple[str, ...]:
    return ("part-panel-a.csv", "part-panel-b.csv", "part-panel-c.csv")


def _tool_names(tools) -> list[str]:  # type: ignore[no-untyped-def]
    return [str(spec.get("function", {}).get("name")) for spec in tools if isinstance(spec, dict)]


def _tool_call(call_id: str, name: str, arguments) -> dict:  # type: ignore[no-untyped-def]
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _tool_payloads(messages) -> list[dict]:  # type: ignore[no-untyped-def]
    payloads: list[dict] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            document = json.loads(str(message.get("content")))
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict):
            payloads.append(document)
    return payloads


def _snapshot_payload(messages):  # type: ignore[no-untyped-def]
    for payload in _tool_payloads(messages):
        if "rows" in payload and "ref" in payload:
            return payload
    return None


def _upstream_payloads(messages) -> dict[str, dict]:  # type: ignore[no-untyped-def]
    """The parts the merge block has already pulled, keyed by input name."""
    fetched: dict[str, dict] = {}
    for index, payload in enumerate(_tool_payloads(messages)):
        if isinstance(payload.get("inputs"), dict):
            fetched.update({str(k): v for k, v in payload["inputs"].items() if isinstance(v, dict)})
        elif "rows" in payload and index < len(_expected_inputs()):
            fetched[_expected_inputs()[index]] = payload
    return fetched


def _columns_of(fetched) -> list[dict]:  # type: ignore[no-untyped-def]
    for payload in fetched.values():
        columns = payload.get("columns")
        if columns:
            return list(columns)
    return [{"name": "obligor"}, {"name": "pd"}, {"name": "amount"}]


def test_the_scripted_multi_block_run_reaches_assembly() -> None:
    """★ 4 块全过 → 装配；合并产物带着自己的溯源引用。"""
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task_dir, client=_ScriptedBlocks(), env={})
    assert run.report.block_pass_rate == 1.0, (run.report.render(), {k: v.failed for k, v in run.verdicts.items()})
    assert run.report.assembly_passed is True, run.assembly_codes
    assert run.plan_shape == "pipeline" and len(run.blocks) == 4
    assert run.report.coverage["deliver"] == "hit"


def test_a_block_that_produces_nothing_is_named_by_the_assembly() -> None:
    """★ 装配检查存在的理由：块没产出东西时，是【装配】说出缺了哪一个。"""
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task_dir, client=_ScriptedBlocks(break_one_read=True), env={})
    assert run.report.block_pass_rate < 1.0
    assert run.report.assembly_passed is False
    assert "cut_complete" in run.assembly_codes, run.assembly_codes


def test_a_global_invariant_cannot_see_a_multi_piece_assembly() -> None:
    """★★ 缺口的看门狗（§14.2 A08）：target="*" 的全局不变量在多块装配上看不见东西。

    `artifact_for` 只在候选【恰好一个产物】时解析 `*`；装配关把**所有** piece 拼成
    一个候选，于是 `*`-targeted 的检查一律报「artifact not found」—— **即使合并
    结果完全正确也会报**。所以「块各自合格但合起来违反全局不变量」这件事，
    今天**表达不出来**（已作为发现上报）。等这条路修好，本测试会变红 → 升级成
    「装配拒绝」的正式断言。
    """
    from bobanana6.core.assemble import check_assembly
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.model import (
        Artifact,
        Column,
        Constraint,
    )

    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    document, ontology, plan, bundle = _plan_for(task_dir)
    global_invariant = Constraint(
        id="G-1", kind="presence", target="*", spec={"min_rows": 1}, rationale="assembly-wide bound"
    )
    from dataclasses import replace

    bundle = replace(bundle, hard=(*bundle.hard, global_invariant))
    pieces = {
        "part-panel-a.csv": Artifact(name="part-panel-a.csv", columns=(Column(name="obligor", dtype="str"),), rows=({"obligor": "A-1"},)),
        "panel.csv": Artifact(name="panel.csv", columns=(Column(name="obligor", dtype="str"),), rows=({"obligor": "A-1"},)),
    }
    report = check_assembly(
        plan, document.contract, bundle, pieces, CheckContext(ontology=ontology), snapshots={}
    )
    assert "global_invariant" in report.codes(), report.codes()
    detail = " ".join(v.detail for v in report.violations if v.check == "global_invariant")
    assert "not found" in detail, detail  # ★ 不是「违反了界」，而是「看不见产物」


# --------------------------------------------------------------------------- #
# §14.4 反例集：不该拆的任务
# --------------------------------------------------------------------------- #


def test_counter_task_is_not_fanned_out() -> None:
    """★ 共享一份状态机的任务，切分器不扇出（今天的机制：没有第二个来源就不切）。

    这是「不做 fake parallelism」在**今天**的真实表达。规格 §14.4 想要的是
    `plan.decision == "refuse_split"` —— 那个出口**在代码里不存在**（已上报），
    所以这里断言的是它今天真能保证的那一半：不扇出。
    """
    task_dir = ACCEPTANCE / "counter" / "state-machine-task"
    document, ontology, plan, bundle = _plan_for(task_dir)
    assert len(plan.subtasks) == 1
    assert plan.shape == "star"
    report = check_split(plan, document.contract, bundle, ontology, frozen_hash=ontology.content_hash())
    assert report.ok, report.codes()


def test_counter_set_gap_refusal_exit_does_not_exist_yet() -> None:
    """★★ 缺口的看门狗（等 `refuse_split` 落地时本测试会变红 → 升级成 §14.4 的判据）。

    三份「看起来可切」的局部视图会被切分器扇成 3 读 + 1 合并，而 11 条合法性检查
    **一条都看不出三方共享同一份状态** —— 门禁会放行。>计划< 这是假的并行。
    """
    task_dir = ACCEPTANCE / "counter" / "state-machine-task"
    document, ontology, plan, bundle = _plan_for(task_dir, task_file="party-task.yaml")
    report = check_split(plan, document.contract, bundle, ontology, frozen_hash=ontology.content_hash())

    assert len(plan.subtasks) == 4, "the shared-state task is no longer fanned out -- upgrade this test"
    assert report.ok, (
        "the gate now refuses a fake-parallel split: `refuse_split` has landed -- "
        "replace this canary with §14.4's `plan.decision == 'refuse_split'` assertion"
    )


# --------------------------------------------------------------------------- #
# §14.6 冒烟门禁：通过数 vs 容差
# --------------------------------------------------------------------------- #


def test_smoke_tasks_are_small_and_offline_runnable() -> None:
    """★ 冒烟题必须分钟级且确定性：3 道题各连跑 5 次，通过数 ≥ 基线容差。"""
    baseline = load_baseline()
    tasks = json.loads((ACCEPTANCE / "smoke" / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    assert sorted(tasks) == sorted(baseline)

    for task in tasks:
        observed = 0
        for _ in range(5):
            run = run_task(ACCEPTANCE / "smoke" / task, env={}, offline=True)
            if run.report.assembly_passed and run.report.block_pass_rate == 1.0:
                observed += 1
        assert smoke_gate(observed, baseline[task]["min_pass_count"]), (task, observed, baseline[task])


# --------------------------------------------------------------------------- #
# §15.4 —— 与真值对照：这是**第四份读数**，不进那三个维度
# --------------------------------------------------------------------------- #


def test_the_scripted_multi_block_run_matches_the_truth_keys() -> None:
    """★ 合成数据的意义：答案对不对可判 —— 而且判据在真值那侧，不在系统自检里。"""
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task_dir, client=_ScriptedBlocks(), env={})
    assert run.truth is not None
    assert run.truth.matched is True, run.truth.render()
    assert run.truth.row_count_actual == 7


def test_a_wrong_answer_passes_every_system_check_but_fails_the_truth() -> None:
    """★★ 真模型那一次的读数就是这个形状：

    4 块全过、装配通过、exit 0 —— 而合并产物是 30 行、主体号是 'B'/'F'/'H' 这类
    单字母，真值 7 行一个都没中。**系统自检判的不是答案对不对。**
    """
    from acceptance import compare_rows, load_truth

    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    truth = load_truth(task_dir)
    invented = [{"obligor": letter, "pd": 0.0, "amount": 910} for letter in "BFLNRSSS"]
    verdict = compare_rows(invented, truth)

    assert verdict.matched is False
    assert verdict.missing_keys == ("A-001", "A-002", "A-003", "B-001", "B-002", "C-001", "C-002")
    assert verdict.invented_keys == ("B", "F", "L", "N", "R", "S")
    assert verdict.row_count_actual == 8 and verdict.row_count_expected == 7
    assert "invented" in verdict.render()


def test_the_truth_is_never_part_of_what_the_run_is_given() -> None:
    """★ §14.1：真值只给判分器读。"""
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task_dir, client=_ScriptedBlocks(), env={})
    assert run.truth is not None
    assert not any("truth" in path for path in run.inputs_given)
