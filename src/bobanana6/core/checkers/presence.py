"""`presence` -- the deliverable exists and reaches a minimum row count."""

from __future__ import annotations

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "presence"


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return fail(constraint, f"artifact for target {constraint.target!r} not found")
    min_rows = constraint.spec.get("min_rows", 0)
    if not isinstance(min_rows, int) or min_rows < 0:
        return fail(constraint, "spec.min_rows must be a non-negative integer")
    if artifact.row_count < min_rows:
        return fail(constraint, f"{artifact.name!r} has {artifact.row_count} rows, needs >= {min_rows}")
    return ok(constraint, f"{artifact.name!r} present with {artifact.row_count} rows")
