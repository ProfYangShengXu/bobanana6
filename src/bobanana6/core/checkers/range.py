"""`range` -- per-value domain check; reports the first out-of-range position."""

from __future__ import annotations

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "range"


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    column = constraint.spec.get("column")
    if not isinstance(column, str) or not column:
        return fail(constraint, "spec.column must be a non-empty string")
    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return fail(constraint, f"artifact for target {constraint.target!r} not found")
    if artifact.column(column) is None:
        return fail(constraint, f"column {column!r} not found in {artifact.name!r}")

    low = constraint.spec.get("min")
    high = constraint.spec.get("max")
    if low is None and high is None:
        return fail(constraint, "spec must declare min and/or max")
    tol = constraint.spec.get("tol", context.limits.tol)
    if not isinstance(tol, (int, float)) or tol < 0:
        return fail(constraint, f"tol must be a non-negative number, got {tol!r}")

    for index, row in enumerate(artifact.rows):
        value = row.get(column)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return fail(constraint, f"row={index} col={column} value={value!r} is not numeric")
        if low is not None and value < low - tol:
            return fail(constraint, f"row={index} col={column} value={value!r} < min {low!r} (tol {tol!r})")
        if high is not None and value > high + tol:
            return fail(constraint, f"row={index} col={column} value={value!r} > max {high!r} (tol {tol!r})")
    return ok(constraint, f"all values of {column!r} within [{low!r}, {high!r}]")
