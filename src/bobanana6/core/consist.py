"""Self-consistency: may this split plan and bundle be released to the cluster?

Any hit here blocks the launch. The check list is the spec's; where a rule also
appears in the split or assembly checkers it is *called* rather than
re-implemented, so a rule change cannot make one gate pass what another
rejects.

Several checks fail closed when their inputs are absent: "no budget given" is a
violation, not a silent pass. A gate that cannot see the numbers is not a gate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import inf
from typing import Any

from .model import (
    DEFAULT_LIMITS,
    Budget,
    Constraint,
    ConstraintBundle,
    Limits,
    Ontology,
    SplitPlan,
    TaskContract,
)
from .ontology import map_field
from .split import check_split

__all__ = ["CHECKS", "ConsistencyReport", "ConsistencyViolation", "check_consistency"]

#: The fourteen checks from the spec's self-consistency section.
CHECKS: tuple[str, ...] = (
    "contradiction",
    "tool_missing",
    "unreachable",
    "orphan_target",
    "stale_blind",
    "unsatisfiable_tol",
    "cycle",
    "missing_iface",
    "unverifiable",
    "write_serial",
    "merge_expensive",
    "concept_unmapped",
    "def_version_mix",
    "unit_conflict",
)


@dataclass(frozen=True)
class ConsistencyViolation:
    check: str
    detail: str
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConsistencyReport:
    violations: tuple[ConsistencyViolation, ...] = ()

    @property
    def start_allowed(self) -> bool:
        return not self.violations

    def codes(self) -> tuple[str, ...]:
        return tuple(v.check for v in self.violations)


def _bounds(spec: Mapping[str, Any]) -> tuple[float, float]:
    low = spec.get("min", -inf)
    high = spec.get("max", inf)
    return float(low), float(high)


def _check_contradiction(bundle: ConstraintBundle) -> list[ConsistencyViolation]:
    violations: list[ConsistencyViolation] = []
    groups: dict[tuple[str, str], list[Constraint]] = {}
    for constraint in bundle.hard:
        if constraint.kind != "range":
            continue
        column = constraint.spec.get("column")
        if isinstance(column, str):
            groups.setdefault((constraint.target, column), []).append(constraint)
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda c: c.id)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                low_a, high_a = _bounds(items[i].spec)
                low_b, high_b = _bounds(items[j].spec)
                if min(high_a, high_b) < max(low_a, low_b):
                    violations.append(
                        ConsistencyViolation(
                            "contradiction",
                            f"{items[i].id} and {items[j].id} have disjoint ranges on {key[1]!r}",
                            (items[i].id, items[j].id),
                        )
                    )
    return violations


def _tool_missing(bundle: ConstraintBundle, available: frozenset[str] | None) -> list[ConsistencyViolation]:
    if available is None:
        return [ConsistencyViolation("tool_missing", "available capability inventory was not provided")]
    violations: list[ConsistencyViolation] = []
    for constraint in sorted(bundle.hard, key=lambda c: c.id):
        required = constraint.spec.get("requires")
        if not isinstance(required, (list, tuple)):
            continue
        for name in sorted(str(item) for item in required):
            if name not in available:
                violations.append(
                    ConsistencyViolation(
                        "tool_missing", f"{constraint.id} requires unavailable capability {name!r}", (constraint.id, name)
                    )
                )
    return violations


def _unreachable(bundle: ConstraintBundle, budget: Budget | None) -> list[ConsistencyViolation]:
    if budget is None:
        return [ConsistencyViolation("unreachable", "budget was not provided; required scale cannot be checked")]
    violations: list[ConsistencyViolation] = []
    limits = {
        "requires_calls": budget.max_calls,
        "requires_output": budget.max_output,
        "requires_wall_s": budget.max_wall_s,
    }
    for constraint in sorted(bundle.hard, key=lambda c: c.id):
        for key in sorted(limits):
            required = constraint.spec.get(key)
            if isinstance(required, (int, float)) and required > limits[key]:
                violations.append(
                    ConsistencyViolation(
                        "unreachable",
                        f"{constraint.id} needs {key}={required} > available {limits[key]}",
                        (constraint.id,),
                    )
                )
    return violations


def _orphan_target(plan: SplitPlan, contract: TaskContract, bundle: ConstraintBundle) -> list[ConsistencyViolation]:
    known = set(contract.deliverable_names) | {subtask.id for subtask in plan.subtasks} | {"*"}
    return [
        ConsistencyViolation("orphan_target", f"{constraint.id} targets unknown {constraint.target!r}", (constraint.id,))
        for constraint in sorted(bundle.hard, key=lambda c: c.id)
        if constraint.target not in known
    ]


def _stale_blind(bundle: ConstraintBundle) -> list[ConsistencyViolation]:
    violations: list[ConsistencyViolation] = []
    for entry in sorted(set(bundle.blind)):
        for constraint in sorted(bundle.hard, key=lambda c: c.id):
            cited = {str(value) for value in constraint.spec.values() if isinstance(value, str)}
            if constraint.target == entry or entry in cited:
                violations.append(
                    ConsistencyViolation(
                        "stale_blind", f"blind entry {entry!r} is already covered by {constraint.id}", (entry, constraint.id)
                    )
                )
    return violations


def _unsatisfiable_tol(bundle: ConstraintBundle) -> list[ConsistencyViolation]:
    violations: list[ConsistencyViolation] = []
    for constraint in sorted(bundle.hard, key=lambda c: c.id):
        tol = constraint.spec.get("tol")
        precision = constraint.spec.get("precision")
        if isinstance(tol, (int, float)) and isinstance(precision, (int, float)) and tol < precision:
            violations.append(
                ConsistencyViolation(
                    "unsatisfiable_tol",
                    f"{constraint.id} tol {tol} is below data precision {precision}",
                    (constraint.id,),
                )
            )
    return violations


def _missing_iface(plan: SplitPlan, bundle: ConstraintBundle) -> list[ConsistencyViolation]:
    produced = {subtask.output: subtask.id for subtask in plan.subtasks}
    consumed: set[str] = set()
    for subtask in plan.subtasks:
        consumed |= set(subtask.inputs)
    constrained = set()
    for constraint in bundle.hard:
        constrained.add(constraint.target)
    violations: list[ConsistencyViolation] = []
    for output in sorted(produced):
        if output in consumed and output not in constrained and produced[output] not in constrained:
            violations.append(
                ConsistencyViolation(
                    "missing_iface",
                    f"interface {output!r} is consumed by another block but carries no constraint",
                    (produced[output],),
                )
            )
    return violations


def _concept_unmapped(
    plan: SplitPlan, bundle: ConstraintBundle, ontology: Ontology | None
) -> list[ConsistencyViolation]:
    if ontology is None:
        return [ConsistencyViolation("concept_unmapped", "ontology was not provided")]
    candidates: set[str] = set()
    for constraint in bundle.hard:
        for key in ("concept", "field"):
            value = constraint.spec.get(key)
            if isinstance(value, str):
                candidates.add(value)
    for subtask in plan.subtasks:
        candidates |= set(subtask.output_fields)
    unmapped = bundle.unmapped_fields
    violations: list[ConsistencyViolation] = []
    for name in sorted(candidates):
        if name in unmapped:
            continue
        if name in ontology.concepts:
            continue
        if map_field(name, ontology) is not None:
            continue
        violations.append(
            ConsistencyViolation(
                "concept_unmapped", f"concept {name!r} is not in the ontology and is not declared unmapped", (name,)
            )
        )
    return violations


def _def_version_mix(bundle: ConstraintBundle) -> list[ConsistencyViolation]:
    grouped: dict[str, set[str]] = {}
    for constraint in bundle.hard:
        if constraint.kind != "definition_pin":
            continue
        concept = constraint.spec.get("concept")
        version = constraint.spec.get("version")
        if isinstance(concept, str) and isinstance(version, str):
            grouped.setdefault(concept, set()).add(version)
    violations: list[ConsistencyViolation] = []
    for concept in sorted(grouped):
        if len(grouped[concept]) > 1:
            violations.append(
                ConsistencyViolation(
                    "def_version_mix",
                    f"concept {concept!r} is pinned to versions {sorted(grouped[concept])!r}",
                    (concept, *sorted(grouped[concept])),
                )
            )
    return violations


def _unit_conflict(bundle: ConstraintBundle) -> list[ConsistencyViolation]:
    grouped: dict[str, set[str]] = {}
    for constraint in bundle.hard:
        if constraint.kind != "unit_consistency" or constraint.spec.get("conversion"):
            continue
        concept = constraint.spec.get("concept")
        unit = constraint.spec.get("unit")
        if isinstance(concept, str) and isinstance(unit, str):
            grouped.setdefault(concept, set()).add(unit)
    violations: list[ConsistencyViolation] = []
    for concept in sorted(grouped):
        if len(grouped[concept]) > 1:
            violations.append(
                ConsistencyViolation(
                    "unit_conflict",
                    f"concept {concept!r} is declared with conflicting units {sorted(grouped[concept])!r}",
                    (concept, *sorted(grouped[concept])),
                )
            )
    return violations


def check_consistency(
    plan: SplitPlan,
    contract: TaskContract,
    bundle: ConstraintBundle,
    ontology: Ontology | None,
    *,
    budget: Budget | None = None,
    available_capabilities: Iterable[str] | None = None,
    limits: Limits = DEFAULT_LIMITS,
    frozen_hash: str | None = None,
) -> ConsistencyReport:
    violations: list[ConsistencyViolation] = []

    # Reuse, never re-implement, the rules shared with split/assemble.
    split_report = check_split(plan, contract, bundle, ontology, frozen_hash=frozen_hash, limits=limits)
    mapped = {"verifiable": "unverifiable", "write_serial": "write_serial", "merge_cheap": "merge_expensive", "cycle": "cycle"}
    for violation in split_report.violations:
        target = mapped.get(violation.check)
        if target is not None:
            violations.append(ConsistencyViolation(target, violation.detail, violation.subtask_ids))

    violations.extend(_check_contradiction(bundle))
    available = None if available_capabilities is None else frozenset(str(c) for c in available_capabilities)
    violations.extend(_tool_missing(bundle, available))
    violations.extend(_unreachable(bundle, budget))
    violations.extend(_orphan_target(plan, contract, bundle))
    violations.extend(_stale_blind(bundle))
    violations.extend(_unsatisfiable_tol(bundle))
    violations.extend(_missing_iface(plan, bundle))
    violations.extend(_concept_unmapped(plan, bundle, ontology))
    violations.extend(_def_version_mix(bundle))
    violations.extend(_unit_conflict(bundle))

    ordered = sorted(violations, key=lambda v: (v.check, v.refs, v.detail))
    return ConsistencyReport(violations=tuple(ordered))
