"""`concept_ref` -- a field must map to shared semantics, or be declared
unmapped on purpose. Mapping nothing by default is the failure this guards."""

from __future__ import annotations

from ..model import Candidate, Constraint
from ..ontology import map_field
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "concept_ref"


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    field = constraint.spec.get("field")
    if not isinstance(field, str) or not field:
        return fail(constraint, "spec.field must be a non-empty string")
    if context.ontology is None:
        return fail(constraint, "ontology is required to judge a concept reference")

    pinned = constraint.spec.get("concept")
    if pinned is not None:
        if pinned not in context.ontology.concepts:
            return fail(constraint, f"concept {pinned!r} is not in the ontology")
        return ok(constraint, f"field {field!r} pinned to existing concept {pinned!r}")

    mapped = map_field(field, context.ontology)
    if mapped is None:
        if field in context.unmapped_fields:
            return ok(constraint, f"field {field!r} is explicitly declared unmapped")
        return fail(constraint, f"field {field!r} maps to no concept and is not declared unmapped")
    if field in context.unmapped_fields:
        return ok(constraint, f"field {field!r} maps to {mapped!r} but was also declared unmapped")
    return ok(constraint, f"field {field!r} maps to {mapped!r}")
