"""decompose/: round budget + strategies that produce a plan core/split.py accepts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bobanana6.contract import compile_bundle, parse_contract
from bobanana6.core.model import AssemblyOutcome, Recon
from bobanana6.core.split import check_split, subtask_layers
from bobanana6.decompose import (
    Decomposer,
    RoundBudget,
    RoundBudgetExceeded,
    can_refine,
    estimate_costs,
    split_by_field,
    split_by_source,
    split_single,
)
from bobanana6.ontology import load_ontology

ONTOLOGY = """
domain: credit-risk
version: "2026.1"
concepts:
  pd: {label: PD, kind: measure, aliases: [pd_3y]}
  amount: {label: Amount, kind: measure}
units:
  amount: {symbol: CNY, scale: 1.0, base: CNY}
"""

CONTRACT = """
task_id: task-1
goal: build the credit panel
deliverables:
  - {name: out.csv, schema: {pd: float, amount: float}}
sources:
  - {ref: src-1}
  - {ref: src-2}
  - {ref: src-3}
constraints:
  hard:
    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "the panel must not be empty"}
"""


def ontology():
    return load_ontology(ONTOLOGY).freeze()


def contract():
    return parse_contract(CONTRACT).contract


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #


def test_round_budget_validation_and_ceiling() -> None:
    with pytest.raises(ValueError):
        RoundBudget(max_rounds=-1)
    budget = RoundBudget(max_rounds=3)
    assert budget.allows(3) and budget.allows(1)
    assert not budget.allows(4)
    assert budget.exhausted(3) and not budget.exhausted(2)
    assert budget.next_round(1) == 2
    with pytest.raises(RoundBudgetExceeded):
        budget.next_round(3)


def test_can_refine_uses_the_ceiling() -> None:
    from bobanana6.decompose import split_single as build

    plan = build(contract(), ontology())
    assert can_refine(plan, RoundBudget(max_rounds=3)) is True
    assert can_refine(plan, RoundBudget(max_rounds=1)) is False


def test_cost_estimate_keeps_merge_cheap() -> None:
    for blocks in range(1, 12):
        costs = estimate_costs(blocks)
        assert costs.execute > 0
        assert costs.merge / costs.execute < 0.5


# --------------------------------------------------------------------------- #
# strategies
# --------------------------------------------------------------------------- #


def test_source_strategy_reads_each_source_then_merges() -> None:
    plan = split_by_source(contract(), ontology())
    reads = [block for block in plan.subtasks if block.mode == "read"]
    writes = [block for block in plan.subtasks if block.mode == "write"]
    assert len(reads) == 3 and len(writes) == 1
    assert {block.axis for block in plan.subtasks} == {"source"}
    assert plan.shape == "pipeline"
    assert plan.interfaces["out.csv"] == "schema:amount|pd"
    # every consumer expectation matches the declared interface
    merge = writes[0]
    assert dict(merge.expects) == {name: plan.interfaces[name] for name in merge.inputs}
    # same-layer writers are partition-isolated
    assert len({block.partition for block in writes}) == len(writes)


def test_source_strategy_falls_back_to_single_for_one_source() -> None:
    one = parse_contract(CONTRACT.replace("  - {ref: src-2}\n", "").replace("  - {ref: src-3}\n", "")).contract
    plan = split_by_source(one, ontology())
    assert plan.shape == "star"
    assert len(plan.subtasks) == 1


def test_field_strategy_splits_per_field() -> None:
    plan = split_by_field(contract(), ontology())
    reads = [block for block in plan.subtasks if block.mode == "read"]
    assert [block.output for block in reads] == ["part-amount.csv", "part-pd.csv"]
    assert {block.axis for block in plan.subtasks} == {"field"}
    assert all(block.output_fields for block in reads)


def test_single_strategy_produces_one_write_per_deliverable() -> None:
    text = CONTRACT.replace(
        "  - {name: out.csv, schema: {pd: float, amount: float}}",
        "  - {name: out.csv, schema: {pd: float, amount: float}}\n  - {name: other.csv, schema: {pd: float}}",
    )
    plan = split_single(parse_contract(text).contract, ontology())
    assert plan.shape == "star"
    assert [block.mode for block in plan.subtasks] == ["write", "write"]
    assert len({block.partition for block in plan.subtasks}) == 2
    assert set(plan.interfaces) == {"out.csv", "other.csv"}


def test_two_same_layer_writers_get_distinct_partitions() -> None:
    """``write_serial`` is a real gate, so it needs a real two-writer layer."""
    text = CONTRACT.replace(
        "  - {name: out.csv, schema: {pd: float, amount: float}}",
        "  - {name: out.csv, schema: {pd: float, amount: float}}\n  - {name: other.csv, schema: {pd: float}}",
    )
    document = parse_contract(text)
    plan = split_by_source(document.contract, ontology(), Recon(source_ids=("src-1", "src-2")))
    writes = [block for block in plan.subtasks if block.mode == "write"]
    assert len(writes) == 2
    assert len({block.partition for block in writes}) == 2

    bundle = compile_bundle(document, plan)
    collapsed = replace(
        plan,
        subtasks=tuple(
            replace(block, partition="deliverable:all") if block.mode == "write" else block
            for block in plan.subtasks
        ),
    )
    assert "write_serial" in check_split(collapsed, document.contract, bundle, ontology()).codes()


def test_decomposer_rejects_an_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        Decomposer(strategy="entity")


def test_decomposer_dispatches_every_declared_strategy() -> None:
    """``STRATEGIES`` is the public surface; each name must reach its own builder."""
    field_plan = Decomposer(strategy="field").split(contract(), ontology())
    assert {block.axis for block in field_plan.subtasks} == {"field"}
    assert field_plan.shape == "pipeline"

    single_plan = Decomposer(strategy="single").split(contract(), ontology())
    assert single_plan.shape == "star"
    assert all(block.mode == "write" for block in single_plan.subtasks)


def test_decomposer_versions_increment() -> None:
    decomposer = Decomposer(strategy="source")
    first = decomposer.split(contract(), ontology(), Recon(source_ids=("src-1", "src-2")))
    second = decomposer.split(contract(), ontology(), Recon(source_ids=("src-1", "src-2")))
    assert first.version == 1 and second.version == 2


def test_refine_is_capped_by_the_round_budget() -> None:
    decomposer = Decomposer(strategy="source", budget=RoundBudget(max_rounds=1))
    plan = decomposer.split(contract(), ontology())
    assert plan.round == 1
    refined = decomposer.refine(contract(), ontology(), plan, AssemblyOutcome(passed=False, failed_blocks=("T-01",)))
    assert refined is plan  # ★ the ceiling returns the plan unchanged, no unbounded loop


def test_refine_advances_when_the_budget_allows() -> None:
    decomposer = Decomposer(strategy="source", budget=RoundBudget(max_rounds=3))
    plan = decomposer.split(contract(), ontology())
    refined = decomposer.refine(contract(), ontology(), plan, AssemblyOutcome(passed=False, failed_blocks=("T-01",)))
    assert refined.round == 2
    assert {block.axis for block in refined.subtasks} == {"field"}


# --------------------------------------------------------------------------- #
# acceptance #7: does the emitted plan pass core/split.py?
# --------------------------------------------------------------------------- #


def test_plan_from_strategy_passes_core_split() -> None:
    document = parse_contract(CONTRACT)
    plan = split_by_source(document.contract, ontology(), Recon(source_ids=("src-1", "src-2", "src-3"), evidence="surveyed 3 sources"))
    bundle = compile_bundle(document, plan)
    report = check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash())
    assert report.ok is True
    assert report.codes() == ()


def test_split_still_catches_a_broken_variant_of_that_plan() -> None:
    """The plan passes because it is legal, not because the check is asleep."""
    document = parse_contract(CONTRACT)
    plan = split_by_source(document.contract, ontology(), Recon(source_ids=("src-1", "src-2", "src-3")))
    bundle = compile_bundle(document, plan)

    assert "round_budget" in check_split(replace(plan, round=4), document.contract, bundle, ontology()).codes()
    assert "empty_plan" in check_split(replace(plan, subtasks=()), document.contract, bundle, ontology()).codes()
    duplicated = replace(plan, subtasks=(plan.subtasks[0], plan.subtasks[0]))
    assert "duplicate_id" in check_split(duplicated, document.contract, bundle, ontology()).codes()


def test_an_unmapped_deliverable_field_survives_as_its_raw_name() -> None:
    """★ The strategy must not drop a field the ontology cannot map -- it must
    hand the raw name on so ``split.concept_mapped`` can flag it."""
    text = CONTRACT.replace("schema: {pd: float, amount: float}", "schema: {pd: float, amount: float, mystery: str}")
    document = parse_contract(text)
    plan = split_by_source(document.contract, ontology(), Recon(source_ids=("src-1", "src-2")))
    merge = next(block for block in plan.subtasks if block.mode == "write")
    assert "mystery" in merge.output_fields
    assert "pd" in merge.output_fields

    bundle = compile_bundle(document, plan)
    assert "concept_mapped" in check_split(plan, document.contract, bundle, ontology()).codes()


def test_an_unmapped_output_field_is_flagged_by_split() -> None:
    """concept_mapped is a real gate even though the strategy maps what it can."""
    text = CONTRACT.replace("  - {ref: src-3}\n", "")
    document = parse_contract(text)
    plan = split_by_source(document.contract, ontology())
    bundle = compile_bundle(document, plan)
    broken = replace(plan, subtasks=tuple(replace(block, output_fields=("ghost_field",)) for block in plan.subtasks))
    assert "concept_mapped" in check_split(broken, document.contract, bundle, ontology()).codes()


# --------------------------------------------------------------------------- #
# ★ 层次切分：契约声明产物依赖 -> 一份产物一块、按声明接线
# --------------------------------------------------------------------------- #

AB = Path(__file__).resolve().parents[1] / "acceptance" / "ab" / "13f-crowding"

CHAIN = """
task_id: task-chain
goal: a chain of products
version: 1
deliverables:
  - {name: a.csv, schema: {pd: float}}
  - {name: b.csv, schema: {pd: float}, depends_on: [a.csv]}
  - {name: c.csv, schema: {pd: float}, depends_on: [b.csv]}
  - {name: d.csv, schema: {pd: float}, depends_on: [b.csv]}
  - {name: e.csv, schema: {pd: float}, depends_on: [c.csv, d.csv]}
sources:
  - {ref: src-1}
  - {ref: src-2}
constraints:
  hard:
    - {id: C-1, kind: presence, target: a.csv, spec: {min_rows: 1}, rationale: "x"}
"""


def _chain_plan(strategy: str = "field"):
    document = parse_contract(CHAIN)
    plan = Decomposer(strategy=strategy).split(document.contract, ontology())
    bundle = compile_bundle(document, plan)
    report = check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash())
    return plan, report


def test_declared_dependencies_produce_a_layered_plan() -> None:
    plan, report = _chain_plan()
    assert report.ok, report.codes()
    assert plan.shape == "layered"
    assert [block.inputs for block in plan.subtasks] == [
        (),
        ("a.csv",),
        ("b.csv",),
        ("b.csv",),
        ("c.csv", "d.csv"),
    ]
    assert {block.axis for block in plan.subtasks} == {"product"}
    assert len({block.partition for block in plan.subtasks}) == len(plan.subtasks)


def test_declared_dependencies_beat_the_selected_strategy() -> None:
    """声明了链，`source` 策略也不许把它拆成"读层 + 写层"的零依赖扇出。"""
    source_plan, report = _chain_plan(strategy="source")
    assert report.ok, report.codes()
    assert source_plan.shape == "layered"
    assert source_plan.subtasks[1].inputs == ("a.csv",)


def test_the_layered_capability_can_be_switched_off() -> None:
    """★ 反证：同一条输入，把「层间可依赖」关掉 ⇒ inputs 回到全空。"""
    from bobanana6.decompose import strategies

    document = parse_contract(CHAIN)
    on = Decomposer(strategy="field").split(document.contract, ontology())
    assert any(block.inputs for block in on.subtasks)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(strategies, "LAYERED_SPLIT", False)
        off = Decomposer(strategy="field").split(document.contract, ontology())
    finally:
        monkey.undo()
    assert off.shape == "star"
    assert all(not block.inputs for block in off.subtasks)


def test_the_layered_capability_is_read_at_call_time() -> None:
    from bobanana6.decompose import strategies as strategies_module

    assert strategies_module.LAYERED_SPLIT is True  # the gate `_build` reads


def test_a_chain_without_declarations_is_unchanged() -> None:
    """④ 反向：产物彼此独立 ⇒ 原样 star，一个 input 都不许多出来。"""
    text = CHAIN.replace(", depends_on: [a.csv]", "").replace(", depends_on: [b.csv]", "").replace(
        ", depends_on: [c.csv, d.csv]", ""
    )
    document = parse_contract(text)
    plan = Decomposer(strategy="field").split(document.contract, ontology())
    assert plan.shape == "star"
    assert all(block.inputs == () for block in plan.subtasks)
    # 逐字等于改动前的 split_single
    baseline = split_single(document.contract, ontology())
    assert plan.subtasks == baseline.subtasks
    assert plan.shape == baseline.shape


def test_a_single_deliverable_is_unchanged() -> None:
    """④ 单产物任务逐字不变：仍然按字段扇出（field 策略的老行为），没有多出任何输入。"""
    single = parse_contract(CONTRACT).contract
    plan = Decomposer(strategy="field").split(single, ontology())
    baseline = split_by_field(single, ontology())
    assert plan.shape == baseline.shape == "pipeline"
    assert plan.subtasks == baseline.subtasks
    assert all(block.inputs == () for block in plan.subtasks if block.mode == "read")


def _linear_chain(length: int) -> str:
    rows = ["  - {name: p00.csv, schema: {pd: float}}"]
    for index in range(1, length):
        rows.append(f"  - {{name: p{index:02d}.csv, schema: {{pd: float}}, depends_on: [p{index - 1:02d}.csv]}}")
    return (
        "task_id: task-linear\ngoal: a linear chain\nversion: 1\ndeliverables:\n"
        + "\n".join(rows)
        + "\n"
    )


@pytest.mark.parametrize("length", [3, 8, 20])
def test_chain_lengths_layer_correctly(length: int) -> None:
    document = parse_contract(_linear_chain(length))
    plan = Decomposer(strategy="field").split(document.contract, ontology())
    depth, cycle = subtask_layers(plan)
    assert cycle == ()
    assert plan.shape == "layered"
    assert [depth[block.id] for block in plan.subtasks] == list(range(length))
    for index, block in enumerate(plan.subtasks):
        expected = () if index == 0 else (f"p{index - 1:02d}.csv",)
        assert block.inputs == expected
    bundle = compile_bundle(document, plan)
    report = check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash())
    assert report.ok, report.codes()


def test_a_wide_chain_only_declares_its_own_edges() -> None:
    """§2.4 的接口数算术：链长 8、每层宽 2。

    两条并肩的链（每块只声明自己那条上游）：层内零边、层间 2 条/层 ⇒ 14 条。
    同一张图若"把上层全部产物都当输入"，每块 2 条 ⇒ 28 条；全平铺则 120 条。
    14/28 都是 O(N·w)，120 是 O(N²) —— 纪律把接口数压在层量级。
    """
    rows = []
    for layer in range(8):
        for lane in range(2):
            name = f"L{layer}N{lane}.csv"
            if layer == 0:
                rows.append(f"  - {{name: {name}, schema: {{pd: float}}}}")
            else:
                rows.append(
                    f"  - {{name: {name}, schema: {{pd: float}}, depends_on: [L{layer - 1}N{lane}.csv]}}"
                )
    document = parse_contract("task_id: task-wide\ngoal: wide\ndeliverables:\n" + "\n".join(rows) + "\n")
    plan = Decomposer(strategy="field").split(document.contract, ontology())
    blocks = len(plan.subtasks)
    edges = sum(len(block.inputs) for block in plan.subtasks)
    assert blocks == 16
    assert edges == 14  # 7 transitions x 2 lanes
    assert blocks * (blocks - 1) // 2 == 120  # the flat-cut灾难 this avoids
    bundle = compile_bundle(document, plan)
    assert check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash()).ok


def test_the_13f_contract_now_declares_its_chain() -> None:
    """★ 反证：13f 的 8 个产物 + 真实链关系 —— 逐条贴出的读数就在这里。"""
    from bobanana6.ontology import load_ontology_file

    document = parse_contract((AB / "task.yaml").read_text(encoding="utf-8"))
    onto = load_ontology_file(AB / "ontology.yaml").freeze()
    plan = Decomposer(strategy="field").split(document.contract, onto)
    assert plan.shape == "layered"
    assert all(block.inputs for block in plan.subtasks if block.output != "output/filing_resolution.csv")
    depth, cycle = subtask_layers(plan)
    assert cycle == ()
    assert [depth[block.id] for block in plan.subtasks] == [0, 1, 2, 2, 2, 2, 2, 3]
    bundle = compile_bundle(document, plan)
    assert check_split(plan, document.contract, bundle, onto, frozen_hash=onto.content_hash()).ok


def test_refine_keeps_the_declared_chain() -> None:
    """重切也不许把链拍平：refine 走同一张产物图。"""
    document = parse_contract(CHAIN)
    decomposer = Decomposer(strategy="source", budget=RoundBudget(max_rounds=3))
    plan = decomposer.split(document.contract, ontology())
    refined = decomposer.refine(
        document.contract, ontology(), plan, AssemblyOutcome(passed=False, failed_blocks=("T-02",))
    )
    assert refined.shape == "layered"
    assert refined.round == 2
    assert refined.subtasks[1].inputs == ("a.csv",)


def test_a_contract_written_out_of_order_is_still_emitted_in_topological_order() -> None:
    """契约把下游写在前面是合法的书写顺序，但计划必须排成可执行的顺序。"""
    text = (
        "task_id: t-reordered\ngoal: g\ndeliverables:\n"
        "  - {name: summary.csv, schema: {pd: float}, depends_on: [b.csv]}\n"
        "  - {name: b.csv, schema: {pd: float}, depends_on: [a.csv]}\n"
        "  - {name: a.csv, schema: {pd: float}}\n"
    )
    document = parse_contract(text)
    plan = Decomposer(strategy="field").split(document.contract, ontology())
    assert [block.output for block in plan.subtasks] == ["a.csv", "b.csv", "summary.csv"]
    assert [block.inputs for block in plan.subtasks] == [(), ("a.csv",), ("b.csv",)]
    depth, _ = subtask_layers(plan)
    assert [depth[block.id] for block in plan.subtasks] == [0, 1, 2]
    bundle = compile_bundle(document, plan)
    assert check_split(plan, document.contract, bundle, ontology(), frozen_hash=ontology().content_hash()).ok
