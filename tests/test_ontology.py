"""Ontology reference validation -- aliases, hierarchy, units, code systems.

Only reference legality is judged. Whether a definition is *right* for a domain
is deliberately not a question this module can answer.
"""

from __future__ import annotations

import pytest
from fakes import mini_ontology

from bobanana6.core.model import Concept, Definition, Ontology, Relation, Unit
from bobanana6.core.ontology import (
    convert_value,
    map_field,
    pinned_version,
    resolve_unit,
    validate_code,
    validate_ontology,
)


def test_reference_clean_ontology_is_valid() -> None:
    report = validate_ontology(mini_ontology())
    assert report.is_valid, report.codes()


def test_definition_citing_missing_concept_is_reported() -> None:
    ontology = Ontology(
        domain="d",
        version="1",
        concepts={"a": Concept(id="a", label="A", kind="measure", domain="d")},
        definitions={"k": Definition(concept_id="ghost", version="v1", text="t")},
        units={},
    )
    assert "definition_orphan" in validate_ontology(ontology).codes()


def test_unit_citing_missing_concept_is_reported() -> None:
    ontology = Ontology(
        domain="d",
        version="1",
        concepts={"a": Concept(id="a", label="A", kind="measure", domain="d")},
        definitions={},
        units={"u": Unit(concept_id="ghost", symbol="X", scale=1.0, base="X")},
    )
    assert "unit_orphan" in validate_ontology(ontology).codes()


def test_relation_endpoints_are_checked() -> None:
    ontology = Ontology(
        domain="d",
        version="1",
        concepts={"a": Concept(id="a", label="A", kind="measure", domain="d")},
        definitions={},
        units={},
        relations=(Relation(src="a", dst="ghost", kind="belongs_to", cardinality="1:1"),),
    )
    assert "relation_dst_missing" in validate_ontology(ontology).codes()


def test_duplicate_alias_is_an_ambiguity() -> None:
    ontology = Ontology(
        domain="d",
        version="1",
        concepts={
            "a": Concept(id="a", label="A", kind="measure", domain="d", aliases=("shared",)),
            "b": Concept(id="b", label="B", kind="measure", domain="d", aliases=("shared",)),
        },
        definitions={},
        units={},
    )
    assert "alias_conflict" in validate_ontology(ontology).codes()


def test_missing_parent_is_reported() -> None:
    ontology = Ontology(
        domain="d",
        version="1",
        concepts={"a": Concept(id="a", label="A", kind="measure", domain="d", parent="ghost")},
        definitions={},
        units={},
    )
    assert "parent_missing" in validate_ontology(ontology).codes()


def test_self_parent_is_reported() -> None:
    ontology = Ontology(
        domain="d",
        version="1",
        concepts={"a": Concept(id="a", label="A", kind="measure", domain="d", parent="a")},
        definitions={},
        units={},
    )
    assert "parent_self" in validate_ontology(ontology).codes()


def test_parent_cycle_is_reported_with_its_members() -> None:
    ontology = Ontology(
        domain="d",
        version="1",
        concepts={
            "a": Concept(id="a", label="A", kind="measure", domain="d", parent="b"),
            "b": Concept(id="b", label="B", kind="measure", domain="d", parent="a"),
        },
        definitions={},
        units={},
    )
    report = validate_ontology(ontology)
    assert "parent_cycle" in report.codes()


def test_map_field_resolves_id_label_and_alias() -> None:
    ontology = mini_ontology()
    assert map_field("credit.pd", ontology) == "credit.pd"
    assert map_field("PD", ontology) == "credit.pd"
    assert map_field("pd_3y", ontology) == "credit.pd"
    assert map_field("probability_of_default", ontology) == "credit.pd"
    assert map_field("unheard_of", ontology) is None


def test_alias_mapping_is_deterministic_across_calls() -> None:
    ontology = mini_ontology()
    assert {map_field("pd_3y", ontology) for _ in range(5)} == {"credit.pd"}


def test_unit_conversion_uses_declared_scale() -> None:
    ontology = mini_ontology()
    unit = resolve_unit("credit.amount", ontology)
    assert unit is not None and unit.symbol == "CNY"
    assert convert_value(700.0, "CNY", "USD", ontology) == pytest.approx(100.0)


def test_unit_conversion_refuses_unknown_or_mismatched_bases() -> None:
    ontology = mini_ontology()
    with pytest.raises(KeyError):
        convert_value(1.0, "EUR", "USD", ontology)


def test_validate_code_accepts_declared_system_only() -> None:
    ontology = mini_ontology()
    assert validate_code("AAA", "rating_scale", ontology)
    assert not validate_code("ZZZ", "rating_scale", ontology)
    assert not validate_code("AAA", "unheard_system", ontology)


def test_pinned_version_reads_the_declared_version() -> None:
    ontology = mini_ontology()
    assert pinned_version("credit.pd", ontology) == "v1"
    assert pinned_version("credit.amount", ontology) is None


def test_validation_does_not_inspect_calendar_contents() -> None:
    """`calendar` is external:decompose -- core neither validates nor reads it."""
    calendar = {"weeks": {"W1": [1, 2]}, "tz": "Asia/Shanghai", "junk": {"deep": [True, None]}}
    ontology = Ontology(
        domain="d",
        version="1",
        concepts={"a": Concept(id="a", label="A", kind="measure", domain="d")},
        definitions={},
        units={},
        calendar=calendar,
    )
    report = validate_ontology(ontology)
    assert report.is_valid, report.codes()

    frozen = ontology.freeze()
    assert dict(frozen.calendar)["tz"] == "Asia/Shanghai"
    assert frozen.content_hash() == ontology.freeze().content_hash()
