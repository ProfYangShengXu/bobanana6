"""`coverage` -- required sources must be present, and a missing source must be
declared rather than silently dropped."""

from __future__ import annotations

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "coverage"

#: Human sentence for the self-check's third state: what is covered is read off
#: the candidate's citations and claims, both of which the producer builds at
#: block end -- a pre-submission product carries neither, so "covered=[]" is not
#: a finding. Declared by the checker at its own failure point; read only by the
#: read-only self-check. Judgment never reads it.
PENDING_INPUT = (
    "此刻验不了：这条核的是哪些来源被产物引用到了，引用和来源声明在交答案、块末对账时才生成；"
    "磁盘上的产物还没带它们，所以现在量到的覆盖来源恒为空。交答案时判定会照常做。"
)


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
        # ★ Citations and claims are both built at block end; with neither on
        #   hand the "covered" set is empty by construction, not by fault.
        pending = PENDING_INPUT if not candidate.provenance and not candidate.claims else ""
        return fail(
            constraint,
            f"missing sources {undeclared!r} are not declared; covered={sorted(covered)!r}",
            pending_input=pending,
        )
    return ok(constraint, f"required sources present or declared missing: {missing!r}")
