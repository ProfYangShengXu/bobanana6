"""Registry of per-kind checkers.

One kind, one module. The registry is a plain dict so an unknown kind cannot
slip through as a silent pass: ``check_kind`` returns a failing outcome with a
fact naming the unknown kind.

``JUDGMENT_ORDER`` fixes the order from the spec: presence and shape first, then
semantics, then values, then provenance, then units and definitions. Shape
errors are cheaper to read than value errors, and a wrong concept reference
makes every value comparison meaningless.
"""

from __future__ import annotations

from ..checkers import range as range_checker
from ..model import Candidate, Constraint
from . import (
    code_valid,
    concept_ref,
    corroboration,
    coverage,
    custom,
    definition_pin,
    freshness,
    invariant,
    presence,
    provenance,
    relation,
    schema,
    unit_consistency,
)
from .base import CheckContext, Checker, CheckOutcome, fail

__all__ = ["CHECKS", "JUDGMENT_ORDER", "check_kind"]

CHECKS: dict[str, Checker] = {
    schema.KIND: schema.check,
    presence.KIND: presence.check,
    range_checker.KIND: range_checker.check,
    invariant.KIND: invariant.check,
    relation.KIND: relation.check,
    provenance.KIND: provenance.check,
    corroboration.KIND: corroboration.check,
    freshness.KIND: freshness.check,
    coverage.KIND: coverage.check,
    concept_ref.KIND: concept_ref.check,
    unit_consistency.KIND: unit_consistency.check,
    definition_pin.KIND: definition_pin.check,
    code_valid.KIND: code_valid.check,
    custom.KIND: custom.check,
}

JUDGMENT_ORDER: tuple[str, ...] = (
    "presence",
    "schema",
    "concept_ref",
    "range",
    "invariant",
    "relation",
    "provenance",
    "corroboration",
    "freshness",
    "coverage",
    "unit_consistency",
    "definition_pin",
    "code_valid",
    "custom",
)


def check_kind(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    checker = CHECKS.get(constraint.kind)
    if checker is None:
        return fail(constraint, f"unknown constraint kind {constraint.kind!r}")
    return checker(constraint, candidate, context)
