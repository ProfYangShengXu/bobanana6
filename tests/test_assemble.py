"""Assembly invariants: every failure must name the block responsible."""

from __future__ import annotations

from fakes import (
    artifact,
    block,
    bundle,
    constraint,
    contract,
    mini_ontology,
    plan,
    provenance,
    snapshot,
    subtask,
    worker,
)

from bobanana6.core.assemble import CHECKS, check_assembly, match_blocks
from bobanana6.core.capabilities import Capability
from bobanana6.core.checkers.base import CheckContext
from bobanana6.core.model import PlanCosts


def good_plan():
    return plan(
        subtasks=(subtask("T-01", output="out.csv", output_fields=("pd_3y",)),),
        interfaces={"out.csv": "schema:pd"},
        costs=PlanCosts(merge=1.0, execute=10.0),
    )


def good_bundle():
    return bundle(hard=(constraint("G-1", "presence", "*", {"min_rows": 1}),))


def good_context():
    return CheckContext(ontology=mini_ontology(), snapshots={"snap-1": snapshot("snap-1")})


def pieces():
    return {"out.csv": artifact("out.csv", ({"a": 1},))}


def provenances():
    return (provenance("out.csv:row=0", "snap-1"),)


def test_check_list_is_declared() -> None:
    assert "global_invariant" in CHECKS and "cut_complete" in CHECKS


def test_clean_assembly_passes() -> None:
    result = check_assembly(good_plan(), contract(), good_bundle(), pieces(), good_context(), provenance=provenances())
    assert result.passed, result.codes()


def test_missing_block_is_named() -> None:
    result = check_assembly(good_plan(), contract(), good_bundle(), {}, good_context(), provenance=())
    assert "cut_complete" in result.codes()
    assert "T-01" in result.violations[0].block_ids


def test_unexpected_piece_is_reported() -> None:
    extra = dict(pieces())
    extra["ghost.csv"] = artifact("ghost.csv", ({"a": 1},))
    result = check_assembly(good_plan(), contract(), good_bundle(), extra, good_context(), provenance=provenances())
    assert "cut_complete" in result.codes()


def test_unproduced_deliverable_is_reported() -> None:
    result = check_assembly(good_plan(), contract(("out.csv", "missing.csv")), good_bundle(), pieces(), good_context(), provenance=provenances())
    assert "cut_complete" in result.codes()


def test_failing_global_invariant_is_reported() -> None:
    failing = bundle(hard=(constraint("G-1", "presence", "*", {"min_rows": 5}),))
    result = check_assembly(good_plan(), contract(), failing, pieces(), good_context(), provenance=provenances())
    assert "global_invariant" in result.codes()
    assert "G-1" in result.violations[0].detail


def test_broken_provenance_names_the_block() -> None:
    result = check_assembly(good_plan(), contract(), good_bundle(), pieces(), good_context(), provenance=())
    assert "provenance_connected" in result.codes()
    assert result.coverage == 0.0


def test_expensive_merge_is_reported() -> None:
    expensive = plan(
        subtasks=(subtask("T-01", output="out.csv", output_fields=("pd_3y",)),),
        interfaces={"out.csv": "schema:pd"},
        costs=PlanCosts(merge=9.0, execute=10.0),
    )
    result = check_assembly(expensive, contract(), good_bundle(), pieces(), good_context(), provenance=provenances())
    assert "merge_cost" in result.codes()


def test_partition_conflict_names_the_writers() -> None:
    a = subtask("T-01", output="a.csv", mode="write", partition="same", output_fields=("pd_3y",))
    b = subtask("T-02", output="b.csv", mode="write", partition="same", output_fields=("pd_3y",))
    item = plan(
        subtasks=(a, b),
        interfaces={"a.csv": "s", "b.csv": "s"},
        costs=PlanCosts(merge=1.0, execute=10.0),
    )
    ctx = CheckContext(ontology=mini_ontology(), snapshots={"snap-1": snapshot("snap-1")})
    result = check_assembly(
        item,
        contract(("a.csv", "b.csv")),
        good_bundle(),
        {"a.csv": artifact("a.csv", ({"a": 1},)), "b.csv": artifact("b.csv", ({"a": 1},))},
        ctx,
        provenance=(provenance("a.csv:row=0"), provenance("b.csv:row=0")),
    )
    assert "partition_conflict" in result.codes()
    offenders = next(v for v in result.violations if v.check == "partition_conflict")
    assert set(offenders.block_ids) == {"T-01", "T-02"}


def test_capability_matching_is_available_through_match_blocks() -> None:
    result = match_blocks((block("B-1", frozenset({Capability.SEARCH})),), (worker("W-1", frozenset({Capability.READ})),))
    assert result.gaps


def test_capability_issues_surface_as_assembly_violations() -> None:
    result = check_assembly(
        good_plan(),
        contract(),
        good_bundle(),
        pieces(),
        good_context(),
        provenance=provenances(),
        blocks=(block("B-1", frozenset({Capability.SEARCH})),),
        workers=(worker("W-1", frozenset({Capability.READ})),),
    )
    assert "capability_gap" in result.codes()


def test_optional_deliverable_may_be_absent() -> None:
    """`DeliverableSpec.required` is load-bearing: an optional deliverable that
    no block produced is not an assembly failure, but a required one still is."""
    result = check_assembly(
        good_plan(),
        contract(("out.csv", "nice-to-have.csv"), optional=("nice-to-have.csv",)),
        good_bundle(),
        pieces(),
        good_context(),
        provenance=provenances(),
    )
    assert result.passed, result.codes()


def test_required_deliverable_missing_is_still_a_failure() -> None:
    result = check_assembly(
        good_plan(),
        contract(("out.csv", "must-have.csv")),
        good_bundle(),
        pieces(),
        good_context(),
        provenance=provenances(),
    )
    assert "cut_complete" in result.codes()
