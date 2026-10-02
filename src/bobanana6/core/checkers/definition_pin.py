"""`definition_pin` -- the same concept id must be pinned to one definition
version. A version drift is a ``DefinitionMismatch`` (a defect to normalize),
never a ``Conflict`` (a signal to record)."""

from __future__ import annotations

from ..model import Candidate, Constraint, DefinitionMismatch
from ..ontology import pinned_version
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "definition_pin"


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    concept = constraint.spec.get("concept")
    version = constraint.spec.get("version")
    if not isinstance(concept, str) or not concept:
        return fail(constraint, "spec.concept must be a non-empty string")
    if not isinstance(version, str) or not version:
        return fail(constraint, "spec.version must be a non-empty string")
    if context.ontology is None:
        return fail(constraint, "ontology is required to judge a definition pin")

    pin = pinned_version(concept, context.ontology)
    if pin is None:
        return fail(constraint, f"concept {concept!r} has no definition to pin to")
    if pin != version:
        mismatch = DefinitionMismatch(
            concept_id=concept,
            versions=tuple(sorted({pin, version})),
            parties=(candidate.subtask_id,),
        )
        return CheckOutcome(
            constraint_id=constraint.id,
            ok=False,
            facts=(f"concept {concept!r} is pinned to {pin!r}, candidate declares {version!r}",),
            divergences=(mismatch,),
        )
    return ok(constraint, f"concept {concept!r} pinned to {version!r}")
