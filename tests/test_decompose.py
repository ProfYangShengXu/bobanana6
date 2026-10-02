"""decompose/: round budget + strategies that produce a plan core/split.py accepts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bobanana6.contract import compile_bundle, parse_contract
from bobanana6.core.model import AssemblyOutcome, Recon
from bobanana6.core.split import check_split
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
