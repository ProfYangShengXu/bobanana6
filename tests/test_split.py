"""Split legality: one fixture per check, plus negatives that look like
violations but are not."""

from __future__ import annotations

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
