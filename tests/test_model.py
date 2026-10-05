"""Value-object validation: illegal and missing-field inputs must not construct."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from fakes import mini_ontology, subtask, toolset_for

from bobanana6.core.model import (
    KNOWN_KINDS,
    MAX_ROUNDS,
    UNIT_SYMBOLS,
    Artifact,
    BlindSpot,
    Budget,
    Candidate,
    Concept,
    Conflict,
    Constraint,
    ConstraintBundle,
    Definition,
    DefinitionMismatch,
    DisagreementTypes,
    Limits,
    Ontology,
    Provenance,
    Relation,
    Snapshot,
    SplitPlan,
    Subtask,
    TaskContract,
    Unit,
    canonical_json,
    content_digest,
)
from bobanana6.core.ports import ToolSet


def test_known_kinds_is_exactly_fifteen() -> None:
    assert len(KNOWN_KINDS) == 15


def test_frozen_value_object_rejects_assignment() -> None:
    concept = Concept(id="a", label="A", kind="measure", domain="d")
    with pytest.raises(FrozenInstanceError):
        concept.label = "B"  # type: ignore[misc]


def test_replace_reruns_validation() -> None:
    concept = Concept(id="a", label="A", kind="measure", domain="d")
    with pytest.raises(ValueError):
        replace(concept, label="")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Constraint(id="", kind="presence", target="*", spec={}, rationale="x"),
        lambda: Constraint(id="C", kind="presence", target="*", spec={}, rationale=""),
        lambda: Concept(id="a", label="A", kind="nonsense", domain="d"),
        lambda: Definition(concept_id="a", version="", text="t"),
        lambda: Unit(concept_id="a", symbol="X", scale=0.0, base="X"),
        lambda: Relation(src="a", dst="b", kind="invented", cardinality="1:1"),
        lambda: Subtask(id="T", goal="g", inputs=(), output="o", contract_ref="c", axis="source", mode="WRITE"),
        lambda: Subtask(id="T", goal="g", inputs=(), output="o", contract_ref="c", axis="time", mode="read"),
        lambda: SplitPlan(task_id="t", version=0, round=0, subtasks=(), shape="star"),
        lambda: SplitPlan(task_id="t", version=1, round=-1, subtasks=(), shape="star"),
        lambda: SplitPlan(task_id="t", version=1, round=0, subtasks=(), shape="mesh"),
        lambda: Provenance(value_ref="", snapshot_ref="s"),
        lambda: Snapshot(ref="", body_path=__import__("pathlib").Path("x"), captured_at="now"),
        lambda: Candidate(subtask_id="", source_id="s"),
        lambda: Budget(max_calls=-1, max_wall_s=1, max_output=1),
        lambda: Limits(max_rounds=-1),
        lambda: Limits(merge_cost_ratio=0.0),
        lambda: Limits(provenance_min_coverage=1.5),
        lambda: TaskContract(task_id="", goal="g"),
    ],
)
def test_invalid_constructions_raise(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_constraint_bundle_rejects_duplicate_ids() -> None:
    c = Constraint(id="C-1", kind="presence", target="*", spec={}, rationale="x")
    with pytest.raises(ValueError):
        ConstraintBundle(task_id="t", version=1, hard=(c, c))


def test_ontology_key_must_match_concept_id() -> None:
    with pytest.raises(ValueError):
        Ontology(domain="d", version="1", concepts={"x": Concept(id="y", label="Y", kind="measure", domain="d")}, definitions={}, units={})


def test_unknown_constraint_kind_constructs_so_the_kernel_can_reject_it() -> None:
    constraint = Constraint(id="C", kind="rang3", target="*", spec={}, rationale="x")
    assert constraint.kind not in KNOWN_KINDS


def test_max_rounds_default_is_three() -> None:
    assert MAX_ROUNDS == 3
    assert Limits().max_rounds == 3


def test_three_divergence_types_have_disjoint_shapes() -> None:
    assert set(DisagreementTypes) == {BlindSpot, Conflict, DefinitionMismatch}
    assert len({frozenset(t.__dataclass_fields__) for t in DisagreementTypes}) == 3


def test_canonical_json_and_digest_are_order_stable() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert content_digest({"a": (1, 2)}) == content_digest({"a": (1, 2)})


def test_toolset_binding_rule_is_owned_by_core() -> None:
    ts = toolset_for("T-03", allowed_inputs=("T-02_output",))
    assert ts.allows_upstream("T-02_output")
    assert not ts.allows_upstream("T-07_output")
    with pytest.raises(PermissionError):
        ts.require_upstream("T-07_output")


def test_toolset_rejects_empty_subtask() -> None:
    with pytest.raises(ValueError):
        ToolSet(subtask="", semantics=None, task=None, check=None)  # type: ignore[arg-type]


def test_toolset_has_no_prompt_field() -> None:
    from dataclasses import fields

    assert "prompt" not in {f.name for f in fields(ToolSet)}


def test_toolset_specs_drop_source_tool_when_unavailable() -> None:
    without = {spec.name for spec in toolset_for("T-1").specs()}
    with_sources = {spec.name for spec in toolset_for("T-1", sources=True).specs()}
    assert "sources.snapshot" not in without
    assert "sources.snapshot" in with_sources


def test_mini_fixtures_are_usable() -> None:
    ontology = mini_ontology()
    assert ontology.is_frozen
    item = subtask("T-01", output="out.csv")
    assert item.mode == "read"


def test_limits_has_no_global_ruler_fields() -> None:
    """`requires_calls` is counted in calls, so a global cost ruler is redundant
    (spec §5.0.1 ①). The ambiguous output ruler lives on Budget instead."""
    from dataclasses import fields

    assert {f.name for f in fields(Limits)} == {
        "max_rounds",
        "merge_cost_ratio",
        "tol",
        "provenance_min_coverage",
        "corroboration_min_sources",
    }


def test_budget_carries_its_own_output_ruler() -> None:
    assert Budget(max_calls=1, max_wall_s=1, max_output=1).max_output_unit == "tokens"


def test_budget_rejects_a_ruler_outside_the_whitelist() -> None:
    for bad in ("widgets", "chars", "seconds", "calls"):
        with pytest.raises(ValueError):
            Budget(max_calls=1, max_wall_s=1, max_output=1, max_output_unit=bad)


def test_every_declared_unit_symbol_is_whitelisted() -> None:
    # Narrowed to the two genuinely ambiguous rulers (spec §5.0.1 ④): chars /
    # seconds / calls were removed because nothing would ever declare them.
    assert UNIT_SYMBOLS == frozenset({"tokens", "bytes"})
    for symbol in UNIT_SYMBOLS:
        Budget(max_calls=0, max_wall_s=0, max_output=0, max_output_unit=symbol)


def test_removed_declaration_only_fields_are_gone() -> None:
    """The §5.0.2 '待复核' fields had no reader anywhere, so they were deleted
    rather than kept 'just in case'."""
    from dataclasses import fields

    assert "provenance" not in {f.name for f in fields(ConstraintBundle)}
    assert "magnitude" not in {f.name for f in fields(BlindSpot)}
    assert "path" not in {f.name for f in fields(Artifact)}
