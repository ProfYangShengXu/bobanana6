"""`unit_consistency` -- a quantity passed between blocks keeps one unit, or the
conversion is declared. An undeclared disagreement is a conflict, not a pass."""

from __future__ import annotations

from ..model import Candidate, Constraint
from ..ontology import resolve_unit
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "unit_consistency"


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    concept = constraint.spec.get("concept")
    declared = constraint.spec.get("unit")
    if not isinstance(concept, str) or not concept:
        return fail(constraint, "spec.concept must be a non-empty string")
    if not isinstance(declared, str) or not declared:
        return fail(constraint, "spec.unit must be a non-empty string")
    if context.ontology is None:
        return fail(constraint, "ontology is required to judge unit consistency")

    unit = resolve_unit(concept, context.ontology)
    if unit is None:
        return fail(constraint, f"concept {concept!r} declares no unit in the ontology")

    facts: list[str] = []
    if declared != unit.symbol:
        if constraint.spec.get("conversion"):
            facts.append(f"declared {declared!r} != ontology {unit.symbol!r}, conversion declared")
        else:
            return fail(constraint, f"declared unit {declared!r} != ontology unit {unit.symbol!r} with no conversion")

    peer = context.peer_units.get(concept)
    if peer is not None and peer != declared and not constraint.spec.get("conversion"):
        return fail(constraint, f"peer block declares {peer!r} for {concept!r}, this block declares {declared!r}")

    if facts:
        return ok(constraint, *facts)
    return ok(constraint, f"unit {declared!r} consistent for {concept!r}")
