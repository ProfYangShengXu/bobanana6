"""`provenance` -- can every point be traced back to a snapshot?

Missing rate above the limit is a failure. A block with no points at all is
also a failure: "nothing to trace" is not the same as "everything traced".
"""

from __future__ import annotations

from ..model import Artifact, Candidate, Constraint
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "provenance"


def required_refs(artifact: Artifact) -> tuple[str, ...]:
    """The set of traceable points a deliverable is expected to expose."""
    refs = [f"{artifact.name}:row={index}" for index in range(artifact.row_count)]
    if artifact.text is not None:
        refs.extend(f"{artifact.name}:line={index}" for index, line in enumerate(artifact.text.splitlines()) if line)
    return tuple(refs)


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return fail(constraint, f"artifact for target {constraint.target!r} not found")
    minimum = constraint.spec.get("min_coverage", context.limits.provenance_min_coverage)
    if not isinstance(minimum, (int, float)) or not 0.0 <= minimum <= 1.0:
        return fail(constraint, f"min_coverage must be in [0, 1], got {minimum!r}")

    points = required_refs(artifact)
    if not points:
        return fail(constraint, f"{artifact.name!r} exposes no traceable points")

    covered = {
        provenance.value_ref
        for provenance in candidate.provenance
        if provenance.snapshot_ref in context.snapshots
    }
    traced = [point for point in points if point in covered]
    coverage = len(traced) / len(points)
    if coverage < minimum:
        first_missing = next((point for point in points if point not in covered), None)
        unknown = tuple(
            sorted(
                {
                    provenance.snapshot_ref
                    for provenance in candidate.provenance
                    if provenance.snapshot_ref not in context.snapshots
                }
            )
        )
        detail = f"coverage {coverage:.4f} < required {minimum}; first missing {first_missing!r}"
        if unknown:
            detail += f"; unknown snapshots {unknown!r}"
        return fail(constraint, detail)
    return ok(constraint, f"coverage {coverage:.4f} >= required {minimum}")
