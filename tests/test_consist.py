"""Self-consistency gate: one fixture per check plus lookalike negatives.

Any hit blocks the cluster launch, so the gate is asserted to be *closed* on a
clean input and *open with the right code* on each injected defect.
"""

from __future__ import annotations

from fakes import bundle, constraint, contract, mini_ontology, plan, subtask

from bobanana6.core.consist import CHECKS, check_consistency
from bobanana6.core.model import Budget, PlanCosts


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
    values = {"hard": (constraint("C-0", "presence", "T-01", {"min_rows": 1}),)}
    values.update(overrides)
    return bundle(**values)


def consist(item=None, contract_=None, item_bundle=None, **overrides):
    values = {
        "budget": Budget(max_calls=10, max_wall_s=100, max_output=100),
        "available_capabilities": frozenset({"read", "compute"}),
    }
    values.update(overrides)
    return check_consistency(
        item if item is not None else good_plan(),
        contract_ if contract_ is not None else contract(),
        item_bundle if item_bundle is not None else good_bundle(),
        mini_ontology(),
        **values,
    )


def test_check_list_matches_the_spec() -> None:
    assert len(CHECKS) == 16
    assert {"ruler_mixed", "ruler_vs_limits"} <= set(CHECKS)


def test_clean_input_is_allowed_to_start() -> None:
    report = consist()
    assert report.start_allowed
    assert report.codes() == ()


# --------------------------------------------------------------------------- #
# one fixture per check
# --------------------------------------------------------------------------- #


def test_contradiction() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("R-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1}),
            constraint("R-2", "range", "out.csv", {"column": "pd_3y", "min": 2, "max": 3}),
        )
    )
    assert "contradiction" in consist(item_bundle=item_bundle).codes()


def test_overlapping_ranges_do_not_contradict() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("R-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 2}),
            constraint("R-2", "range", "out.csv", {"column": "pd_3y", "min": 1, "max": 3}),
        )
    )
    assert "contradiction" not in consist(item_bundle=item_bundle).codes()


def test_tool_missing() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "custom", "T-01", {"check": "x", "requires": ["unobtainium"]}),
        )
    )
    assert "tool_missing" in consist(item_bundle=item_bundle).codes()


def test_tool_missing_fails_closed_without_an_inventory() -> None:
    assert "tool_missing" in consist(available_capabilities=None).codes()


def test_unreachable() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "T-01", {"requires_calls": 999}),
        )
    )
    assert "unreachable" in consist(item_bundle=item_bundle).codes()


def test_unreachable_fails_closed_without_a_budget() -> None:
    assert "unreachable" in consist(budget=None).codes()


def test_orphan_target() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "ghost-target", {"min_rows": 1}),
        )
    )
    assert "orphan_target" in consist(item_bundle=item_bundle).codes()


def test_stale_blind() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "concept_ref", "T-01", {"field": "pd_3y"}),
        ),
        blind=("pd_3y",),
    )
    assert "stale_blind" in consist(item_bundle=item_bundle).codes()


def test_unsatisfiable_tol() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("R-1", "range", "out.csv", {"column": "pd_3y", "min": 0, "max": 1, "tol": 0.001, "precision": 0.01}),
        )
    )
    assert "unsatisfiable_tol" in consist(item_bundle=item_bundle).codes()


def test_cycle() -> None:
    a = subtask("T-01", output="a.csv", inputs=("b.csv",), output_fields=("pd_3y",))
    b = subtask("T-02", output="b.csv", inputs=("a.csv",), output_fields=("pd_3y",))
    item = good_plan(subtasks=(a, b), shape="pipeline", interfaces={"a.csv": "s", "b.csv": "s"})
    item_bundle = good_bundle(
        hard=(constraint("C-1", "presence", "T-01", {}), constraint("C-2", "presence", "T-02", {}))
    )
    codes = consist(item=item, contract_=contract(("a.csv", "b.csv")), item_bundle=item_bundle).codes()
    assert "cycle" in codes


def test_missing_iface() -> None:
    producer = subtask("T-01", output="mid.csv", output_fields=("pd_3y",))
    consumer = subtask("T-02", output="out.csv", inputs=("mid.csv",), output_fields=("pd_3y",))
    item = good_plan(subtasks=(producer, consumer), shape="pipeline", interfaces={"mid.csv": "s", "out.csv": "s"})
    item_bundle = good_bundle(hard=(constraint("C-2", "presence", "T-02", {}),))
    assert "missing_iface" in consist(item=item, item_bundle=item_bundle).codes()


def test_unverifiable() -> None:
    assert "unverifiable" in consist(item_bundle=good_bundle(hard=())).codes()


def test_write_serial() -> None:
    a = subtask("T-01", output="a.csv", mode="write", output_fields=("pd_3y",))
    b = subtask("T-02", output="b.csv", mode="write", output_fields=("pd_3y",))
    item = good_plan(subtasks=(a, b), interfaces={"a.csv": "s", "b.csv": "s"})
    item_bundle = good_bundle(hard=(constraint("C-1", "presence", "T-01", {}), constraint("C-2", "presence", "T-02", {})))
    assert "write_serial" in consist(item=item, contract_=contract(("a.csv", "b.csv")), item_bundle=item_bundle).codes()


def test_merge_expensive() -> None:
    assert "merge_expensive" in consist(item=good_plan(costs=PlanCosts(merge=9.0, execute=10.0))).codes()


def test_concept_unmapped() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "concept_ref", "T-01", {"field": "mystery"}),
        )
    )
    assert "concept_unmapped" in consist(item_bundle=item_bundle).codes()


def test_declared_unmapped_concept_is_not_a_violation() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "concept_ref", "T-01", {"field": "mystery"}),
        ),
        unmapped=(("mystery", "no concept yet"),),
    )
    assert "concept_unmapped" not in consist(item_bundle=item_bundle).codes()


def test_def_version_mix() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("P-1", "definition_pin", "T-01", {"concept": "credit.pd", "version": "v1"}),
            constraint("P-2", "definition_pin", "T-01", {"concept": "credit.pd", "version": "v2"}),
        )
    )
    assert "def_version_mix" in consist(item_bundle=item_bundle).codes()


def test_same_version_pin_is_not_a_mix() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("P-1", "definition_pin", "T-01", {"concept": "credit.pd", "version": "v1"}),
            constraint("P-2", "definition_pin", "out.csv", {"concept": "credit.pd", "version": "v1"}),
        )
    )
    assert "def_version_mix" not in consist(item_bundle=item_bundle).codes()


def test_unit_conflict() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("U-1", "unit_consistency", "T-01", {"concept": "credit.amount", "unit": "CNY"}),
            constraint("U-2", "unit_consistency", "out.csv", {"concept": "credit.amount", "unit": "USD"}),
        )
    )
    assert "unit_conflict" in consist(item_bundle=item_bundle).codes()


def test_same_unit_is_not_a_conflict() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("U-1", "unit_consistency", "T-01", {"concept": "credit.amount", "unit": "CNY"}),
            constraint("U-2", "unit_consistency", "out.csv", {"concept": "credit.amount", "unit": "CNY"}),
        )
    )
    assert "unit_conflict" not in consist(item_bundle=item_bundle).codes()


def test_reports_every_hit_not_only_the_first() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "concept_ref", "T-01", {"field": "pd_3y"}),
            constraint("P-1", "definition_pin", "T-01", {"concept": "credit.pd", "version": "v1"}),
            constraint("P-2", "definition_pin", "out.csv", {"concept": "credit.pd", "version": "v2"}),
        ),
        blind=("pd_3y",),
    )
    codes = consist(item_bundle=item_bundle).codes()
    assert "def_version_mix" in codes and "stale_blind" in codes


# --------------------------------------------------------------------------- #
# ruler checks (架构设计 §10; spec §5.0.1): ruler_mixed / ruler_vs_limits
# --------------------------------------------------------------------------- #


def test_ruler_mixed_accepts_a_uniform_ruler() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "T-01", {"requires_calls": 5, "unit": "calls"}),
            constraint("C-2", "presence", "T-01", {"requires_calls": 7, "unit": "calls"}),
        )
    )
    assert "ruler_mixed" not in consist(item_bundle=item_bundle).codes()


def test_ruler_mixed_rejects_two_rulers_in_one_dimension() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "T-01", {"requires_wall_s": 5, "unit": "seconds"}),
            constraint("C-2", "presence", "T-01", {"requires_wall_s": 7, "unit": "minutes"}),
        )
    )
    assert "ruler_mixed" in consist(item_bundle=item_bundle).codes()


def test_ruler_mixed_ignores_declarations_without_a_ruler() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "T-01", {"requires_calls": 5}),
            constraint("C-2", "presence", "T-01", {"requires_calls": 6, "unit": "calls"}),
        )
    )
    assert "ruler_mixed" not in consist(item_bundle=item_bundle).codes()


def test_ruler_vs_limits_accepts_a_matching_budget_ruler() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "T-01", {"requires_output": 100, "unit": "tokens"}),
        )
    )
    assert "ruler_vs_limits" not in consist(item_bundle=item_bundle).codes()


def test_ruler_vs_limits_rejects_a_ruler_the_budget_does_not_use() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "T-01", {"requires_output": 100, "unit": "bytes"}),
        )
    )
    # bytes (HTTP body ruler) vs tokens (model-output ruler): not comparable.
    assert "ruler_vs_limits" in consist(item_bundle=item_bundle).codes()


def test_ruler_vs_limits_checks_against_the_budget_it_is_given() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "T-01", {"requires_output": 100, "unit": "bytes"}),
        )
    )
    matching = Budget(max_calls=10, max_wall_s=100, max_output=100, max_output_unit="bytes")
    assert "ruler_vs_limits" not in consist(item_bundle=item_bundle, budget=matching).codes()


def test_ruler_vs_limits_fails_closed_without_a_budget() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "T-01", {"requires_output": 100, "unit": "tokens"}),
        )
    )
    assert "ruler_vs_limits" in consist(item_bundle=item_bundle, budget=None).codes()


def test_ruler_vs_limits_is_silent_when_no_output_ruler_is_declared() -> None:
    item_bundle = good_bundle(
        hard=(
            constraint("C-0", "presence", "T-01", {"min_rows": 1}),
            constraint("C-1", "presence", "T-01", {"requires_calls": 5, "unit": "calls"}),
        )
    )
    assert "ruler_vs_limits" not in consist(item_bundle=item_bundle).codes()
