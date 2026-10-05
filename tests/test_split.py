"""Split legality: one fixture per check, plus negatives that look like
violations but are not."""

from __future__ import annotations

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

from bobanana6.core.model import PlanCosts
from bobanana6.core.split import CHECKS, STRUCTURAL_CHECKS, check_split
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
    assert len(CHECKS) == 11
    assert CHECKS[0] == "star"


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
