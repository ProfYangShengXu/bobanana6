"""`corroboration` -- enough independent sources, and disagreement is a signal.

Two distinct outcomes are kept apart on purpose:

* too few independent sources  -> the constraint **fails**
* enough sources but different values -> the constraint **passes** and emits a
  ``Conflict``, leaving both claims and their provenance for the record

Collapsing the second case into a failure would destroy the difference between
"this is wrong" and "these two authorities disagree".
"""

from __future__ import annotations

from ..model import Candidate, Conflict, Constraint
from ..ontology import map_field, pinned_version
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "corroboration"


#: Human sentence for the self-check's third state: the claims this check compares
#: travel in the block's answer and are only read at submission, so a product
#: that sits on disk before then carries none of them -- "0 sources" is not a
#: finding. Declared by the checker at its own failure point; read only by the
#: read-only self-check. Judgment never reads it.
PENDING_INPUT = (
    "此刻验不了：这条要比对的是各个来源各自的说法，它们写在交上去的答案里、交答案时才被读到；"
    "磁盘上的产物不带它们，所以现在量到的来源数恒为 0。交答案时判定会照常做。"
)


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    field = constraint.spec.get("field")
    if not isinstance(field, str) or not field:
        return fail(constraint, "spec.field must be a non-empty string")
    minimum = constraint.spec.get("min_sources", context.limits.corroboration_min_sources)
    if not isinstance(minimum, int) or minimum < 1:
        return fail(constraint, f"min_sources must be an integer >= 1, got {minimum!r}")

    claims = [claim for claim in candidate.claims if claim.field == field]
    sources = sorted({claim.source_id for claim in claims})
    if len(sources) < minimum:
        # ★ No claims at all = the block-end input is missing (not a finding).
        #   Claims that exist but do not cover this field are a real failure.
        pending = PENDING_INPUT if not candidate.claims else ""
        return fail(
            constraint,
            f"field {field!r} has {len(sources)} sources, needs >= {minimum}",
            pending_input=pending,
        )

    distinct_values = {repr(claim.value) for claim in claims}
    if len(distinct_values) <= 1:
        return ok(constraint, f"field {field!r} corroborated by {len(sources)} sources with equal values")

    concept_id = field
    definition_version = ""
    if context.ontology is not None:
        mapped = map_field(field, context.ontology)
        if mapped is not None:
            concept_id = mapped
        pin = pinned_version(concept_id, context.ontology)
        if pin is not None:
            definition_version = pin
    ordered = sorted(claims, key=lambda claim: (claim.source_id, repr(claim.value)))
    conflict = Conflict(
        concept_id=concept_id,
        definition_version=definition_version,
        claims=tuple((claim.source_id, claim.value, claim.provenance) for claim in ordered),
    )
    return ok(
        constraint,
        f"field {field!r} has {len(sources)} sources with differing values -> conflict recorded",
        divergences=(conflict,),
    )
