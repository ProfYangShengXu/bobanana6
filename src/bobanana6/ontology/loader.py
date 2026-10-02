"""Load an ontology *file* into :class:`bobanana6.core.model.Ontology`.

This is the environment side of the ontology: it reads YAML, checks the file's
*structure* against ``schemas/ontology.schema.json``, and constructs the frozen
data objects ``core/`` consumes. It deliberately does **not** decide whether the
ontology is self-consistent -- dangling concept references, unit conflicts and
definition versions belong to ``core/ontology.py`` (AGENTS.md §1.5: the two are
named alike on purpose and must not merge).

Every failure names the file, the JSON path inside it, and which rule rejected
it, so a bad ontology is repairable without guessing (AGENTS.md §5.2).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from ..core.model import Concept, Definition, Ontology, Relation, Unit

__all__ = ["SCHEMA_PATH", "OntologyLoadError", "load_ontology", "load_ontology_file"]

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "ontology.schema.json"


class OntologyLoadError(ValueError):
    """A load failure that says *where* the file went wrong and *which rule* caught it."""


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate(data: Any, source: str) -> None:
    validator = Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.absolute_path))
    if not errors:
        return
    first = errors[0]
    raise OntologyLoadError(f"{source}:{first.json_path}: {first.message} (rule: {first.validator})")


def _parse(text: str, source: str) -> Any:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # malformed YAML: report the mark
        mark = getattr(exc, "problem_mark", None)
        where = f"{mark.line + 1}:{mark.column + 1}" if mark is not None else "<unknown>"
        problem = getattr(exc, "problem", None) or str(exc).strip()
        raise OntologyLoadError(f"{source}:{where}: YAML parse error: {problem}") from exc
    if data is None:
        raise OntologyLoadError(f"{source}: the ontology file is empty")
    return data


def _concepts(data: Mapping[str, Any], domain: str) -> dict[str, Concept]:
    concepts: dict[str, Concept] = {}
    for key in sorted(data["concepts"]):
        raw = data["concepts"][key]
        try:
            concepts[key] = Concept(
                id=key,
                label=raw["label"],
                kind=raw["kind"],
                domain=raw.get("domain", domain),
                parent=raw.get("parent"),
                aliases=tuple(raw.get("aliases", ())),
            )
        except ValueError as exc:
            raise OntologyLoadError(f"{key}: invalid concept: {exc} (rule: concept)") from exc
    return concepts


def _units(data: Mapping[str, Any], source: str) -> dict[str, Unit]:
    units: dict[str, Unit] = {}
    for key in sorted(data.get("units", {})):
        raw = data["units"][key]
        concept_id = raw.get("concept_id", key)
        try:
            units[key] = Unit(concept_id=concept_id, symbol=raw["symbol"], scale=float(raw["scale"]), base=raw["base"])
        except ValueError as exc:
            raise OntologyLoadError(f"{source}:$.units['{key}']: invalid unit: {exc} (rule: unit)") from exc
    return units


def _check_concept_references(data: Mapping[str, Any], units: Mapping[str, Unit], source: str) -> None:
    """A concept may name a ``unit`` / ``code_system``; a dangling name must not vanish.

    The model has no per-concept field for these, so the loader is their only
    reader -- which is exactly why it refuses an unresolvable name instead of
    dropping it silently.
    """
    symbols = {unit.symbol for unit in units.values()}
    systems = set(data.get("code_systems", {}))
    for key in sorted(data["concepts"]):
        raw = data["concepts"][key]
        unit_name = raw.get("unit")
        if unit_name is not None and unit_name not in symbols:
            raise OntologyLoadError(
                f"{source}:$.concepts['{key}'].unit: unit {unit_name!r} is not defined in `units` (rule: reference)"
            )
        system_name = raw.get("code_system")
        if system_name is not None and system_name not in systems:
            raise OntologyLoadError(
                f"{source}:$.concepts['{key}'].code_system: code system {system_name!r} is not defined in "
                "`code_systems` (rule: reference)"
            )


def _definitions(data: Mapping[str, Any], source: str) -> dict[str, Definition]:
    definitions: dict[str, Definition] = {}
    for key in sorted(data.get("definitions", {})):
        raw = data["definitions"][key]
        concept_id = raw.get("concept_id", key)
        try:
            definitions[key] = Definition(
                concept_id=concept_id,
                version=raw["version"],
                text=raw["text"],
                formula=raw.get("formula"),
                edge_cases=tuple(raw.get("edge_cases", ())),
            )
        except ValueError as exc:
            raise OntologyLoadError(f"{source}:$.definitions['{key}']: invalid definition: {exc} (rule: definition)") from exc
    return definitions


def _relations(data: Mapping[str, Any], source: str) -> tuple[Relation, ...]:
    relations: list[Relation] = []
    for index, raw in enumerate(data.get("relations", ())):
        try:
            relations.append(Relation(src=raw["src"], dst=raw["dst"], kind=raw["kind"], cardinality=raw["cardinality"]))
        except ValueError as exc:
            raise OntologyLoadError(f"{source}:$.relations[{index}]: invalid relation: {exc} (rule: relation)") from exc
    return tuple(relations)


def load_ontology(text: str, *, source: str = "<string>") -> Ontology:
    """Parse + structurally validate an ontology document. The result is *not* frozen.

    Freezing before workers start is a separate, explicit step: call
    ``Ontology.freeze()`` (``core/split.py::semantics_frozen`` checks it).
    """
    data = _parse(text, source)
    _validate(data, source)

    domain = data["domain"]
    concepts = _concepts(data, domain)
    units = _units(data, source)
    _check_concept_references(data, units, source)
    return Ontology(
        domain=domain,
        version=data["version"],
        concepts=concepts,
        definitions=_definitions(data, source),
        units=units,
        relations=_relations(data, source),
        code_systems={key: tuple(value) for key, value in data.get("code_systems", {}).items()},
        calendar=dict(data.get("calendar", {})),
    )


def load_ontology_file(path: str | Path) -> Ontology:
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise OntologyLoadError(f"{file}: cannot read the ontology file: {exc}") from exc
    return load_ontology(text, source=str(file))
