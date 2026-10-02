"""`code_valid` -- categorical values must come from a declared code system."""

from __future__ import annotations

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "code_valid"


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    column = constraint.spec.get("column")
    system = constraint.spec.get("code_system")
    if not isinstance(column, str) or not column:
        return fail(constraint, "spec.column must be a non-empty string")
    if not isinstance(system, str) or not system:
        return fail(constraint, "spec.code_system must be a non-empty string")
    if context.ontology is None:
        return fail(constraint, "ontology is required to judge a code system")
    allowed = context.ontology.code_systems.get(system)
    if allowed is None:
        return fail(constraint, f"code system {system!r} is not declared in the ontology")

    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return fail(constraint, f"artifact for target {constraint.target!r} not found")
    if artifact.column(column) is None:
        return fail(constraint, f"column {column!r} not found in {artifact.name!r}")

    permitted = frozenset(allowed)
    for index, row in enumerate(artifact.rows):
        value = row.get(column)
        if value not in permitted:
            return fail(constraint, f"row={index} col={column} value={value!r} is not in {sorted(permitted)!r}")
    return ok(constraint, f"all values of {column!r} are in code system {system!r}")
