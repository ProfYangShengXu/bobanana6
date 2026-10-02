"""ontology/loader.py: structural file validation, then build the core model."""

from __future__ import annotations

from pathlib import Path

import pytest

from bobanana6.core.ontology import validate_ontology
from bobanana6.ontology import OntologyLoadError, load_ontology, load_ontology_file

VALID = """
domain: credit-risk
version: "2026.1"
concepts:
  credit.pd: {label: PD, kind: measure, aliases: [pd_3y], unit: ratio}
  credit.rating: {label: Rating, kind: attribute, code_system: rating_scale}
units:
  credit.pd: {symbol: ratio, scale: 1.0, base: ratio}
relations:
  - {src: credit.pd, dst: credit.rating, kind: belongs_to, cardinality: "N:M"}
definitions:
  credit.pd: {version: "2026.1", text: point-in-time PD, edge_cases: [restructured]}
code_systems:
  rating_scale: [AAA, BBB]
calendar: {timezone: Asia/Shanghai}
"""


def test_valid_file_builds_the_core_model() -> None:
    ontology = load_ontology(VALID)
    assert ontology.domain == "credit-risk"
    assert ontology.version == "2026.1"
    assert set(ontology.concepts) == {"credit.pd", "credit.rating"}
    assert ontology.units["credit.pd"].symbol == "ratio"
    assert ontology.relations[0].cardinality == "N:M"
    assert ontology.code_systems["rating_scale"] == ("AAA", "BBB")
    assert ontology.calendar == {"timezone": "Asia/Shanghai"}


def test_loaded_ontology_is_not_frozen() -> None:
    """Freezing before workers start is an explicit caller step (core:split checks it)."""
    assert load_ontology(VALID).is_frozen is False


def test_concept_domain_defaults_to_the_file_domain() -> None:
    assert load_ontology(VALID).concepts["credit.pd"].domain == "credit-risk"


def test_definitions_keyed_by_concept_id() -> None:
    definition = load_ontology(VALID).definitions["credit.pd"]
    assert definition.concept_id == "credit.pd"
    assert definition.version == "2026.1"
    assert definition.edge_cases == ("restructured",)


def test_multiple_definition_versions_coexist() -> None:
    text = (
        "domain: d\nversion: '1'\nconcepts:\n  pd: {label: PD, kind: measure}\n"
        "definitions:\n"
        '  pd@v1: {concept_id: pd, version: "2026.1", text: a}\n'
        '  pd@v2: {concept_id: pd, version: "2027.1", text: b}\n'
    )
    definitions = load_ontology(text).definitions
    assert {definition.version for definition in definitions.values()} == {"2026.1", "2027.1"}
    assert {definition.concept_id for definition in definitions.values()} == {"pd"}


# --------------------------------------------------------------------------- #
# structural failures name file + path + rule
# --------------------------------------------------------------------------- #


def test_missing_required_section_is_reported() -> None:
    with pytest.raises(OntologyLoadError) as error:
        load_ontology("domain: d\nversion: '1'\n", source="bad.yaml")
    message = str(error.value)
    assert message.startswith("bad.yaml:")
    assert "'concepts' is a required property" in message and "rule: required" in message


def test_invalid_concept_kind_names_the_path_and_rule() -> None:
    text = "domain: d\nversion: '1'\nconcepts:\n  a: {label: A, kind: widget}\n"
    with pytest.raises(OntologyLoadError) as error:
        load_ontology(text, source="bad.yaml")
    message = str(error.value)
    assert message.startswith("bad.yaml:$.concepts.a.kind:")
    assert "'widget' is not one of" in message
    assert "rule: enum" in message


def test_wildcard_version_is_rejected() -> None:
    with pytest.raises(OntologyLoadError, match="rule: not"):
        load_ontology("domain: d\nversion: '*'\nconcepts:\n  a: {label: A, kind: measure}\n")


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(OntologyLoadError, match="additionalProperties"):
        load_ontology("domain: d\nversion: '1'\nconcepts:\n  a: {label: A, kind: measure}\nextra: 1\n")


def test_dangling_unit_reference_is_refused_not_dropped() -> None:
    text = (
        "domain: d\nversion: '1'\n"
        "concepts:\n  a: {label: A, kind: measure, unit: ratio}\n"
        "units:\n  a: {symbol: CNY, scale: 1.0, base: CNY}\n"
    )
    with pytest.raises(OntologyLoadError) as error:
        load_ontology(text, source="bad.yaml")
    assert "unit 'ratio' is not defined" in str(error.value)
    assert "rule: reference" in str(error.value)


def test_dangling_code_system_reference_is_refused() -> None:
    text = "domain: d\nversion: '1'\nconcepts:\n  a: {label: A, kind: attribute, code_system: nope}\n"
    with pytest.raises(OntologyLoadError, match="code system 'nope'"):
        load_ontology(text)


def test_relation_kind_outside_the_core_enum_is_refused() -> None:
    """The design doc's YAML example uses `measured_on`; core only knows `measured_in`."""
    text = (
        "domain: d\nversion: '1'\nconcepts:\n  a: {label: A, kind: measure}\n  b: {label: B, kind: entity}\n"
        "relations:\n  - {src: a, dst: b, kind: measured_on, cardinality: '1:N'}\n"
    )
    with pytest.raises(OntologyLoadError) as error:
        load_ontology(text, source="bad.yaml")
    assert "rule: enum" in str(error.value) and "$.relations[0]" in str(error.value)


def test_relation_without_cardinality_is_refused() -> None:
    text = (
        "domain: d\nversion: '1'\nconcepts:\n  a: {label: A, kind: measure}\n  b: {label: B, kind: entity}\n"
        "relations:\n  - {src: a, dst: b, kind: belongs_to}\n"
    )
    with pytest.raises(OntologyLoadError, match="cardinality"):
        load_ontology(text)


def test_yaml_syntax_error_reports_position() -> None:
    with pytest.raises(OntologyLoadError) as error:
        load_ontology("domain: d\nversion: '1'\nconcepts: [oops\n", source="bad.yaml")
    assert "bad.yaml:" in str(error.value) and "YAML parse error" in str(error.value)


def test_empty_file_is_refused() -> None:
    with pytest.raises(OntologyLoadError, match="empty"):
        load_ontology("\n", source="empty.yaml")


def test_semantic_problems_are_left_to_core() -> None:
    """The loader checks structure; self-consistency is core/ontology.py's job."""
    text = (
        "domain: d\nversion: '1'\nconcepts:\n  a: {label: A, kind: measure}\n"
        'definitions:\n  ghost: {version: "1", text: t}\n'
    )
    ontology = load_ontology(text)
    assert "definition_orphan" in validate_ontology(ontology).codes()


def test_load_file_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "credit.yaml"
    path.write_text(VALID, encoding="utf-8")
    assert load_ontology_file(path).domain == "credit-risk"
    with pytest.raises(OntologyLoadError, match="cannot read"):
        load_ontology_file(tmp_path / "missing.yaml")
