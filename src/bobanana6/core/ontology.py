"""Ontology reference validation.

This module checks *reference legality* only -- whether ids that are cited
exist, whether aliases are unambiguous, whether the classification hierarchy is
acyclic, and whether units/definitions/code systems resolve. It never
interprets domain meaning: deciding what "revenue" *means* is the job of the
party that wrote the ontology, not of the kernel.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from .model import Concept, Ontology, Unit

__all__ = [
    "OntologyReport",
    "OntologyViolation",
    "base_unit",
    "convert_value",
    "map_field",
    "ontology_report_codes",
    "pinned_version",
    "resolve_unit",
    "split_quantity",
    "validate_code",
    "validate_ontology",
]

#: ``<number><separator><unit symbol>`` -- the shape the Kubernetes ``Quantity``
#: pattern puts on the value itself (``实现规格 §5.0.1``②). A bare number carries
#: no unit; whether that is legal is the caller's judgment, not this parser's.
_QUANTITY = re.compile(r"^(?P<number>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(?P<symbol>[^\s\d].*)$")


def split_quantity(value: str) -> tuple[float, str] | None:
    """``"250 千元"`` -> ``(250.0, "千元")``; ``None`` when the value carries no unit.

    ★ The symbol must start with a **letter**. Without that rule the pattern backs
    off the number and reads the fractional part as the unit: ``"1200000.0"`` came
    back as ``(1200000.0, ".0")`` -- so a perfectly ordinary decimal was reported as
    a quantity in unit ``".0"``, and a value that simply has no unit got *rejected*
    instead of counted as unlabelled. Found by a real-model run (2026-10-03): a
    merge block was refused for ``value '1200000.0' declares unit '.0'``.
    """
    match = _QUANTITY.match(value.strip())
    if match is None:
        return None
    symbol = match.group("symbol").strip()
    if not symbol or not symbol[0].isalpha():
        return None
    try:
        number = float(match.group("number"))
    except ValueError:  # pragma: no cover - the pattern only admits number shapes
        return None
    return number, symbol


@dataclass(frozen=True)
class OntologyViolation:
    code: str
    detail: str
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class OntologyReport:
    violations: tuple[OntologyViolation, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.violations

    def codes(self) -> tuple[str, ...]:
        return tuple(v.code for v in self.violations)


def ontology_report_codes(report: OntologyReport) -> tuple[str, ...]:
    return report.codes()


def validate_ontology(ontology: Ontology) -> OntologyReport:
    """Check reference legality of concepts, definitions, units and relations.

    ``Ontology.calendar`` is deliberately **not inspected**. Trading-day
    semantics belong to time-window splitting (``external:decompose``), and the
    freshness check works in natural days, so no core judgment reads it. core
    only guarantees it is carried through unchanged -- neither validated nor
    interpreted here.
    """
    violations: list[OntologyViolation] = []

    concepts: Mapping[str, Concept] = ontology.concepts

    # -- definitions -------------------------------------------------------
    for key in sorted(ontology.definitions):
        definition = ontology.definitions[key]
        if definition.concept_id not in concepts:
            violations.append(
                OntologyViolation(
                    "definition_orphan",
                    f"definition {key!r} cites missing concept {definition.concept_id!r}",
                    (key, definition.concept_id),
                )
            )

    # -- units -------------------------------------------------------------
    for key in sorted(ontology.units):
        unit = ontology.units[key]
        if unit.concept_id not in concepts:
            violations.append(
                OntologyViolation(
                    "unit_orphan",
                    f"unit {key!r} cites missing concept {unit.concept_id!r}",
                    (key, unit.concept_id),
                )
            )

    # -- relations ---------------------------------------------------------
    for relation in ontology.relations:
        if relation.src not in concepts:
            violations.append(
                OntologyViolation("relation_src_missing", f"relation source {relation.src!r} is unknown", (relation.src,))
            )
        if relation.dst not in concepts:
            violations.append(
                OntologyViolation("relation_dst_missing", f"relation target {relation.dst!r} is unknown", (relation.dst,))
            )

    # -- alias ambiguity ---------------------------------------------------
    alias_owner: dict[str, str] = {}
    for key in sorted(concepts):
        concept = concepts[key]
        names = (concept.label, *concept.aliases)
        for alias in names:
            if alias == "":
                violations.append(OntologyViolation("empty_alias", f"concept {key!r} has an empty alias", (key,)))
                continue
            prior = alias_owner.get(alias)
            if prior is not None and prior != key:
                violations.append(
                    OntologyViolation(
                        "alias_conflict",
                        f"name {alias!r} is claimed by both {prior!r} and {key!r}",
                        (prior, key),
                    )
                )
            else:
                alias_owner[alias] = key

    # -- hierarchy: parent exists and is acyclic ---------------------------
    for key in sorted(concepts):
        parent = concepts[key].parent
        if parent is None:
            continue
        if parent == key:
            violations.append(OntologyViolation("parent_self", f"concept {key!r} is its own parent", (key,)))
        elif parent not in concepts:
            violations.append(OntologyViolation("parent_missing", f"concept {key!r} cites missing parent {parent!r}", (key, parent)))

    graph: dict[str, set[str]] = {}
    for key in sorted(concepts):
        parent = concepts[key].parent
        if parent is not None and parent in concepts and parent != key:
            graph.setdefault(key, set()).add(parent)
        else:
            graph.setdefault(key, set())
    sorter = TopologicalSorter({node: sorted(graph[node]) for node in sorted(graph)})
    try:
        sorter.prepare()
    except CycleError as exc:
        cycle = tuple(str(part) for part in exc.args[1]) if len(exc.args) > 1 else ()
        violations.append(OntologyViolation("parent_cycle", f"concept hierarchy has a cycle: {cycle}", cycle))

    # ontology.calendar is intentionally not visited here: external:decompose.
    return OntologyReport(violations=tuple(violations))


def map_field(field_name: str, ontology: Ontology) -> str | None:
    """Resolve a field name to a concept id via exact id, label or alias."""
    if field_name in ontology.concepts:
        return field_name
    matches: list[str] = []
    for key in sorted(ontology.concepts):
        concept = ontology.concepts[key]
        if field_name == concept.label or field_name in concept.aliases:
            matches.append(key)
    if not matches:
        return None
    return matches[0]


def resolve_unit(concept_id: str, ontology: Ontology) -> Unit | None:
    for key in sorted(ontology.units):
        unit = ontology.units[key]
        if unit.concept_id == concept_id:
            return unit
    return None


def base_unit(concept_id: str, ontology: Ontology) -> Unit | None:
    """The concept's canonical unit: the one whose symbol *is* the base.

    Several units may be declared for one concept (元 and 千元 both measure
    ``amount``); the base one is the unit every other one is expressed in, so it
    is the stable target a comparison converts to. Falls back to the first
    declared unit when nothing identifies itself as the base.
    """
    candidates = [unit for _, unit in sorted(ontology.units.items()) if unit.concept_id == concept_id]
    for unit in candidates:
        if unit.symbol == unit.base:
            return unit
    return candidates[0] if candidates else None


def convert_value(value: float, from_symbol: str, to_symbol: str, ontology: Ontology) -> float:
    """Convert between two declared units of the same base."""
    units = list(ontology.units.values())
    source = next((u for u in units if u.symbol == from_symbol), None)
    target = next((u for u in units if u.symbol == to_symbol), None)
    if source is None:
        raise KeyError(f"unknown source unit {from_symbol!r}")
    if target is None:
        raise KeyError(f"unknown target unit {to_symbol!r}")
    if source.base != target.base:
        raise ValueError(f"cannot convert {from_symbol!r} to {to_symbol!r}: different base")
    return value * source.scale / target.scale


def validate_code(value: str, code_system: str, ontology: Ontology) -> bool:
    allowed = ontology.code_systems.get(code_system)
    if allowed is None:
        return False
    return value in allowed


def pinned_version(concept_id: str, ontology: Ontology) -> str | None:
    """The version a concept is pinned to, or None when no definition exists.

    Definitions are keyed independently of concept id so several versions of
    the same concept can coexist; the pinned version is the one the ontology
    declares for that concept.
    """
    versions = sorted(
        definition.version for definition in ontology.definitions.values() if definition.concept_id == concept_id
    )
    if not versions:
        return None
    # ★ A02 后半: 本体【显式声明】的 pin 说了算（§5.0.1 的同源原则：权威定义在一处）。
    declared = ontology.pins.get(concept_id)
    if isinstance(declared, str) and declared in versions:
        return declared
    # An ambiguous pin is a defect the caller must resolve; surfacing the first
    # sorted version keeps the judgment deterministic either way.
    return versions[0]


def pin_is_explicit(concept_id: str, ontology: Ontology) -> bool:
    """★ 这一条 pin 是【本体声明的】还是【字典序兜底】的？兜底要说出来，不静默。"""
    declared = ontology.pins.get(concept_id, "")  # 与 pinned_version 的那一行刻意不同：见证变体只打前一处
    if not isinstance(declared, str) or not declared:
        return False
    return any(
        definition.concept_id == concept_id and definition.version == declared
        for definition in ontology.definitions.values()
    )
