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
        return fail(constraint, f"field {field!r} has {len(sources)} sources, needs >= {minimum}")

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
