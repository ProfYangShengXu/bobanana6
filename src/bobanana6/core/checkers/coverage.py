"""`coverage` -- required sources must be present, and a missing source must be
declared rather than silently dropped."""

from __future__ import annotations

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "coverage"


def _covered_sources(candidate: Candidate) -> frozenset[str]:
    covered = {provenance.snapshot_ref for provenance in candidate.provenance}
    covered |= {claim.source_id for claim in candidate.claims}
    return frozenset(covered)


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    required = constraint.spec.get("required_sources")
    if not isinstance(required, (list, tuple)) or not required:
        return fail(constraint, "spec.required_sources must be a non-empty list")
    declared_missing = constraint.spec.get("declared_missing", [])  # blame: fail-open — ★该改，但受"不动 core/"约束（§五①）：省略即最宽松口径（什么都没声明也算覆盖齐）
    if not isinstance(declared_missing, (list, tuple)):
        return fail(constraint, "spec.declared_missing must be a list")

    covered = _covered_sources(candidate)
    missing = tuple(sorted(str(name) for name in required if str(name) not in covered))
    undeclared = tuple(name for name in missing if name not in {str(d) for d in declared_missing})
    if undeclared:
        return fail(
            constraint,
            f"missing sources {undeclared!r} are not declared; covered={sorted(covered)!r}",
        )
    return ok(constraint, f"required sources present or declared missing: {missing!r}")
