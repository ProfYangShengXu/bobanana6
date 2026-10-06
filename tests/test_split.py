"""Split legality: one fixture per check, plus negatives that look like
violations but are not."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

ACCEPTANCE = Path(__file__).resolve().parents[1] / "acceptance"

from fakes import (
    bundle,
    constraint,
    contract,
    mini_ontology,
    plan,
    subtask,
    unfrozen_ontology,
)

from bobanana6.core.model import DeliverableSpec, PlanCosts, TaskContract
from bobanana6.core.split import CHECKS, STRUCTURAL_CHECKS, check_split, subtask_layers
from bobanana6.ontology import load_ontology_file


def good_plan(**overrides):
    item = subtask("T-01", output="out.csv", output_fields=("pd_3y",))
    values = {
        "subtasks": (item,),
        "interfaces": {"out.csv": "schema:pd"},
        "costs": PlanCosts(merge=1.0, execute=10.0),
    }
    values.update(overrides)
    return plan(**values)


def good_bundle(**overrides):
    values = {
        "hard": (constraint("C-1", "presence", "T-01", {"min_rows": 1}),),
    }
    values.update(overrides)
    return bundle(**values)


def report(item=None, contract_=None, item_bundle=None, ontology=None, **kwargs):
    return check_split(
        item if item is not None else good_plan(),
        contract_ if contract_ is not None else contract(),
        item_bundle if item_bundle is not None else good_bundle(),
        ontology if ontology is not None else mini_ontology(),
        **kwargs,
    )


def test_check_list_matches_the_spec() -> None:
    # ★ 层次切分把 11 条扩到 13 条（layer_lateral / layer_wiring），规格同步改过。
    assert len(CHECKS) == 13
    assert CHECKS[0] == "star"


def test_the_layered_checks_are_named_in_the_spec_list() -> None:
    assert "layer_lateral" in CHECKS
    assert "layer_wiring" in CHECKS


def test_clean_plan_passes_every_check() -> None:
    result = report()
    assert result.ok, result.codes()


def test_empty_plan_is_rejected() -> None:
    assert "empty_plan" in report(item=good_plan(subtasks=(), interfaces={})).codes()


def test_duplicate_ids_are_rejected() -> None:
    a = subtask("T-01", output="a.csv")
    b = subtask("T-01", output="b.csv")
    codes = report(item=good_plan(subtasks=(a, b), interfaces={"a.csv": "s", "b.csv": "s"})).codes()
    assert "duplicate_id" in codes


def test_duplicate_outputs_are_rejected() -> None:
    a = subtask("T-01", output="out.csv")
    b = subtask("T-02", output="out.csv")
    codes = report(item=good_plan(subtasks=(a, b), interfaces={"out.csv": "s"})).codes()
    assert "duplicate_output" in codes


def test_star_shape_rejects_block_to_block_input() -> None:
    producer = subtask("T-01", output="mid.csv")
    consumer = subtask("T-02", output="out.csv", inputs=("mid.csv",))
    item = good_plan(subtasks=(producer, consumer), interfaces={"mid.csv": "s", "out.csv": "s"})
    assert "star" in report(item=item).codes()


def test_non_star_shape_allows_a_dependency_chain() -> None:
    producer = subtask("T-01", output="mid.csv")
    consumer = subtask("T-02", output="out.csv", inputs=("mid.csv",))
    item = good_plan(subtasks=(producer, consumer), shape="pipeline", interfaces={"mid.csv": "s", "out.csv": "s"})
    codes = report(item=item, contract_=contract(("out.csv",))).codes()
    assert "star" not in codes


def test_iface_complete_rejects_undeclared_output() -> None:
    assert "iface_complete" in report(item=good_plan(interfaces={})).codes()


def test_iface_match_rejects_schema_disagreement() -> None:
    item = subtask("T-01", output="out.csv", inputs=("up.csv",), expects=(("up.csv", "schema:new"),), output_fields=("pd_3y",))
    item_plan = good_plan(subtasks=(item,), interfaces={"out.csv": "s", "up.csv": "schema:old"})
    assert "iface_match" in report(item=item_plan, contract_=contract(("out.csv", "up.csv"))).codes()


def test_iface_match_accepts_matching_schema() -> None:
    item = subtask("T-01", output="out.csv", inputs=("up.csv",), expects=(("up.csv", "schema:pd"),), output_fields=("pd_3y",))
    item_plan = good_plan(subtasks=(item,), interfaces={"out.csv": "s", "up.csv": "schema:pd"})
    codes = report(item=item_plan, contract_=contract(("out.csv",))).codes()
    assert "iface_match" not in codes


def test_verifiable_rejects_a_block_with_no_constraint() -> None:
    assert "verifiable" in report(item_bundle=good_bundle(hard=())).codes()


def test_verifiable_accepts_a_constraint_targeting_the_output_name() -> None:
    assert "verifiable" not in report(item_bundle=good_bundle(hard=(constraint("C-1", "presence", "out.csv", {"min_rows": 1}),))).codes()


def test_axis_uniform_rejects_mixed_axes_in_one_layer() -> None:
    a = subtask("T-01", output="a.csv", axis="source")
    b = subtask("T-02", output="b.csv", axis="window")
    item = good_plan(subtasks=(a, b), interfaces={"a.csv": "s", "b.csv": "s"})
    item_plan = item
    codes = report(item=item_plan, contract_=contract(("a.csv", "b.csv")), item_bundle=good_bundle(hard=(constraint("C-1", "presence", "T-01", {}), constraint("C-2", "presence", "T-02", {})))).codes()
    assert "axis_uniform" in codes


def test_axis_uniform_accepts_same_axis_in_one_layer() -> None:
    a = subtask("T-01", output="a.csv", axis="source")
    b = subtask("T-02", output="b.csv", axis="source")
    codes = report(
        item=good_plan(subtasks=(a, b), interfaces={"a.csv": "s", "b.csv": "s"}),
        contract_=contract(("a.csv", "b.csv")),
        item_bundle=good_bundle(hard=(constraint("C-1", "presence", "T-01", {}), constraint("C-2", "presence", "T-02", {}))),
    ).codes()
    assert "axis_uniform" not in codes


def test_no_orphan_rejects_an_output_nobody_uses() -> None:
    item = subtask("T-01", output="orphan.csv", output_fields=("pd_3y",))
    item_plan = good_plan(subtasks=(item,), interfaces={"orphan.csv": "s"})
    assert "no_orphan" in report(item=item_plan).codes()


def test_write_serial_rejects_unpartitioned_parallel_writes() -> None:
    a = subtask("T-01", output="a.csv", mode="write", output_fields=("pd_3y",))
    b = subtask("T-02", output="b.csv", mode="write", output_fields=("pd_3y",))
    codes = report(
        item=good_plan(subtasks=(a, b), interfaces={"a.csv": "s", "b.csv": "s"}),
        contract_=contract(("a.csv", "b.csv")),
        item_bundle=good_bundle(hard=(constraint("C-1", "presence", "T-01", {}), constraint("C-2", "presence", "T-02", {}))),
    ).codes()
    assert "write_serial" in codes


def test_write_serial_accepts_partitioned_parallel_writes() -> None:
    a = subtask("T-01", output="a.csv", mode="write", partition="p1", output_fields=("pd_3y",))
    b = subtask("T-02", output="b.csv", mode="write", partition="p2", output_fields=("pd_3y",))
    codes = report(
        item=good_plan(subtasks=(a, b), interfaces={"a.csv": "s", "b.csv": "s"}),
        contract_=contract(("a.csv", "b.csv")),
        item_bundle=good_bundle(hard=(constraint("C-1", "presence", "T-01", {}), constraint("C-2", "presence", "T-02", {}))),
    ).codes()
    assert "write_serial" not in codes


def test_write_serial_rejects_shared_partitions() -> None:
    a = subtask("T-01", output="a.csv", mode="write", partition="same", output_fields=("pd_3y",))
    b = subtask("T-02", output="b.csv", mode="write", partition="same", output_fields=("pd_3y",))
    codes = report(
        item=good_plan(subtasks=(a, b), interfaces={"a.csv": "s", "b.csv": "s"}),
        contract_=contract(("a.csv", "b.csv")),
        item_bundle=good_bundle(hard=(constraint("C-1", "presence", "T-01", {}), constraint("C-2", "presence", "T-02", {}))),
    ).codes()
    assert "write_serial" in codes


def test_merge_cheap_rejects_expensive_merges() -> None:
    assert "merge_cheap" in report(item=good_plan(costs=PlanCosts(merge=9.0, execute=10.0))).codes()


def test_merge_cheap_fails_closed_when_costs_are_undeclared() -> None:
    assert "merge_cheap" in report(item=good_plan(costs=None)).codes()


def test_concept_mapped_rejects_unmapped_output_field() -> None:
    item = subtask("T-01", output="out.csv", output_fields=("mystery",))
    item_plan = good_plan(subtasks=(item,), interfaces={"out.csv": "s"})
    assert "concept_mapped" in report(item=item_plan).codes()


def test_concept_mapped_accepts_declared_unmapped_field() -> None:
    item = subtask("T-01", output="out.csv", output_fields=("mystery",))
    item_plan = good_plan(subtasks=(item,), interfaces={"out.csv": "s"})
    codes = report(item=item_plan, item_bundle=good_bundle(unmapped=(("mystery", "no concept yet"),))).codes()
    assert "concept_mapped" not in codes


def test_round_budget_rejects_the_fourth_round() -> None:
    assert "round_budget" in report(item=good_plan(round=4)).codes()
    assert "round_budget" not in report(item=good_plan(round=3)).codes()


def test_semantics_frozen_rejects_unfrozen_ontology() -> None:
    assert "semantics_frozen" in report(ontology=unfrozen_ontology()).codes()


def test_semantics_frozen_rejects_a_mismatched_hash() -> None:
    assert "semantics_frozen" in report(frozen_hash="deadbeef").codes()


def test_semantics_frozen_accepts_the_matching_hash() -> None:
    ontology = mini_ontology()
    assert "semantics_frozen" not in report(ontology=ontology, frozen_hash=ontology.content_hash()).codes()


def test_structural_checks_are_named_separately_from_the_spec_list() -> None:
    assert "empty_plan" in STRUCTURAL_CHECKS
    assert "empty_plan" not in CHECKS


def test_iface_complete_rejects_input_with_no_interface() -> None:
    item = subtask("T-01", output="out.csv", inputs=("ghost.csv",), output_fields=("pd_3y",))
    item_plan = good_plan(subtasks=(item,), interfaces={"out.csv": "s"})
    assert "iface_complete" in report(item=item_plan).codes()


# --------------------------------------------------------------------------- #
# ★ `#33`: split_by_module —— 切口落在模块边界上
# --------------------------------------------------------------------------- #

MODULE_TASK = """
task_id: code-convergence
goal: converge duplicate implementations into one shared abstraction
version: 1
deliverables:
  - {name: input/repo/shared.py, schema: {text: str}}
  - {name: interface.json, schema: {text: str}}
  - {name: input/repo/shop_a.py, schema: {text: str}}
  - {name: input/repo/shop_b.py, schema: {text: str}}
sources:
  - {ref: a-source, locator: input/repo/shop_a.py, module: shop_a}
  - {ref: b-source, locator: input/repo/shop_b.py, module: shop_b}
  - {ref: the-interface, locator: input/repo/interface.py}
  - {ref: test-a, locator: input/repo/test_shop_a.py}
constraints:
  hard:
    - {id: C-1, kind: presence, target: input/repo/shared.py, spec: {min_rows: 1}, rationale: "x"}
    - {id: C-2, kind: presence, target: interface.json, spec: {min_rows: 1}, rationale: "x"}
    - {id: C-3, kind: presence, target: input/repo/shop_a.py, spec: {min_rows: 1}, rationale: "x"}
    - {id: C-4, kind: presence, target: input/repo/shop_b.py, spec: {min_rows: 1}, rationale: "x"}
limits: {max_rounds: 2}
"""


def _module_plan(text: str = MODULE_TASK, strategy: str = "module"):
    from bobanana6.contract import compile_bundle, parse_contract
    from bobanana6.core.model import Recon
    from bobanana6.decompose import Decomposer

    document = parse_contract(text)
    # ★ 用真题的 ontology（`text` 概念在这里）——fakes 的迷你本体没有它。
    ontology = load_ontology_file(ACCEPTANCE / "stress" / "code-convergence" / "ontology.yaml").freeze()
    plan = Decomposer(strategy=strategy).split(
        document.contract, ontology, Recon(source_ids=tuple(s.ref for s in document.contract.sources))
    )
    bundle = compile_bundle(document, plan)
    return plan, check_split(plan, document.contract, bundle, ontology, frozen_hash=ontology.content_hash())


def test_the_module_strategy_cuts_on_module_boundaries() -> None:
    """★ 一个模块一块（写区互斥），共享抽象与接口在最前面 —— 全部 11 条检查要过。"""
    plan, report = _module_plan()
    assert report.ok, report.codes()
    assert plan.shape == "pipeline"
    by_id = {block.id: block for block in plan.subtasks}
    # 接口块 + 共享抽象块 + 两个模块块
    assert plan.subtasks[0].output == "interface.json"
    assert plan.subtasks[1].output == "input/repo/shared.py"
    modules = [block for block in plan.subtasks if block.partition and block.partition.startswith("module:")]
    assert {block.partition for block in modules} == {"module:shop_a", "module:shop_b"}
    # 模块块把共享抽象声明成输入（"接口先冻结、再实现"）
    assert all("input/repo/shared.py" in block.inputs for block in modules)
    assert by_id["T-01"].partition == "deliverable:interface.json"


def test_a_single_module_is_not_fanned_out() -> None:
    """★ 反向之一：只有一个模块 = 切口不存在 ⇒ 不按模块扇出，回落到 split_single。

    （`split_single` 的约定是"每份交付物一个写块" —— 所以块数不等于 1 也没关系，
    要看的是**没有任何 `module:` 分区**：切口没有被按模块切开。）
    """
    plan, _report = _module_plan(MODULE_TASK.replace("  - {ref: b-source, locator: input/repo/shop_b.py, module: shop_b}\n", ""))
    partitions = {block.partition for block in plan.subtasks}
    assert not any(str(part).startswith("module:") for part in partitions), partitions
    assert plan.refusal == ""


def test_a_declared_shared_state_refuses_the_module_cut() -> None:
    """★ 反向之二（§15.5 的反例形状）：来源共享同一份状态 ⇒ 拒绝按模块扇出。"""
    plan, _report = _module_plan(MODULE_TASK.replace("version: 1", "version: 1\nsources_share_state: true"))
    assert plan.decision == "refuse_split"
    assert "share one state" in plan.refusal
    assert not any(str(block.partition).startswith("module:") for block in plan.subtasks)


def test_the_module_strategy_needs_two_deliverables_for_the_interface_block() -> None:
    """只有一份交付物时退化成两层（共享抽象 + 模块），不多造一个接口块。"""
    text = MODULE_TASK.replace("  - {name: interface.json, schema: {text: str}}\n", "").replace(
        '    - {id: C-2, kind: presence, target: interface.json, spec: {min_rows: 1}, rationale: "x"}\n', ""
    )
    plan, report = _module_plan(text)
    assert report.ok, report.codes()
    assert plan.subtasks[0].output == "input/repo/shared.py"
    assert not any(block.output == "interface.json" for block in plan.subtasks)

# --------------------------------------------------------------------------- #
# ★ 层次切分：layered 的两条新检查（layer_lateral / layer_wiring）
# --------------------------------------------------------------------------- #


def _layered_contract(*, d_deps=("b.csv", "c.csv")):
    """a leaf; b/c siblings from a; d joins b+c. `depends_on` is the only input."""
    return TaskContract(
        task_id="task-1",
        goal="layered",
        deliverables=(
            DeliverableSpec(name="a.csv"),
            DeliverableSpec(name="b.csv", depends_on=("a.csv",)),
            DeliverableSpec(name="c.csv", depends_on=("a.csv",)),
            DeliverableSpec(name="d.csv", depends_on=tuple(d_deps)),
        ),
    )


def _layered_plan(contract_=None):
    item_contract = contract_ if contract_ is not None else _layered_contract()
    blocks = (
        subtask("T-01", output="a.csv", mode="write", partition="deliverable:a", output_fields=("pd_3y",)),
        subtask("T-02", output="b.csv", inputs=("a.csv",), mode="write", partition="deliverable:b", output_fields=("pd_3y",)),
        subtask("T-03", output="c.csv", inputs=("a.csv",), mode="write", partition="deliverable:c", output_fields=("pd_3y",)),
        subtask("T-04", output="d.csv", inputs=("b.csv", "c.csv"), mode="write", partition="deliverable:d", output_fields=("pd_3y",)),
    )
    interfaces = {name: "schema:pd" for name in ("a.csv", "b.csv", "c.csv", "d.csv")}
    item_bundle = bundle(hard=tuple(constraint(f"C-{i}", "presence", name, {"min_rows": 1}) for i, name in enumerate(("a.csv", "b.csv", "c.csv", "d.csv"))))
    return plan(subtasks=blocks, shape="layered", interfaces=interfaces, costs=PlanCosts(merge=1.0, execute=10.0)), item_contract, item_bundle


def _replace_subtask(item, sid, **changes):
    return replace(item, subtasks=tuple(replace(block, **changes) if block.id == sid else block for block in item.subtasks))


def test_layered_plan_passes_and_layers_by_dependency_depth() -> None:
    item, item_contract, item_bundle = _layered_plan()
    result = check_split(item, item_contract, item_bundle, mini_ontology(), frozen_hash=mini_ontology().content_hash())
    assert result.ok, result.codes()
    depth, cycle = subtask_layers(item)
    assert cycle == ()
    assert [depth[sid] for sid in ("T-01", "T-02", "T-03", "T-04")] == [0, 1, 1, 2]


def test_layer_lateral_rejects_a_same_layer_dependency() -> None:
    """① 同层不许横向依赖：契约里 d 与 a 同在第 0 层，计划却让 d 吃 a 的产物。"""
    item, item_contract, item_bundle = _layered_plan(_layered_contract(d_deps=()))
    broken = _replace_subtask(item, "T-04", inputs=("a.csv",))
    codes = check_split(broken, item_contract, item_bundle, mini_ontology()).codes()
    assert "layer_lateral" in codes


def test_layer_wiring_rejects_declaring_more_than_the_contract() -> None:
    """③ 接口数纪律：把"上层的全部产物"都当输入（§4.3 的 N(N-1)/2 灾难）要红。"""
    item, item_contract, item_bundle = _layered_plan()
    broken = _replace_subtask(item, "T-04", inputs=("a.csv", "b.csv", "c.csv"))
    result = check_split(broken, item_contract, item_bundle, mini_ontology())
    assert "layer_wiring" in result.codes()
    assert any("contract declares" in v.detail for v in result.violations if v.check == "layer_wiring")


def test_layer_wiring_rejects_a_self_dependency() -> None:
    item, item_contract, item_bundle = _layered_plan()
    broken = _replace_subtask(item, "T-02", inputs=("b.csv",))
    codes = check_split(broken, item_contract, item_bundle, mini_ontology()).codes()
    assert "layer_wiring" in codes


def test_layered_plan_still_rejects_a_cycle() -> None:
    """② 跨层无环：b↔c 互依赖，`cycle` 必须在分层之前就报出来。"""
    item, item_contract, item_bundle = _layered_plan()
    broken = _replace_subtask(_replace_subtask(item, "T-02", inputs=("c.csv",)), "T-03", inputs=("b.csv",))
    codes = check_split(broken, item_contract, item_bundle, mini_ontology()).codes()
    assert "cycle" in codes


def test_a_read_block_on_the_chain_does_not_confuse_the_layers() -> None:
    """③ 换块类型：链上混入 read 模式，层号仍按依赖深度算，检查不误伤。"""
    item, item_contract, item_bundle = _layered_plan()
    reader = subtask("T-00", output="part-a.csv", mode="read", output_fields=("pd_3y",))
    wired = tuple(
        replace(block, inputs=("part-a.csv",)) if block.id == "T-01" else block
        for block in item.subtasks
    )
    item = plan(
        subtasks=(reader, *wired),
        shape="layered",
        interfaces={**dict(item.interfaces), "part-a.csv": "schema:pd"},
        costs=PlanCosts(merge=1.0, execute=10.0),
    )
    depth, _ = subtask_layers(item)
    assert depth["T-00"] == 0 and depth["T-01"] == 1 and depth["T-04"] == 3
    codes = check_split(item, item_contract, item_bundle, mini_ontology()).codes()
    assert "layer_lateral" not in codes


def test_layer_wiring_rejects_a_consumer_listed_before_its_producer() -> None:
    """顺序也是接线：runner 按 subtasks 顺序跑，上游排在下游后面 ⇒ 计划不可执行。"""
    item, item_contract, item_bundle = _layered_plan()
    swapped = replace(item, subtasks=(item.subtasks[1], item.subtasks[0], *item.subtasks[2:]))
    result = check_split(swapped, item_contract, item_bundle, mini_ontology())
    assert "layer_wiring" in result.codes()
    assert any("listed before its upstream" in v.detail for v in result.violations if v.check == "layer_wiring")
