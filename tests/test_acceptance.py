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
import yaml

from acceptance import (
    EXIT_KINDS,
    AcceptanceReport,
    load_baseline,
    load_truth,
    run_task,
    smoke_gate,
)
from bobanana6.adapters import EchoToolResultClient, ScriptedClient
from bobanana6.agentloop.tools import ToolCall
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
    """★ 白名单：基线行只允许 {min_pass_count, baseline_run}，换个名字也绕不过去。

    上一版按精确键名拦 `reward` / `pass` —— 夜报 2026-10-03 的探针证明
    `reward_rate` / `mean_reward` / `overall_score` / 嵌套 `{"gate": {...}}`
    全能溜过去。
    """
    bypasses = (
        {"pass": True},
        {"reward": 0.9},
        {"reward_rate": 0.9},
        {"mean_reward": 0.9},
        {"pass_rate": 0.9},
        {"overall_score": 0.9},
        {"gate": {"reward": 0.9}},
        {"min_pass_count": 5, "reward": 0.9},
    )
    for index, row in enumerate(bypasses):
        bad = tmp_path / f"bad-{index}.json"
        bad.write_text(json.dumps({"t": row}), encoding="utf-8")
        with pytest.raises(ValueError, match="unknown keys"):
            load_baseline(bad)

    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"t": {"min_pass_count": 5, "baseline_run": "r"}}), encoding="utf-8")
    assert load_baseline(ok)["t"]["min_pass_count"] == 5

    wrong_type = tmp_path / "wrong.json"
    wrong_type.write_text(json.dumps({"t": {"min_pass_count": "five"}}), encoding="utf-8")
    with pytest.raises(TypeError, match="min_pass_count"):
        load_baseline(wrong_type)


def test_the_reverse_validation_harness_restores_what_it_mutated(tmp_path: Path) -> None:
    """★ 反向验证脚本被强杀时必须还原，否则变异留在树里让别的测试**假红**。

    这里验的是还原机制本身（记录 → 写回）；信号到达那一跳由
    `install_restore_hooks()` 注册，不在单测里发信号。
    """
    from tools import reverse_validation

    victim = tmp_path / "victim.py"
    victim.write_text("mutated()", encoding="utf-8")
    reverse_validation._RESTORE[victim] = b"original()\n"
    reverse_validation.restore_all()
    assert victim.read_bytes() == b"original()\n"
    assert reverse_validation._RESTORE == {}

    # 没有待还原的东西时也必须安全（进程正常结束时的常见路径）
    reverse_validation.restore_all()
    assert victim.read_bytes() == b"original()\n"


def test_the_harness_refuses_to_measure_a_tree_that_already_carries_a_mutation() -> None:
    """★ SIGKILL 落在变异中途时还原钩子根本不跑 —— 脏树必须被【动手之前】挡住。

    实测教训：一次被杀的跑把 `core/model.py` 的变异留在树里，下一次全量跑
    照样报出「170 捕获 / 1 未捕获」，除了那条自己的锚点没了之外没有任何迹象。
    """
    from tools import reverse_validation

    # 干净树（当前表里每个锚点都在）：这条自检不该报出「锚点不见」
    assert [p for p in reverse_validation.preflight() if "anchor found" in p] == []

    # 锚点已经被上一次跑替换掉的树 = 表里那条变异的【结果】还在文件里
    poisoned = reverse_validation.Mutation(
        ident="T-dirty-tree",
        relative_path="model.py",
        old="DisagreementTypes: tuple[type, ...] = (BlindSpot, BlindSpot, DefinitionMismatch)",
        new="DisagreementTypes: tuple[type, ...] = (BlindSpot, Conflict, DefinitionMismatch)",
        selector="tests/test_model.py::test_three_divergence_types_have_disjoint_shapes",
        note="measuring a tree that already carries a mutation proves nothing",
    )
    problems = reverse_validation.preflight([poisoned])
    assert len(problems) == 1, problems
    assert "T-dirty-tree" in problems[0], problems
    assert "anchor found 0" in problems[0], problems


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

    def __init__(
        self,
        *,
        break_one_read: bool = False,
        invent_rows: int = 0,
        decline_one_read: bool = False,
        normalise_units: bool = True,
    ) -> None:
        self.normalise_units = normalise_units
        self.break_one_read = break_one_read
        #: ★ ② 的 context 场景：上游读块拒答（它没拿到需要的信息），
        #: 于是下游合并块的声明输入**从来没有被生产出来**。
        self.decline_one_read = decline_one_read
        #: ★ A11 的对照开关：读块"编"出这么多行（真值只有 3 行），全部引用自己的快照。
        self.invent_rows = invent_rows
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
            missing = [name for name, payload in fetched.items() if payload.get("missing") is True]
            if missing:
                return ModelResponse(
                    text=json.dumps({"declined": True, "reason": f"input {missing[0]} was never produced"})
                )
            rows = [row for payload in fetched.values() for row in payload.get("rows", []) if isinstance(row, dict)]
            # 一个称职的合并按主体去重（真值就是这么定的：A-003 与 B-002 各出现两次）
            deduped: dict[str, dict] = {}
            for row in rows:
                deduped.setdefault(str(row.get("obligor")), row)
            columns = _columns_of(fetched)
            if not self.normalise_units or not all(
                _values_carry_a_unit(payload.get("rows", []), "amount") for payload in fetched.values()
            ):
                # 合并块只能声明它收到的：有一个输入没标单位，这一列的 unit 就不能成立
                columns = [{key: value for key, value in column.items() if key != "unit"} for column in columns]
            merged = {"ref": "panel-a", "rows": list(deduped.values()), "columns": columns}
            return ModelResponse(text=json.dumps(merged))

        if self.decline_one_read and block == "T-01":
            return ModelResponse(text=json.dumps({"declined": True, "reason": "the source did not carry the field"}))

        snapshot = _snapshot_payload(messages)
        if snapshot is None:  # nothing fetched yet: read this block's own source
            ref = part[len("part-") : -len(".csv")] if part.startswith("part-") else "panel-a"
            tool = "sources.snapshot" if "sources.snapshot" in offered else offered[0]
            return ModelResponse(tool_calls=(_tool_call(f"s{len(self.calls)}", tool, {"ref": ref}),))
        rows = _amounts_normalised(snapshot.get("rows", [])) if self.normalise_units else snapshot.get("rows", [])
        if self.break_one_read and block == "T-01":
            rows = []
        if self.invent_rows and block == "T-01":
            rows = [{"obligor": f"X{i}", "pd": "0.1", "amount": "1"} for i in range(self.invent_rows)]
        return ModelResponse(
            text=json.dumps(
                {
                    "ref": snapshot.get("ref", ""),
                    "rows": rows,
                    "columns": _declared_columns(snapshot.get("columns", []), snapshot.get("rows", []))
                    if self.normalise_units
                    else snapshot.get("columns", []),
                    # ★ 脚本替身交的是**最终答案**：不写 done，循环按新语义不终结。
                                    }
            )
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
    """The parts the merge block has already pulled, keyed by input name.

    Every payload names itself -- including an input that was never produced
    (``{"name": ..., "missing": true}``), so the caller can tell "not yet asked
    for" from "asked for and absent".
    """
    fetched: dict[str, dict] = {}
    for payload in _tool_payloads(messages):
        if isinstance(payload.get("inputs"), dict):
            fetched.update({str(k): v for k, v in payload["inputs"].items() if isinstance(v, dict)})
        elif isinstance(payload.get("name"), str) and payload["name"] in _expected_inputs():
            fetched[payload["name"]] = payload
    return fetched


def _amount_in_cny(value):  # type: ignore[no-untyped-def]
    """A worker reading a source understands its unit: ``"250 千元"`` -> ``250000.0`` (元).

    ★ ① 的落点：归一发生在【读取块】，不在合并块。单位来自值本身
    （`实现规格 §5.0.1`②），换算系数来自本体声明的单位表。
    """
    if not isinstance(value, str):
        return value
    number, _, unit = value.partition(" ")
    try:
        amount = float(number)
    except ValueError:
        return value
    return amount * 1000.0 if unit.strip() == "千元" else amount


def _amounts_normalised(rows):  # type: ignore[no-untyped-def]
    normalised = []
    for row in rows:
        row = dict(row)
        if "amount" in row:
            row["amount"] = _amount_in_cny(row["amount"])
        normalised.append(row)
    return normalised


def _values_carry_a_unit(rows, name: str) -> bool:
    from bobanana6.core.ontology import split_quantity

    return bool(rows) and all(
        isinstance(row.get(name), str) and split_quantity(row[name]) is not None for row in rows
    )


def _declared_columns(columns, rows):  # type: ignore[no-untyped-def]
    """Add ``unit: CNY`` to a column only when every value it read carried a unit.

    ★ ①：能【确立】单位才声明它；源里没标就不声明 —— 那一段的单位只能是假设，
    由判定层说出来，而不是由生产者硬安一个。
    """
    declared = []
    for column in columns or []:
        name = column.get("name") if isinstance(column, dict) else None
        if name and _values_carry_a_unit(rows, name):
            column = {**column, "unit": "CNY"}
        declared.append(column)
    return declared


def _columns_of(fetched) -> list[dict]:  # type: ignore[no-untyped-def]
    for payload in fetched.values():
        columns = payload.get("columns")
        if columns:
            return list(columns)
    return [{"name": "obligor"}, {"name": "pd"}, {"name": "amount"}]


def test_the_scripted_multi_block_run_reaches_assembly() -> None:
    """★ 4 块全过 → 装配；合并产物带着自己的溯源引用。

    `#30` 的反向检验也在这里：多块 pipeline 的计划层决定是 `split`，
    所以 `refuse_split` / `single_agent` 这两格**必须仍是不变**的 `not_hit`
    —— 采集补上计划层不等于把这两格送出去。
    """
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task_dir, client=_ScriptedBlocks(), env={})
    assert run.report.block_pass_rate == 1.0, (run.report.render(), {k: v.failed for k, v in run.verdicts.items()})
    assert run.report.assembly_passed is True, run.assembly_codes
    assert run.plan_shape == "pipeline" and len(run.blocks) == 4
    assert run.report.coverage["deliver"] == "hit"
    assert run.report.coverage["refuse_split"] == "not_hit"
    assert run.report.coverage["single_agent"] == "not_hit"


# --------------------------------------------------------------------------- #
# ★ ② A07 的声明通道：真实 run 里由【产生失败的那一层】自己声明
# --------------------------------------------------------------------------- #


def test_a_real_run_attributes_a_missing_input_to_context() -> None:
    """★ ②：读块拒答 → 合并块的声明输入从来没被生产出来 → 声明的是 context。

    走的全是真代码：`task.upstream` 的载荷说 `missing`，`produce.execute` 落事件，
    runner 的 `_attribute` 读事件流。测试一行 `cause` 都没写。
    """
    run = run_task(
        ACCEPTANCE / "main" / "panel-three-sources",
        client=_ScriptedBlocks(decline_one_read=True),
        env={},
        expected_blame="context",
    )
    assert run.report.attribution_accuracy == 1.0, run.report.notes
    assert any("got 'context'" in note for note in run.report.notes), run.report.notes
    # ★ 而且要【问得出是哪一个输入缺席】才停得下来：载荷不报名字的话，调用方只会
    #   一遍遍再要同一个输入，最后死在预算上（那是另一种失败，不是 context）。
    assert run.verdicts["T-04"].evidence.get("exit") == "declined", run.verdicts["T-04"].evidence
    # ★ `#30` 的九条出口表：`decline` 由**块级**产生，采集一直读得到它 —— 这里钉住。
    assert run.report.coverage["decline"] == "hit"


def test_a_real_run_attributes_a_rejected_block_to_the_constraint() -> None:
    """★ ②：候选被具名约束拒了 → 判定器自己声明 → 归因到 constraint。"""
    shallow = ScriptedClient(
        [
            {"tool_calls": (_tool_call("c1", "sources.snapshot", {"ref": "src-1"}),)},
            {"text": json.dumps({"ref": "src-1", "rows": []})},
        ]
    )
    run = run_task(ACCEPTANCE / "smoke" / "smoke-panel-one-source", client=shallow, env={}, expected_blame="constraint")
    assert run.report.attribution_accuracy == 1.0, run.report.notes
    assert any("got" in note and "constraint" in note for note in run.report.notes), run.report.notes


def test_a_real_run_attributes_an_unknown_tool_to_the_model() -> None:
    """★ ②：模型叫了一个不存在的工具 → 是调用方的错，声明的是 model（不是 tool）。"""
    shallow = ScriptedClient([{"tool_calls": (_tool_call("c1", "semantics.nope", {}),)}])
    run = run_task(ACCEPTANCE / "smoke" / "smoke-panel-one-source", client=shallow, env={}, expected_blame="model")
    assert run.report.attribution_accuracy == 1.0, run.report.notes
    assert any("got 'model'" in note for note in run.report.notes), run.report.notes


def test_an_unreadable_frozen_carrier_is_a_named_gate(monkeypatch) -> None:
    """★ ②：载体读不回来 → 报【具名门 sources】，而不是通用失败。

    换的只是报错的**形式**：读不回来仍然是失败，判定器不许退化成"只查名字"。
    """
    from bobanana6.sources import SnapshotStore

    original = SnapshotStore.read

    def unreadable(self, snapshot):  # type: ignore[no-untyped-def]
        snapshot.body_path.unlink(missing_ok=True)
        return original(self, snapshot)

    monkeypatch.setattr(SnapshotStore, "read", unreadable)
    run = run_task(ACCEPTANCE / "main" / "panel-three-sources", client=_ScriptedBlocks(), env={})
    assert run.refused == "sources", run.report.render()
    assert "sources" in run.report.notes[-1], run.report.notes
    # ★ 一个块都没跑：读不回来不是"降级继续跑"
    assert run.report.block_pass_rate == 0.0


def _copied_task(tmp_path: Path, name: str = "panel") -> Path:
    import shutil

    target = tmp_path / name / "panel-three-sources"
    shutil.copytree(
        ACCEPTANCE / "main" / "panel-three-sources",
        target,
        ignore=shutil.ignore_patterns("_run", "__pycache__", "generate.py"),
    )
    return target


def test_a_dead_source_degrades_the_run_instead_of_killing_it(tmp_path: Path) -> None:
    """★ A06：一个源整体下线 → 用剩下的源继续，并把缺席的源【标出来】。

    修前：`_fetch_sources` 见一个源取不到就 `RunRefused('sources')`，
    可用的那两份也一起丢了 —— 那是"一个输入坏了"被当成"整个 run 坏了"。
    """
    task = _copied_task(tmp_path, "partial")
    (task / "input" / "panel-b.csv").unlink()
    run = run_task(task, client=EchoToolResultClient(), env={})

    assert run.refused is None, run.report.notes
    # ★ 计划只覆盖还拿得到的源：给一个取不到的源切块，那个块只能失败
    assert run.blocks == ("T-01", "T-02", "T-03"), run.blocks
    assert any("sources unavailable: panel-b (1 of 3)" == note for note in run.report.notes), run.report.notes


def test_a_run_whose_only_source_is_dead_is_still_refused(tmp_path: Path) -> None:
    """★ 边界：一个源都没取到 → 还是在 sources 那关拒跑（没有任务可跑了）。"""
    task = _copied_task(tmp_path, "total")
    for ref in ("panel-a", "panel-b", "panel-c"):
        (task / "input" / f"{ref}.csv").unlink()
    run = run_task(task, client=EchoToolResultClient(), env={})
    assert run.refused == "sources", run.report.notes
    assert "sources" in run.report.notes[-1]


def _task_with_a_unit_constraint(tmp_path: Path, *, explicit: bool) -> Path:
    """A copy of the panel task that *requires* the panel's unit to be judged."""
    import shutil

    import yaml

    source = ACCEPTANCE / "main" / "panel-three-sources"
    target = tmp_path / ("strict" if explicit else "lenient") / source.name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("_run", "__pycache__", "generate.py"))
    document = yaml.safe_load((target / "task.yaml").read_text(encoding="utf-8"))
    # the shipped task already declares C-units: replace its spec, do not add a second
    document["constraints"]["hard"] = [c for c in document["constraints"]["hard"] if c["id"] != "C-units"]
    spec: dict[str, object] = {"concept": "amount", "unit": "CNY"}
    if explicit:
        spec["unit_source"] = "explicit"
    document["constraints"]["hard"].append(
        {
            "id": "C-units",
            "kind": "unit_consistency",
            "target": "panel.csv",
            "spec": spec,
            "rationale": "面板的金额必须带单位；一个源不标单位时，这段不能被静默当成元",
        }
    )
    (target / "task.yaml").write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return target


def test_a_mixed_unit_panel_is_caught_only_once_the_task_judges_units(tmp_path: Path) -> None:
    """★★ 真模型跑测（#19）暴露的洞：面板里混着「千元」原值也能过三口径。

    A 客户端的读块**照抄**源里的值（不归一）→ 面板带着 `250 千元`：
      · 把 C-units 摘掉（= #18 之前的主验题）→ 块级/装配/来源全覆盖，加上真值对照
        「rows 7/7, invented 0」—— **看起来完美，值却是错的**
      · 任务要求判单位（现在的主验题）→ 合并块被 C-units 拦下，点到具体行列
    """
    import yaml

    blind = _copied_task(tmp_path, "no-units")
    document = yaml.safe_load((blind / "task.yaml").read_text(encoding="utf-8"))
    document["constraints"]["hard"] = [c for c in document["constraints"]["hard"] if c["id"] != "C-units"]
    (blind / "task.yaml").write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

    silent = run_task(blind, client=_ScriptedBlocks(normalise_units=False), env={})
    assert silent.report.block_pass_rate == 1.0, silent.report.render()
    assert silent.report.assembly_passed is True, silent.assembly_codes
    assert silent.truth is not None and silent.truth.matched is True, silent.truth.render()  # ★ 真值只比主体号
    panel = silent.candidates["T-04"].artifact("panel.csv")
    assert any("千元" in str(row.get("amount")) for row in panel.rows), "the panel kept the source unit"

    judged = run_task(ACCEPTANCE / "main" / "panel-three-sources", client=_ScriptedBlocks(normalise_units=False), env={})
    assert judged.report.block_pass_rate < 1.0, judged.report.render()
    # ★ `#66`: 这条规则现在在【读块】就生效（以前只在合并块爆）—— 照抄千元的那一块
    #   产出时就被点名，脏值不必等到最后。
    caught = {
        block: " ".join(str(value) for key, value in verdict.evidence.items() if key.startswith("C-units"))
        for block, verdict in judged.verdicts.items()
    }
    caught = {block: fact for block, fact in caught.items() if "declares unit '千元'" in fact}
    assert caught, {b: dict(v.evidence) for b, v in judged.verdicts.items()}
    # 读 panel-c 的那一块【自己】就被点名（不是等到合并块）
    assert len(caught) == 1, caught


def test_a_source_that_does_not_label_its_unit_is_not_silently_assumed(tmp_path: Path) -> None:
    """★★ ① 端到端：三个源里【有的源不标单位】。

    读取块只在能**确立**单位时声明它（panel-c 的值自带千元 → 归一并声明 CNY）；
    panel-a/b 的值是光秃秃的数 → 不声明。于是面板那一列的单位【只是假设】：

      · 默认      → 判定层把它**说出来**（"assumption, not a declaration"），不静默
      · 严格模式  → 这个块**被拒**（要求单位显式），而不是把没标的数当成已知单位
    ★ 两种情况下都没有「静默猜出来并当正确的值」。
    """
    lenient = run_task(_task_with_a_unit_constraint(tmp_path, explicit=False), client=_ScriptedBlocks(), env={})
    assert lenient.report.block_pass_rate == 1.0, {
        name: (verdict.failed, dict(verdict.evidence)) for name, verdict in lenient.verdicts.items()
    }
    panel_fact = " ".join(
        str(value) for verdict in lenient.verdicts.values() for key, value in verdict.evidence.items() if key.startswith("C-units")
    )
    assert "assumption, not a declaration" in panel_fact, panel_fact
    assert "carry no unit" in panel_fact, panel_fact

    strict = run_task(_task_with_a_unit_constraint(tmp_path, explicit=True), client=_ScriptedBlocks(), env={})
    assert strict.report.block_pass_rate < 1.0, strict.report.render()
    # ★ `#66`: 严格模式下【没标单位的那一块自己】就被拒（以前是合并块替它挨）
    strict_facts = " ".join(
        str(value) for v in strict.verdicts.values() for key, value in v.evidence.items() if key.startswith("C-units")
    )
    assert "requires the unit to be explicit" in strict_facts, {
        b: dict(v.evidence) for b, v in strict.verdicts.items()
    }


def test_a_unit_heterogeneous_source_is_normalised_at_read_time() -> None:
    """★★ ①：单位异构的端到端 —— 归一在【读取块】，合并块仍然只搬运。

    panel-c 的值自带单位（`实现规格 §5.0.1`②），读取块归一到元；合并块照抄收到的
    行，于是**既不被 `untraceable_row` 拒收**（它确实只是搬运），
    **又不违反 A11**（引用仍指向自己读到的快照行，换算系数来自本体声明的单位表）。
    """
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task_dir, client=_ScriptedBlocks(), env={})
    assert run.report.block_pass_rate == 1.0, (run.report.render(), {k: (v.failed, dict(v.evidence)) for k, v in run.verdicts.items()})
    assert run.report.assembly_passed is True, run.assembly_codes

    panel = run.candidates["T-04"].artifact("panel.csv")
    amounts = {row["obligor"]: row["amount"] for row in panel.rows}
    # ★ 合并产物是【元】：3000 千元 -> 3_000_000 元
    assert amounts["C-002"] == 3_000_000.0, amounts
    assert amounts["B-002"] == 250_000.0, amounts
    # 三个读块自己的溯源引用仍然立得住（换算不是把引用改掉，而是把量换算过去）
    assert run.verdicts["T-04"].evidence["C-prov"].startswith("coverage 1.0000")
    assert run.truth is not None and run.truth.matched is True, run.truth.render()


def test_a_merge_that_transforms_is_accepted_and_reported() -> None:
    """★ 规格 ① 改契约：合并块**改了自己收到的东西** ⇒ 不再当场拒收，而是【收下 + 上报】。

    ★ 改前断言什么：`code == "untraceable_row"`（归一该发生在读取块）。
    ★ 改后断言什么：① 行被**接受**（加工块结构上永远匹配不上逐字，形状判据必然误伤）；
      ② 溯源没丢 —— 落一条**产物级锚点**（指回收到的那个上游件）；
      ③ **不许静默放行** —— `unverified` 上报给出谁/期望/实际（这里是 `amount` 差多少）。
    ★ 为什么没变弱：原来它保护的是"块不许偷偷改值"；那条**已经由 CI 从原始数据重算**接管
      （红线一在 `tests/test_principles_three.py`：真编造的产物必须被重算抓住）。
      这里保护的是新契约的两半：**溯源仍在** + **改动必须可见**。
    """
    from bobanana6.assemble_impl.produce import parse_candidate
    from bobanana6.core.model import Subtask

    merge = Subtask(
        id="T-04", goal="merge the parts", inputs=("part-panel-c.csv",), output="panel.csv",
        contract_ref="panel", axis="field", mode="write",
    )
    parts = {
        "part-panel-c.csv": {
            "rows": [{"obligor": "C-001", "amount": 90_000.0}],
            "provenance": [
                {"value_ref": "part-panel-c.csv:row=0", "snapshot_ref": "panel-c", "source_row": 0, "locator": "L"}
            ],
            "handoff": {"from": "T-03", "producer_commit": "deadbeefcafe"},
        }
    }
    reported: list = []
    # 合并块把 90_000 又乘了一次 —— 它改了自己收到的东西
    candidate = parse_candidate(
        '{"ref": "panel-c", "rows": [{"obligor": "C-001", "amount": 90000000.0}]}',
        subtask=merge, source_id="T-04", authorized_sources=("panel-c",), parts=parts,
        on_unverified=reported.append,
    )
    assert candidate is not None  # ★ ① 不再拒收
    anchors = [p for p in candidate.provenance if p.snapshot_ref == "part-panel-c.csv"]
    assert anchors, candidate.provenance  # ★ ② 溯源没丢
    assert "T-03" in anchors[0].locator and "deadbeefcafe"[:12] in anchors[0].locator
    assert reported, "★ ③ 不许静默放行：改动必须进报告"
    assert reported[0]["column_mismatches"] == [
        {"column": "amount", "expected": 90_000.0, "actual": 90000000.0}
    ]


def test_a_fabricated_answer_is_only_caught_once_the_chain_reads_the_snapshot(monkeypatch) -> None:
    """★★ A11 的离线端到端对照：**同一道题、同一个客户端**，只有尺子不同。

    读块编 30 行（真值只有 3 行），全部引用自己的快照：
      · 修前（链只查名字：没有快照内容）→ 4 块全过、装配通过、exit passed
      · 修后（链逐点重放）→ 合并块被 C-prov 拦下，并把 row=3 点到名
    """
    from bobanana6.assemble_impl.runner import Runner

    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    # 修前读数：把「有没有快照内容」关掉 = 这一版之前的链（只查名字）
    monkeypatch.setattr(Runner, "_snapshot_bodies", lambda self, snapshots: {})
    before = run_task(task_dir, client=_ScriptedBlocks(invent_rows=30), env={})
    assert before.report.block_pass_rate == 1.0, before.report.render()
    assert before.report.assembly_passed is True, before.assembly_codes
    assert "invented 0" not in " ".join(before.report.notes), before.report.notes

    # 修后读数：同一个客户端、同一道题，链能读到快照内容
    monkeypatch.undo()
    after = run_task(task_dir, client=_ScriptedBlocks(invent_rows=30), env={})
    assert after.report.block_pass_rate < 1.0, after.report.render()
    assert after.report.assembly_passed is False, after.assembly_codes
    facts = " ".join(str(verdict.evidence.get("C-prov", "")) for verdict in after.verdicts.values())
    # ★ 两种证伪都点到名：引用的行不存在（3 行的快照被引到第 10 行）、值也对不上
    assert "holds 3 row(s)" in facts, facts
    assert "does not match snapshot" in facts, facts


def test_a_block_that_produces_nothing_is_named_by_the_assembly() -> None:
    """★ 装配检查存在的理由：块没产出东西时，是【装配】说出缺了哪一个。"""
    task_dir = ACCEPTANCE / "main" / "panel-three-sources"
    run = run_task(task_dir, client=_ScriptedBlocks(break_one_read=True), env={})
    assert run.report.block_pass_rate < 1.0
    assert run.report.assembly_passed is False
    assert "cut_complete" in run.assembly_codes, run.assembly_codes


def test_a_global_invariant_sees_the_multi_piece_assembly_and_can_reject_it() -> None:
    """★★ §14.2 A08 的**正式断言**（看门狗到点升级了）。

    修前：`*` 只在候选恰好一个产物时解析 → 装配关把**所有** piece 拼成一个候选 →
    `*` 的检查一律报「artifact not found」，**即使合并结果完全正确也报**；
    「块各自合格、合起来违反全局不变量」这件事表达不出来。
    修后：同构的多个产物合成一张表 → 装配照常判；违反就被装配拒（不再"看不见"）。
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
    # ★ 这条界本身是满足的（装配后共 2 行 ≥ 1）→ 不该报违反，也不该报「找不到产物」
    assert "global_invariant" not in report.codes(), report.codes()

    # ★ 而违反的时候必须被拦住（"判据真的在看"那一半）
    violating = replace(
        bundle,
        hard=(*bundle.hard, Constraint(id="G-2", kind="presence", target="*", spec={"min_rows": 99}, rationale="bound")),
    )
    caught = check_assembly(plan, document.contract, violating, pieces, CheckContext(ontology=ontology), snapshots={})
    assert "global_invariant" in caught.codes(), caught.codes()
    detail = " ".join(v.detail for v in caught.violations if v.check == "global_invariant")
    assert "not found" not in detail, detail  # ★ 是「违反了界」，不是「看不见产物」


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


def test_counter_task_refuses_to_split_and_falls_back_to_single_agent() -> None:
    """★★ §14.4 的正式判据（`#14` 的看门狗到点升级了）。

    三份「看起来可切」的局部视图以前会被扇成 3 读 + 1 合并、门禁放行（假并行）。
    现在判据是**契约的声明**（`sources_share_state`）而不是来源数：
    切分器拒绝扇出、回落单块，而且拒绝的理由跟着计划一起报出来。
    """
    task_dir = ACCEPTANCE / "counter" / "state-machine-task"
    document, ontology, plan, bundle = _plan_for(task_dir, task_file="party-task.yaml")
    report = check_split(plan, document.contract, bundle, ontology, frozen_hash=ontology.content_hash())

    assert plan.decision == "refuse_split", plan.decision
    assert plan.fallback == "single_agent", plan.fallback
    assert len(plan.subtasks) == 1 and plan.shape == "star"
    assert "share one state" in plan.refusal
    # 回落之后这份计划本身是【合法】的：拒绝扇出不是"报个红就完了"
    assert report.ok, report.codes()


def test_a_refused_split_reaches_the_exit_coverage() -> None:
    """★★ `#30` 的反证：这两条出口在**计划层**，采集必须读到它。

    同一跑里 run 级与块级信号**一条都没有**这两条出口（下面两条断言钉住这一点），
    所以它们只可能来自 `SplitPlan.decision` / `.fallback`：

    ```
    修前采集（只读 run 级 ok + 块级 verdict）：refuse_split not_hit · single_agent not_hit
    修后采集（加计划层）                      ：refuse_split HIT     · single_agent HIT
    ```

    ★ 判定语义一个字没动 —— 仍然是 `#25` 那条「契约声明共享状态」的判据。
    """
    task_dir = ACCEPTANCE / "counter" / "state-machine-task"
    run = run_task(task_dir, task_file="party-task.yaml", env={}, offline=True)

    # ① 修后：计划层的两条进了图
    assert run.report.coverage["refuse_split"] == "hit"
    assert run.report.coverage["single_agent"] == "hit"
    assert run.plan_shape == "star" and len(run.blocks) == 1
    # ② 反证的另一半：这一跑既没有门禁拒跑、块级也没有这两条出口 —— 只可能来自计划层
    assert run.refused is None
    reasons = {str(verdict.evidence.get("exit", "")) for verdict in run.verdicts.values()}
    assert not (reasons & {"refuse_split", "single_agent"}), reasons


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


# --------------------------------------------------------------------------- #
# §15.3 同形状 × 3 域：新域的实体列不叫 obligor —— 真值自己声明键列
# --------------------------------------------------------------------------- #


def test_the_truth_comparison_uses_the_key_the_task_declares() -> None:
    """★ 域 2/3 的实体列叫 `site` / `shipper`：对照必须按【真值声明的键列】比。

    不按它比的话，两边的键集都是空的、只剩行数在比 —— 第四口径会静默失效，
    而表面上还是"matched"。
    """
    from acceptance import compare_rows, declared_key

    assert declared_key({"key": "site"}) == "site"
    assert declared_key({}) == "obligor"
    # 复合键今天还不支持：回落按一列比，绝不假装按两列比（缺口已上报）
    assert declared_key({"key": ["obligor", "year"]}) == "obligor"

    document = {"key": "site", "rows": [{"site": "WF-01"}, {"site": "WF-02"}]}
    dropped = compare_rows([{"site": "WF-01"}], document)
    assert dropped.row_count_actual == 1 and dropped.missing_keys == ("WF-02",)
    invented = compare_rows([{"site": "WF-01"}, {"site": "XX-99"}], document)
    assert invented.invented_keys == ("XX-99",)


def test_the_other_two_main_domains_are_the_same_shape() -> None:
    """★ §15.3 同形状 × 3 域：域 2/3 与域 1 只差【实体/字段/单位】，形状一模一样。

    同形状换领域验的是**泛化**：切分/装配/溯源/单位四条机制在别的实体名、别的业务
    含义下也得成立。这里钉住的是形状（4 块 pipeline / 四条主验约束 / 三条来源 /
    真值隔离），不是某一次的输出质量。
    """
    from acceptance import declared_key

    for name, entity_key, unit_concept, unit_symbol in (
        ("panel-energy-sites", "site", "output_mwh", "MWh"),
        ("panel-freight-lanes", "shipper", "weight_kg", "kg"),
    ):
        task_dir = ACCEPTANCE / "main" / name
        run = run_task(task_dir, client=EchoToolResultClient(), env={})
        assert run.refused is None, run.report.notes
        assert run.plan_shape == "pipeline" and len(run.blocks) == 4, (name, run.plan_shape, run.blocks)
        assert run.truth is not None and run.truth.row_count_expected == 7, name
        # ★ 真值自己声明键列 → 对照按它比；键集不空（不然第四口径会静默失效）
        assert declared_key(load_truth(task_dir)) == entity_key
        assert len(run.truth.missing_keys) == 7, (name, run.truth.render())
        assert not any("truth" in path for path in run.inputs_given)

        document = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
        constraints = {row["id"]: row for row in document["constraints"]["hard"]}
        assert {"C-rows", "C-prov", "C-fields", "C-units"} <= set(constraints), (name, sorted(constraints))
        assert constraints["C-units"]["spec"] == {"concept": unit_concept, "unit": unit_symbol}
        locators = [source["locator"] for source in document["sources"]]
        assert len(locators) == 3 and all(locator.startswith("input/") for locator in locators)


# --------------------------------------------------------------------------- #
# ① 输入递送：壳在派活前把声明的输入取好；一件都没到就不许开工
# --------------------------------------------------------------------------- #


def test_the_declared_inputs_are_delivered_before_the_merge_block_is_asked() -> None:
    """★ ①：计划的 inputs 由【壳】在派活前取好、放进 context —— 不再靠模型自己去取。

    （`#22`：E-0 什么都不加 → 0 次取回、编一张表；E-2 加一句提示 → 2/3；
      E-5 直接把部件放进 context → 3/3。所以把"取回"从模型的工作里拿掉。）
    """
    seen: list[list[dict]] = []

    class _Capture(_ScriptedBlocks):
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            if _block_of(messages) == "T-04" and not seen:
                seen.append([dict(message) for message in messages])
            return super().complete(messages, tools, max_tokens, **kw)

    run = run_task(ACCEPTANCE / "main" / "panel-three-sources", client=_Capture(), env={})
    assert run.report.block_pass_rate == 1.0, run.report.render()
    assert seen, "the merge block was never asked"
    delivered = [
        message
        for message in seen[0]
        if message.get("role") == "user" and "delivered_inputs" in str(message.get("content"))
    ]
    assert len(delivered) == 1, [message.get("role") for message in seen[0]]
    body = json.loads(str(delivered[0]["content"]).split("\n", 1)[1])
    assert sorted(body["delivered_inputs"]) == ["part-panel-a.csv", "part-panel-b.csv", "part-panel-c.csv"]
    # ★ 给的是【内容】（列 + 行 + 逐行引用），不是又一份名字清单
    payload = body["delivered_inputs"]["part-panel-a.csv"]
    assert payload.get("missing") is not True
    assert payload["rows"] and payload["provenance"]
    # ★ 而且它不能被当成 briefing（读归档的地方在找那一条 JSON user 消息）
    assert not str(delivered[0]["content"]).startswith("{")


def test_a_block_whose_inputs_never_arrived_is_not_dispatched() -> None:
    """★★ 拆掉"一抓杀停全场"：读块全挂 ⇒ 合并块**仍然开工**（旧行为：0 次调用、直接判死）。

    ★ 函数名是旧行为的遗留（这个条件以前是 `continue`）；本条现在测的是**新约束**：
    照样开工，但缺哪几份、谁答应的要摆进 brief 与证据，降级起点必须留痕。
    实测代价（13f，8 块）：T-01 的答案信封不合法 ⇒ T-02..T-08 全部停工，八块只跑过一块。
    """
    asked: list[str] = []
    briefs: dict[str, dict] = {}

    class _EmptyReads:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages)
            asked.append(block)
            briefs.setdefault(block, _briefing(messages))
            return ModelResponse(text=json.dumps({"ref": "panel-a", "rows": [], "columns": []}))

    run = run_task(ACCEPTANCE / "main" / "panel-three-sources", client=_EmptyReads(), env={})

    # ② 上游缺失 ⇒ 下游【真的被叫了】（旧行为：T-04 从没被问过）、不再记 inputs_unavailable
    assert "T-04" in asked, asked
    verdict = run.verdicts["T-04"]
    assert verdict.evidence.get("exit") != "inputs_unavailable", verdict.evidence
    # ★ 降级起点留痕：缺哪几份 + 答应的块（不许静默）
    gaps = str(verdict.evidence.get("input_gaps") or "")
    assert "part-panel-a.csv (produced by T-01)" in gaps, gaps
    assert "part-panel-b.csv (produced by T-02)" in gaps, gaps
    assert "part-panel-c.csv (produced by T-03)" in gaps, gaps
    # ③ 模型收到的 brief 里能看到"缺的是哪几份" + "不许编"
    delivered = briefs["T-04"].get("input_gaps") or {}
    assert delivered.get("you_were_expected_to_receive") == [
        "part-panel-a.csv",
        "part-panel-b.csv",
        "part-panel-c.csv",
    ], delivered
    assert [entry["input"] for entry in delivered.get("these_did_not_arrive", [])] == [
        "part-panel-a.csv",
        "part-panel-b.csv",
        "part-panel-c.csv",
    ], delivered
    assert "不许凭名字猜" in str(delivered.get("do_not_invent") or ""), delivered
    assert len(delivered.get("legitimate_ways_out") or []) == 2, delivered
    # 该拼不上还是拼不上
    assert run.report.assembly_passed is False


def test_an_invented_table_for_missing_inputs_is_reported_and_caught_by_recomputation() -> None:
    """★★ ④ E-0 反例（规格 ① 改契约后）：拿不到输入就【按输入的名字编一张表】。

    ★ 改前断言什么：继承守卫**当场拒收**（`matches no row this block was handed`）。
    ★ 改后断言什么：块级不拒了 ⇒ **必须换一个地方逮住它**：对账结果落进报告，
      而且**CI 从原始数据重算**判它不对（红线一）。
    ★ 为什么没变弱：原来保护的是"编造要被挡住"，现在保护的是同一件事，
      只是拦住它的位置从"形状判据"换成"**结果对账**"——后者才是能算出对错的判据。
    """
    invented = [
        {"obligor": "part-panel-a.csv", "pd": "0.1", "amount": "100"},
        {"obligor": "part-panel-b.csv", "pd": "0.2", "amount": "200"},
        {"obligor": "part-panel-c.csv", "pd": "0.3", "amount": "300"},
    ]

    class _Inventing:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            if _block_of(messages) != "T-04":
                return ModelResponse(text=json.dumps({"ref": "panel-a", "rows": [], "columns": []}))
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "panel-a",
                        "columns": [
                            {"name": "obligor", "dtype": "str"},
                            {"name": "pd", "dtype": "str"},
                            {"name": "amount", "dtype": "str"},
                        ],
                        "rows": invented,
                        "done": True,
                    }
                )
            )

    run = run_task(ACCEPTANCE / "main" / "panel-three-sources", client=_Inventing(), env={})
    verdict = run.verdicts["T-04"]
    # ★ 块级不再靠形状判据拒收；但它交的东西**必须**留下"没对上"的痕迹
    facts = " ".join(str(value) for value in verdict.evidence.values())
    assert "matches no row this block was handed" in facts or verdict.ok is False, facts
    # ★ 换地方逮住它：CI 从**原始数据**重算 —— 编出来的三行对不上任何真值
    from bobanana6.services.ci import invariant_findings

    # ★ 这条不变量的**形状**是夹具替身（真的那份该由 `spec_extractor` 从判分器里提出来，
    #   见 `tests/test_principles_three.py` 的提取器读数）；这里只要求"编出来的值不满足它"。
    recomputed = invariant_findings(
        ({"left": "amount", "right": "pd", "op": "le", "tol": 0.0},),
        {
            "panel.csv": {
                "columns": [{"name": "amount", "dtype": "str"}, {"name": "pd", "dtype": "str"}],
                "rows": invented,
            }
        },
    )
    assert recomputed and all(entry["ok"] is False for entry in recomputed), recomputed
    assert recomputed[0]["violated"] and recomputed[0]["expected"] and recomputed[0]["got"]


def _chain_task(tmp_path: Path, *, name: str, statement: str = "") -> Path:
    """A two-block chain (``a.csv`` -> ``b.csv``) with a workspace.

    The upstream block's deliverable can land on disk even when its verdict fails
    (the thing本次改动 covers); ``statement`` optionally declares which source is
    the题面.
    """
    task = tmp_path / name
    (task / "input").mkdir(parents=True)
    (task / "input" / "seed.txt").write_text("draft\n", encoding="utf-8")
    (task / "ontology.yaml").write_text(
        "domain: text\nversion: \"2026.1\"\nconcepts:\n  obligor: {label: 主体, kind: entity}\n",
        encoding="utf-8",
    )
    (task / "task.yaml").write_text(
        "task_id: chain\ngoal: chain two products\nversion: 1\n"
        + (f"statement: {statement}\n" if statement else "")
        + "deliverables:\n"
        "  - {name: a.csv, schema: {obligor: str}}\n"
        "  - {name: b.csv, schema: {obligor: str}, depends_on: [a.csv]}\n"
        "sources:\n  - {ref: seed, locator: input/seed.txt}\n"
        "constraints:\n  hard:\n"
        "    - {id: C-1, kind: presence, target: \"*\", spec: {min_rows: 1}, rationale: \"no empty products\"}\n",
        encoding="utf-8",
    )
    return task


def test_a_failed_block_whose_artifact_is_on_disk_still_feeds_downstream(tmp_path: Path) -> None:
    """★★ 判定失败 ≠ 产物不存在：上游只是**信封没封好**（散文开头），产物照样给下游。

    边界：`pieces` 管"下游拿得到什么"、`verdicts` 管"谁通过了" —— 产物进了 `pieces`，
    但上游的 verdict **仍然是 ok=False**；交接件里留 `from_disk` / `producer_verdict`
    两个显式标记（不许静默降级）。
    """
    task = _chain_task(tmp_path, name="disk-artifact-chain")
    briefs: dict[str, dict] = {}
    data = "obligor\nA-1\n"

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages)
            briefs.setdefault(block, _briefing(messages))
            tool_count = sum(1 for message in messages if message.get("role") == "tool")
            if block == "T-01":
                if tool_count == 0:
                    return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
                if tool_count == 1:
                    return ModelResponse(
                        tool_calls=(_tool_call("w1", "files.write", {"path": "a.csv", "content": data}),)
                    )
                # ★ 信封没封好：开头是散文 ⇒ 格式门拒收；但 a.csv 已经在磁盘上。
                return ModelResponse(text="I am still working on it.")
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}],
                        "rows": [{"obligor": "A-1"}],
                        "done": True,
                    }
                )
            )

    run = run_task(task, client=_Client(), env={}, workspace=True)

    upstream = run.verdicts["T-01"]
    assert upstream.ok is False  # ★ ④ 绝不许因为产物在磁盘上就改成通过
    assert upstream.evidence.get("artifact_on_disk") == "a.csv"
    assert (run.workspace / "a.csv").read_text(encoding="utf-8") == data
    entry = (briefs["T-02"].get("upstream") or {}).get("a.csv") or {}
    assert entry.get("rows") == 1, entry  # ★ ② 下游拿到的是【内容】，不再是 missing
    assert entry.get("producer_verdict") == "failed", entry  # ★ 看得出它来自没通过判定的块
    assert entry.get("from_disk") is True, entry
    assert not (briefs["T-02"].get("input_gaps") or {}), briefs["T-02"].get("input_gaps")


def test_the_statement_full_text_reaches_every_block_brief(tmp_path: Path) -> None:
    """★★ 契约声明的题面，**全文**出现在**每个块**的上下文里。

    原来题面只是一条普通来源，块要自己调 `sources.snapshot` 去取 —— 八个块各自读、
    各自理解。后来壳在派活前把同一份原文内联进每个 brief。

    ★★ 规格 ①（静态知识归位）之后它**换了位置**：题面对全 DAG 恒定 ⇒ 进 system 的恒定前缀
    （前缀缓存才命中），brief 里**不再出现**。断言强度不变 —— 仍然是"每个块都拿得到同一份
    **全文**"，只是要说清它在哪一层；并且反向断言 brief 里**没有**它（不许两边都放）。
    """
    task = _chain_task(tmp_path, name="statement-chain", statement="seed")
    briefs: dict[str, dict] = {}
    systems: dict[str, str] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages)
            briefs.setdefault(block, _briefing(messages))
            systems.setdefault(
                block, "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
            )
            if not any(message.get("role") == "tool" for message in messages):
                return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}],
                        "rows": [{"obligor": "A-1"}],
                        "done": True,
                    }
                )
            )

    run_task(task, client=_Client(), env={}, workspace=True)

    assert {"T-01", "T-02"} <= set(briefs), sorted(briefs)
    def static_reference(system: str) -> dict:
        """从恒定前缀里把那份 STATIC REFERENCE 解析回来（它是 JSON，不是散文）。"""
        marker = "repeated in the per-block brief below):\n"
        assert marker in system, system[:200]
        return json.loads(system.split(marker, 1)[1].splitlines()[0])

    for block, brief in briefs.items():
        # ★ 已在 system 的恒定前缀里（换个位置，不是删）：**全文**都在
        static = static_reference(systems[block])
        assert static["statement"]["ref"] == "seed", (block, static.get("statement"))
        assert static["statement"]["text"] == "draft\n", (block, static.get("statement"))
        # ★ 反向：brief 里**不再出现**已移进 system 的键（不许两边都放）
        assert "statement" not in brief, (block, sorted(brief))
    # ★ 恒定前缀在**不同块之间**逐字节相同（缓存口径）
    assert len({systems[block].split("YOUR TASK:")[0] for block in systems}) == 1, sorted(systems)


def test_the_batch_tool_returns_every_declared_input_at_once() -> None:
    """★ ①(b) 兜底：一次把全部声明输入取回来（"忘了取"的代价 = 一次调用）。"""
    from bobanana6.services.registry import build_registry

    payloads = {
        "part-a.csv": {"name": "part-a.csv", "rows": [{"x": 1}]},
        "part-b.csv": {"name": "part-b.csv", "missing": True},
    }

    class _Task:
        def upstream(self, name):  # type: ignore[no-untyped-def]
            return payloads[name]

    class _Tools:
        from bobanana6.core.ports import ToolSpec

        subtask = "T-09"
        allowed_inputs = ("part-a.csv", "part-b.csv")
        task = _Task()

        def specs(self):  # type: ignore[no-untyped-def]
            return (
                self.ToolSpec("task.upstream", ("name",), "schema+path", "inputs"),
                self.ToolSpec("task.upstream_all", (), "every declared input, by name", "inputs"),
            )

        def require_upstream(self, name):  # type: ignore[no-untyped-def]
            if name not in self.allowed_inputs:
                raise PermissionError(name)

    registry = build_registry(_Tools(), lambda snapshot: b"")  # type: ignore[arg-type]
    result = registry.execute((ToolCall(call_id="c1", name="task.upstream_all", arguments={}),))
    assert len(result) == 1 and result[0].ok, result
    body = json.loads(result[0].content)
    assert sorted(body) == ["part-a.csv", "part-b.csv"] and body["part-b.csv"]["missing"] is True


# --------------------------------------------------------------------------- #
# ③ 该不该拆：判据是契约的【声明】，不是来源数
# --------------------------------------------------------------------------- #


def test_the_split_decision_reads_the_declaration_not_the_source_count() -> None:
    """★ ③：1 源不拆 / 3 源声明了不拆 / **5 源声明了也不拆** —— 来源数不再是决定因素。

    反向（同一份 3 源题，只把声明去掉）必须【照旧扇出】：
    拦的是声明，不是"来源数"，也不是"这道题看起来像什么"。
    """
    from dataclasses import replace

    counter = ACCEPTANCE / "counter" / "state-machine-task"
    single = _plan_for(counter, task_file="task.yaml")[2]
    three = _plan_for(counter, task_file="party-task.yaml")[2]
    five = _plan_for(counter, task_file="five-party-task.yaml")[2]

    assert (single.decision, single.fallback, len(single.subtasks)) == ("single_agent", "single_agent", 1)
    for plan, sources in ((three, 3), (five, 5)):
        assert plan.decision == "refuse_split", (sources, plan.decision)
        assert plan.fallback == "single_agent"
        assert len(plan.subtasks) == 1 and plan.shape == "star"
        assert plan.refusal, "a refusal must say why"

    # ★ 反向：把声明拿掉，3 源照旧扇出（判据换掉了，不是把"切"这件事关掉了）
    document, ontology, _plan, _bundle = _plan_for(counter, task_file="party-task.yaml")
    no_declaration = replace(document.contract, sources_share_state=False)
    fanned = Decomposer(strategy="source").split(no_declaration, ontology, None)
    assert fanned.decision == "split" and len(fanned.subtasks) == 4 and fanned.shape == "pipeline"


def test_a_plan_that_fans_out_declared_shared_sources_is_caught() -> None:
    """★ ③：声明了共享状态、计划却仍然切成多块 → split 检查报 `shared_state`。"""
    from dataclasses import replace

    counter = ACCEPTANCE / "counter" / "state-machine-task"
    document, ontology, _plan, _bundle = _plan_for(counter, task_file="party-task.yaml")
    hand_made = Decomposer(strategy="source").split(
        replace(document.contract, sources_share_state=False), ontology, None
    )
    report = check_split(
        hand_made,
        replace(document.contract, sources_share_state=True),
        compile_bundle(document, hand_made),
        ontology,
        frozen_hash=ontology.content_hash(),
    )
    assert "shared_state" in report.codes(), report.codes()
    assert any("share one state" in violation.detail for violation in report.violations)


def test_hundreds_of_inputs_do_not_cost_the_worker_any_turns() -> None:
    """★ ① 换规模（3 → 240）：壳侧行为不变 —— 搬运不花模型的推理预算。

    小件：240 个全部放进 context（默认给全）。
    大件：超预算 → 退化成索引 + 一次 `task.upstream_all`（这才是"忘了取"的兜底形态）。
    两种都不需要模型为"取回"多花一轮。
    """
    from bobanana6.assemble_impl.produce import LoopIOImpl
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.model import Budget, ConstraintBundle, Subtask
    from bobanana6.services import BlockJudge

    class _Task:
        def __init__(self, parts):  # type: ignore[no-untyped-def]
            self.parts = parts

        def upstream(self, name):  # type: ignore[no-untyped-def]
            return self.parts[name]

    class _Tools:
        def __init__(self, parts):  # type: ignore[no-untyped-def]
            self.task = _Task(parts)

    def phase(names: list[str], rows: int) -> LoopIOImpl:
        parts = {
            name: {"name": name, "columns": [{"name": "x", "dtype": "str"}], "rows": [{"x": i} for i in range(rows)]}
            for name in names
        }
        block = Subtask(
            id="T-99", goal="g", inputs=tuple(names), output="out.csv", contract_ref="t", axis="source", mode="write"
        )
        return LoopIOImpl(
            client=None,  # type: ignore[arg-type]
            toolset=_Tools(parts),  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
            # ★ #37: 壳的投递路径要问判据（它点名了哪些参照物），所以这里给一个空包 ——
            #   真实的 LoopIOImpl 一定带 judge（`runtime.py` 只从 `block_bundle` 造）。
            judge=BlockJudge(bundle=ConstraintBundle(task_id="t", version=1), subtask=block, context=CheckContext()),
            subtask=block,
            budget=Budget(max_calls=1, max_wall_s=1, max_output=1),
        )

    small = phase([f"part-{index:03d}.csv" for index in range(240)], rows=2)
    body = small._delivered_inputs()
    assert len(body["delivered_inputs"]) == 240, "240 small inputs are delivered whole"

    big = phase([f"big-{index:03d}.csv" for index in range(240)], rows=200)
    body = big._delivered_inputs()
    assert len(body["delivered_inputs_index"]) == 240
    assert "task.upstream_all" in body["note"], body["note"]


def test_a_structurally_bad_answer_is_declared_as_the_models_failure() -> None:
    """★ `#65`：真跑里最常见的失败（答案不可用）必须有 blame —— 以前是 `None`。

    ★ 只声明【结构性】错误（非 JSON / 缺必需字段 / 行对不上）：这些在进程内可判；
      "语义答错"与"上下文不够"在进程内分不开（`#17` 已论证），所以不加启发式硬判。
    """
    from bobanana6.adapters import ScriptedClient

    client = ScriptedClient(
        [
            # ★ read-before-produce：先读来源，再交那份"不是 JSON"的答案。
            {"tool_calls": (_tool_call("r1", "sources.snapshot", {"ref": "src-1"}),)},
            {"text": "not json at all"},
        ]
    )
    run = run_task(ACCEPTANCE / "smoke" / "smoke-panel-one-source", client=client, env={}, expected_blame="model")
    assert run.report.attribution_accuracy == 1.0, run.report.notes
    assert run.report.block_pass_rate == 0.0
    # ★ 该块确实是被"答案读不出来"拦下的（不是别的失败）
    evidence = next(iter(run.verdicts.values())).evidence
    assert "CandidateFormatError" in str(evidence.get("errors")), evidence


# --------------------------------------------------------------------------- #
# #63 跨块（跨源）真冲突：按实体键对齐，三类分歧分开
# --------------------------------------------------------------------------- #


def _two_block_candidates(ontology, *, left_value: str, right_value: str, versions=()):
    """两份部件，都带 `obligor|revenue` 两列、引用各自的快照。"""
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.model import Artifact, Candidate, Column, Provenance

    def one(sid: str, ref: str, value: str, version: str | None) -> Candidate:
        artifact = Artifact(
            name=f"part-{ref}.csv",
            columns=(Column(name="obligor", dtype="str"), Column(name="revenue", dtype="str")),
            rows=({"obligor": "ENT-03", "revenue": value},),
        )
        return Candidate(
            subtask_id=sid,
            source_id=ref,
            artifacts=(artifact,),
            provenance=(Provenance(value_ref=f"part-{ref}.csv:row=0", snapshot_ref=ref, locator="input/" + ref),),
            definition_versions={} if version is None else {"revenue": version},
        )

    return [
        one("T-02", "s02", left_value, versions[0] if versions else None),
        one("T-20", "s20", right_value, versions[1] if versions else None),
    ], CheckContext(ontology=ontology)


def _revenue_ontology():
    from bobanana6.core.model import Concept, Definition, Ontology

    return Ontology(
        domain="d",
        version="1",
        concepts={
            "obligor": Concept(id="obligor", label="O", kind="entity", domain="d"),
            "revenue": Concept(id="revenue", label="R", kind="measure", domain="d"),
        },
        definitions={
            "revenue:v1": Definition(concept_id="revenue", version="v1", text="t"),
            "revenue:v4": Definition(concept_id="revenue", version="v4", text="t"),
        },
        units={},
    ).freeze()


def test_the_three_divergence_classes_stay_apart_across_blocks() -> None:
    """★ `#63`：装配关只拿 pieces，两个块的候选从来不会被放在一起 —— 这里对齐后分类。

    三条判据：同一个值（多源都这么说）→ 什么都不记；**同一口径不同值 → 记 Conflict
    且两侧证据都留**；口径版本不同 → 记 DefinitionMismatch，**绝不记成冲突**。
    """
    from bobanana6.core.kernel import compare_across

    ontology = _revenue_ontology()
    agree, context = _two_block_candidates(ontology, left_value="3010000", right_value="3010000")
    assert compare_across(agree, context).is_empty, "agreement is not a divergence"

    disagree, context = _two_block_candidates(ontology, left_value="3010000", right_value="9000000")
    result = compare_across(disagree, context)
    assert len(result.conflicts) == 1 and not result.definition_mismatches
    conflict = result.conflicts[0]
    assert conflict.concept_id == "revenue"
    assert [(source, value) for source, value, _ in conflict.claims] == [("s02", "3010000"), ("s20", "9000000")]

    versions_differ, context = _two_block_candidates(
        ontology, left_value="3010000", right_value="9000000", versions=("v1", "v4")
    )
    result = compare_across(versions_differ, context)
    assert not result.conflicts, "a definition mismatch must not be recorded as a conflict"
    assert [mismatch.versions for mismatch in result.definition_mismatches] == [("v1", "v4")]


def test_the_conflicts_report_names_the_cross_block_pair_and_not_a_definition_drift() -> None:
    """★ `#63` 的判据落点：CONFLICTS.md 里【出现那一对】（双方证据都留），
    而口径不一致【不许】混进冲突那一节。"""
    from bobanana6.core.kernel import compare_across
    from bobanana6.core.model import ConvergeDecision, Convergence
    from bobanana6.report import render_conflicts

    ontology = _revenue_ontology()
    disagree, context = _two_block_candidates(ontology, left_value="3010000", right_value="9000000")
    classification = compare_across(disagree, context)
    text = render_conflicts(
        _plan_for(ACCEPTANCE / "main" / "panel-three-sources")[3],
        classification,
        convergence=Convergence(decision=ConvergeDecision.RECORD_CONFLICT),
    )
    assert "`revenue`" in text and "s02='3010000'" in text and "s20='9000000'" in text, text
    assert "- 冲突 Conflict: 1" in text and "- 口径不一致 DefinitionMismatch: 0" in text


def test_an_explicit_ontology_pin_outranks_the_lexicographic_fallback(tmp_path: Path) -> None:
    """★ A02 后半：规范口径由【本体声明】，不靠版本号字典序猜；没有声明时要把兜底【报出来】。

    ★ 反向：题侧已改过名的压力题本体（`v1-...` 排在最前）读数不变。
    """
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.checkers.definition_pin import check as pin_check
    from bobanana6.core.model import Candidate, Constraint
    from bobanana6.core.ontology import pin_is_explicit, pinned_version
    from bobanana6.ontology import load_ontology_file

    base = (ACCEPTANCE / "stress" / "ontology.yaml").read_text(encoding="utf-8")
    declared = tmp_path / "declared.yaml"
    declared.write_text(base.replace("definitions:", "pins:\n  revenue: v4-excl-tax\ndefinitions:", 1), encoding="utf-8")

    pinned = load_ontology_file(declared).freeze()
    assert pinned_version("revenue", pinned) == "v4-excl-tax" and pin_is_explicit("revenue", pinned)

    fallback = load_ontology_file(ACCEPTANCE / "stress" / "ontology.yaml").freeze()
    assert pinned_version("revenue", fallback) == "v1-group-consolidated"
    assert pin_is_explicit("revenue", fallback) is False, "an undeclared pin must not look declared"

    candidate = Candidate(subtask_id="T-04", source_id="s02")
    on_pin = pin_check(
        Constraint(id="C-rev", kind="definition_pin", target="panel.csv", spec={"concept": "revenue", "version": "v4-excl-tax"}, rationale="r"),
        candidate,
        CheckContext(ontology=pinned),
    )
    assert on_pin.ok and any("declared by the ontology" in fact for fact in on_pin.facts), on_pin.facts

    on_fallback = pin_check(
        Constraint(id="C-rev", kind="definition_pin", target="panel.csv", spec={"concept": "revenue", "version": "v1-group-consolidated"}, rationale="r"),
        candidate,
        CheckContext(ontology=fallback),
    )
    assert on_fallback.ok, on_fallback.facts
    assert any("lexicographic fallback" in fact for fact in on_fallback.facts), on_fallback.facts


def test_rows_from_different_periods_are_never_paired() -> None:
    """★ `#63` 的**假冲突**防线：键 = 【不是量】的列（实体 + 期间）。

    只按实体对齐会在压力题上造出 15 条假冲突（`ENT-01|2021` 撞上 `ENT-01|2024`）——
    本块真跑实测到过，这条把根因钉住。
    """
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.kernel import compare_across
    from bobanana6.core.model import (
        Artifact,
        Candidate,
        Column,
        Concept,
        Ontology,
        Provenance,
    )

    ontology = Ontology(
        domain="d",
        version="1",
        concepts={
            "obligor": Concept(id="obligor", label="O", kind="entity", domain="d"),
            "revenue": Concept(id="revenue", label="R", kind="measure", domain="d"),
        },
        definitions={},
        units={},
    ).freeze()
    columns = (
        Column(name="obligor", dtype="str"),
        Column(name="year", dtype="str"),
        Column(name="revenue", dtype="str"),
    )

    def one(sid: str, ref: str, year: str, value: str) -> Candidate:
        artifact = Artifact(
            name=f"part-{ref}.csv",
            columns=columns,
            rows=({"obligor": "ENT-02", "year": year, "revenue": value},),
        )
        return Candidate(
            subtask_id=sid,
            source_id=ref,
            artifacts=(artifact,),
            provenance=(Provenance(value_ref=f"part-{ref}.csv:row=0", snapshot_ref=ref, locator="L"),),
        )

    context = CheckContext(ontology=ontology)
    assert compare_across([one("T-11", "s11", "2021", "2000000 元"), one("T-18", "s18", "2024", "40")], context).is_empty
    assert len(compare_across([one("T-02", "s02", "2022", "3010000"), one("T-20", "s20", "2022", "9000000")], context).conflicts) == 1


# --------------------------------------------------------------------------- #
# ★ `#43`: 继承守卫的观测是【开关】—— 不开就一个字节都不写（既有读数不受影响）
# --------------------------------------------------------------------------- #


def test_the_harness_can_turn_on_the_inheritance_guard_observation() -> None:
    """★ `#43`: 开了才有观测目录；不开就是 `None`（默认路径零开销、零文件）。"""
    task = ACCEPTANCE / "smoke" / "smoke-panel-one-source"

    off = run_task(task, client=EchoToolResultClient(), env={})
    assert off.observations_dir is None

    on = run_task(task, client=EchoToolResultClient(), env={}, observe=True)
    assert on.observations_dir is not None
    assert Path(on.observations_dir).is_dir()
    # 观测目录由验收层建；这一跑没有继承守卫的拒收，所以里面不该有文件
    assert list(Path(on.observations_dir).glob("*")) == []


# --------------------------------------------------------------------------- #
# ★ `#44`: 判据命令的**执行环境** —— 派活前具名拒；走到了也要归到 tool 而不是 constraint
# --------------------------------------------------------------------------- #


def _task_with_a_judge_command(tmp_path: Path, command: str, *, name: str) -> Path:
    """主验题的改造版：判据换成一条 shell 命令（`#44` 的预检 / 归因用）。"""
    import shutil

    import yaml

    source = ACCEPTANCE / "main" / "panel-three-sources"
    target = tmp_path / name / source.name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("_run", "__pycache__", "generate.py"))
    document = yaml.safe_load((target / "task.yaml").read_text(encoding="utf-8"))
    document["constraints"]["hard"].append(
        {
            "id": "C-judge",
            "kind": "custom",
            "target": "panel.csv",
            "spec": {"check": "shell_command", "command": command},
            "rationale": "面板要过这条命令",
        }
    )
    (target / "task.yaml").write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return target


class _CountingClient:
    """数一数模型被叫了几次（`#44` 的验收标准就是"0 次模型调用"）。"""

    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.calls = 0

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.inner.complete(messages, tools, max_tokens, **kw)  # type: ignore[attr-defined]


def test_a_judge_command_that_cannot_run_refuses_the_run_before_any_worker(tmp_path: Path) -> None:
    """★ `#44`：判据命令跑不起来 ⇒ 具名拒 `judge` + **0 次模型调用**（不再是跑完整轮）。

    （改前：`python3 -m pytest` 在没装 pytest 的解释器下必然拒，却记成"产出不合格"。）
    """
    task = _task_with_a_judge_command(
        tmp_path, "cd . && definitely-missing-interpreter-42 -m pytest -q", name="unrunnable"
    )
    client = _CountingClient(_ScriptedBlocks())
    run = run_task(task, client=client, env={})

    assert run.refused == "judge", run.refused
    assert run.refused_codes == ("C-judge:interpreter_missing",), run.refused_codes
    assert client.calls == 0, "判据跑不起来时一个模型都不该被叫"
    assert run.report.block_pass_rate == 0.0


def test_a_judge_command_that_breaks_at_run_time_blames_the_tool(tmp_path: Path) -> None:
    """★ `#44` 的兜底：预检看不进去的命令（`sh -c` 里面）在判据那一步失败 ⇒ 归 `tool`。

    改前它会记成 `blame=constraint` + 建议 `relax_constraint` —— 正确的动作是修工具。
    """
    task = _task_with_a_judge_command(
        tmp_path, 'sh -c "definitely-missing-binary-42 -q"', name="breaks-at-run-time"
    )
    run = run_task(task, client=_ScriptedBlocks(), env={}, expected_blame="tool")

    assert run.refused is None  # 预检放行（它看不进引号里面），判定那一步才失败
    assert run.report.attribution_accuracy == 1.0, run.report.notes
    assert run.report.block_pass_rate < 1.0


def test_a_fresh_task_whose_judge_cds_into_a_runtime_workspace_is_not_refused(tmp_path: Path) -> None:
    """★ `#46`：判据命令 `cd` 到**壳在运行期才创建**的工作区 ⇒ 门必须放行。

    改前这里是 `RunRefused("judge", …("C-judge:cwd_missing",))` + 0 次模型调用 —— 也就是说
    这道题在"从没跑过"的状态下永远起不来，而**历史上跑过、`_run` 残留**的同类题却能过
    （门在给运气放行）。
    """
    task = _task_with_a_judge_command(
        tmp_path, 'cd _run/workspace && python3 -c "print(1)"', name="fresh-runtime-workspace"
    )
    assert not (task / "_run").exists()  # 前提：新题，没有运行期目录

    client = _CountingClient(_ScriptedBlocks())
    run = run_task(task, client=client, env={})

    assert run.refused is None, (run.refused, run.refused_codes)
    assert client.calls > 0, "门放行之后应该真的派活"


def test_a_judge_command_referencing_a_missing_absolute_path_is_still_refused(tmp_path: Path) -> None:
    """★ `#46` 的另一半：作者**事先写下**的绝对路径不存在 ⇒ 照样具名拒 + 0 次模型调用。

    （别为了修上面那条，把"运行期不会改变的东西"也一起放松了。）
    """
    missing = tmp_path / "no-such-absolute-dir"
    task = _task_with_a_judge_command(
        tmp_path, f'cd {missing} && python3 -c "print(1)"', name="missing-absolute-dir"
    )
    client = _CountingClient(_ScriptedBlocks())
    run = run_task(task, client=client, env={})

    assert run.refused == "judge", run.refused
    assert run.refused_codes == ("C-judge:cwd_missing",), run.refused_codes
    assert client.calls == 0, "判据命令跑不起来时一个模型都不该被叫"


# --------------------------------------------------------------------------- #
# ★ `#47`: 通道 2 —— 块直接写工作区，判定器与装配关必须仍然看得见
# --------------------------------------------------------------------------- #


def _workspace_task(tmp_path: Path, *, name: str, target: str = "out.txt") -> Path:
    """一道最小的代码类题：一份草稿来源 + 一条判据命令 + 一个交付物。

    ★ 刻意**不**依赖任何未入库的 `acceptance/ab/` fixture：这类题的形状（写工作区 +
    判据读工作区）必须能用仓库里已有的东西复现，否则它就只是一个本地读数。
    """
    import shutil

    task = tmp_path / name
    (task / "input").mkdir(parents=True)
    (task / "input" / "seed.txt").write_text("draft\n", encoding="utf-8")
    (task / "ontology.yaml").write_text(
        "domain: text-fix\nversion: \"2026.1\"\nconcepts:\n  text: {label: 文本, kind: attribute}\n"
        "calendar: {timezone: Asia/Shanghai}\n",
        encoding="utf-8",
    )
    (task / "task.yaml").write_text(
        "task_id: ws-task\ngoal: write the deliverable\nversion: 1\n"
        f"deliverables:\n  - name: {target}\n    schema: {{text: str}}\n"
        "sources:\n  - {ref: seed, locator: input/seed.txt}\n"
        "constraints:\n  hard:\n"
        f"    - id: C-judge\n      kind: custom\n      target: {target}\n"
        "      spec:\n        check: shell_command\n"
        f"        command: \"cd _run/workspace && grep -q EXPECTED {target}\"\n"
        "      rationale: \"产物必须真的写出来并含 EXPECTED\"\n"
        "limits: {max_rounds: 3}\n",
        encoding="utf-8",
    )
    shutil.rmtree(tmp_path / name / "_run", ignore_errors=True)
    return task


class _ChannelTwoClient:
    """一条会走通道 2 的模型：先 `files.write`，再交 JSON 答案。

    ``path=None`` 时走通道 1（把内容放进答案的 `files` 字段），这是同一个问题的旧走法。
    """

    def __init__(self, *, path: str | None = "out.txt", content: str = "EXPECTED\n", tool: str = "files.write") -> None:
        self.path, self.content, self.tool = path, content, tool
        self.calls = 0

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        self.calls += 1
        brief = _briefing(messages)
        output = str(brief.get("subtask", {}).get("output", ""))
        # ★ read-before-produce：这一块只有来源一条取数路，必须先读过来源才准产出。
        #   分三步走 —— 读来源 → 写交付物 → 交答案（原来只有后两步）。
        answered = sum(1 for message in messages if message.get("role") == "tool")
        if answered == 0:
            return ModelResponse(
                tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),)
            )
        if self.path is not None and answered == 1:
            return ModelResponse(
                tool_calls=(
                    _tool_call("w1", self.tool, {"path": self.path, "content": self.content}),
                )
            )
        # ★ 这是一份最终答案：不写 done，循环按新语义不终结。
        body: dict[str, object] = {"ref": "seed", "columns": [], "rows": []}
        if self.path is None:
            body["files"] = {output: self.content}
        return ModelResponse(text=json.dumps(body))


def test_a_block_that_writes_its_deliverable_directly_is_judged_and_assembled(tmp_path: Path) -> None:
    """★ `#47`：通道 2 写出来的交付物必须**仍然被判定器看见**。

    三个要点一起钉住：
      · 判据命令（读工作区）通过 ⇒ 文件真的落到了判据看得见的地方；
      · 候选的**判定内容取自磁盘**（`artifact.text` == 文件原文，不是答案里那句话）；
      · 文件级溯源（`out.txt:file`）进了候选 ⇒ 装配关与报告看得见它。
    """
    task = _workspace_task(tmp_path, name="channel-two")
    client = _ChannelTwoClient()
    run = run_task(task, client=client, env={}, workspace=True)

    assert run.refused is None
    assert run.report.block_pass_rate == 1.0, run.verdicts
    assert client.calls > 1, "应该先写文件、再交答案"
    candidate = next(iter(run.candidates.values()))
    assert "out.txt" in candidate.files
    assert candidate.artifact("out.txt") is not None
    assert candidate.artifact("out.txt").text == "EXPECTED\n", "判定内容必须取自磁盘"
    assert any(point.value_ref == "out.txt:file" for point in candidate.provenance), candidate.provenance
    assert (run.workspace / "out.txt").read_text(encoding="utf-8") == "EXPECTED\n"


def test_the_same_deliverable_through_the_answer_json_still_works(tmp_path: Path) -> None:
    """★ 通道 1（答案 JSON 的 `files`）一字未改 —— 同一个题、同一份内容，同样通过。"""
    task = _workspace_task(tmp_path, name="channel-one")
    run = run_task(task, client=_ChannelTwoClient(path=None), env={}, workspace=True)

    assert run.report.block_pass_rate == 1.0, run.verdicts
    candidate = next(iter(run.candidates.values()))
    assert "out.txt" in candidate.files
    assert (run.workspace / "out.txt").read_text(encoding="utf-8") == "EXPECTED\n"


def test_a_channel_two_write_into_a_frozen_path_never_lands(tmp_path: Path) -> None:
    """★ 通道 2 **不是后门**（其一）：`files.write` 写到【frozen 路径】（声明的来源）⇒ 具名拒。

    旧靶子是"写区之外的草稿纸"—— 那条旧规则已退役（草稿纸现在合法）。职责没变：
    **撞到别人的东西（别的交付物 / 任务声明的来源）必须拒**，所以靶子换成 frozen 里的
    `input/seed.txt`，断言强度不动：拒绝 + 那份来源一个字都不许变。
    """
    task = _workspace_task(tmp_path, name="frozen-target-region")
    run = run_task(task, client=_ChannelTwoClient(path="input/seed.txt"), env={}, workspace=True)

    assert run.report.block_pass_rate < 1.0, run.verdicts
    # ★ 声明的来源是 frozen：写它必须被拒，盘上那份一个字都不许变
    assert (run.workspace / "input" / "seed.txt").read_text(encoding="utf-8") == "draft\n"
    assert not (run.workspace / "out.txt").exists(), "交付物根本没被写出来"


class _ShellWritingClient(_ChannelTwoClient):
    """走 `shell.run` 落一个文件到【frozen 路径】—— 工具看不出来，只有块结束时的对账能抓住。"""

    def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
        self.calls += 1
        brief = _briefing(messages)
        output = str(brief.get("subtask", {}).get("output", ""))
        # ★ read-before-produce：先读来源，再执行 shell，最后交答案。
        answered = sum(1 for message in messages if message.get("role") == "tool")
        if answered == 0:
            return ModelResponse(
                tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),)
            )
        if answered == 1:
            return ModelResponse(
                tool_calls=(
                    # ★ 靶子是 frozen：写任务【声明的来源】，不是写一块草稿纸。
                    _tool_call("w1", "shell.run", {"command": "echo stray > input/seed.txt"}),
                )
            )
        return ModelResponse(text=json.dumps({"ref": "seed", "columns": [], "rows": [], "files": {output: "EXPECTED\n"}}))


def test_a_shell_command_that_writes_into_a_frozen_path_is_caught_at_block_end(tmp_path: Path) -> None:
    """★ 通道 2 **不是后门**（其二）：`shell.run` 的副作用壳看不到参数 —— 由块结束时的对账兜住。

    旧靶子是"写区之外的草稿纸"（已退役）；职责不变：**撞 frozen 必须在块末被抓**。
    这就是"只登记 `files.write`"会留的那个洞：能执行代码之后，写文件的路不止一条。
    """
    task = _workspace_task(tmp_path, name="shell-frozen-target")
    run = run_task(task, client=_ShellWritingClient(), env={}, workspace=True)

    assert run.report.block_pass_rate < 1.0, run.verdicts
    errors = " ".join(str((verdict.evidence or {}).get("errors", "")) for verdict in run.verdicts.values())
    assert "outside its write region" in errors, errors
