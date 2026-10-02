"""End-to-end smoke over the kernel with fakes only.

No model, no network, no ontology file. The point is that the whole chain --
plan -> bundle -> admit -> converge -> assemble -> consistency gate -- runs
with the same deterministic inputs and that iteration is bounded.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fakes import (
    artifact,
    bundle,
    candidate,
    constraint,
    contract,
    mini_ontology,
    plan,
    provenance,
    snapshot,
    subtask,
)

from bobanana6.core.assemble import check_assembly
from bobanana6.core.checkers.base import CheckContext
from bobanana6.core.consist import check_consistency
from bobanana6.core.kernel import converge
from bobanana6.core.model import Budget, ConvergeDecision, PlanCosts, Recon
from bobanana6.core.split import check_split

pytestmark = pytest.mark.acceptance_smoke


def test_full_fake_pipeline_and_bounded_iteration() -> None:
    ontology = mini_ontology()
    task_contract = contract()

    # R0: reconnaissance happens before any cut.
    recon = Recon(evidence="surveyed src-1 and src-2", source_ids=("src-1", "src-2"))
    assert recon.evidence

    # R1: a cut, a bundle, and two admissible but disagreeing candidates.
    first_plan = plan(
        subtasks=(subtask("T-01", output="out.csv", output_fields=("pd_3y",)),),
        interfaces={"out.csv": "schema:pd"},
        round=1,
        version=1,
        costs=PlanCosts(merge=1.0, execute=10.0),
    )
    bundle_v1 = bundle(
        hard=(constraint("C-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),),
        version=1,
    )
    ctx = CheckContext(ontology=ontology, snapshots={"snap-1": snapshot("snap-1")})

    left = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"pd_3y": 0.1},)),))
    right = candidate("T-01", "src-2", artifacts=(artifact("out.csv", ({"pd_3y": 0.9},)),))
    convergence = converge(bundle_v1, (left, right), ctx)
    assert convergence.decision is ConvergeDecision.RECORD_CONFLICT

    # Assembly of the chosen piece holds, and its provenance reaches a snapshot.
    assembly = check_assembly(
        first_plan,
        task_contract,
        bundle_v1,
        {"out.csv": artifact("out.csv", ({"pd_3y": 0.1},))},
        ctx,
        provenance=(provenance("out.csv:row=0", "snap-1"),),
    )
    assert assembly.passed, assembly.codes()

    # The consistency gate lets this plan start.
    gate = check_consistency(
        first_plan,
        task_contract,
        bundle_v1,
        ontology,
        budget=Budget(max_calls=10, max_wall_s=100, max_output=100),
        available_capabilities=frozenset({"read"}),
    )
    assert gate.start_allowed, gate.codes()

    # R2: iteration bumps both counters together.
    second_plan = replace(first_plan, round=2, version=2)
    bundle_v2 = replace(bundle_v1, version=2)
    assert second_plan.round > first_plan.round
    assert bundle_v2.version > bundle_v1.version

    # R4 is refused: iteration is bounded, not open ended.
    fourth_plan = replace(first_plan, round=4, version=4)
    assert "round_budget" in check_split(fourth_plan, task_contract, bundle_v2, ontology).codes()


def test_repeated_runs_are_identical() -> None:
    """Same inputs, same outputs -- the kernel is a pure function."""
    ontology = mini_ontology()
    item = bundle(hard=(constraint("C-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),))
    ctx = CheckContext(ontology=ontology)
    left = candidate("T-01", "src-1", artifacts=(artifact("out.csv", ({"pd_3y": 0.1},)),))
    right = candidate("T-01", "src-2", artifacts=(artifact("out.csv", ({"pd_3y": 0.9},)),))
    first = converge(item, (left, right), ctx)
    second = converge(item, (left, right), ctx)
    assert first == second
