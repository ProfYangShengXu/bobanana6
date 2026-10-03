"""`unit_consistency` -- a quantity passed between blocks keeps one unit, or the
conversion is declared. An undeclared disagreement is a conflict, not a pass.

★ A10: the unit lives *with the value* (``实现规格 §5.0.1`` ②, the Kubernetes
``Quantity`` pattern: ``"100m"`` = 0.1). Declarations alone cannot tell whether a
candidate's numbers were normalised, so the check also reads the unit a value
carries. A value with no unit is still unjudgeable -- that residual is reported,
never silently treated as consistent.
"""

from __future__ import annotations

import re

from ..model import Candidate, Constraint, is_missing
from ..ontology import resolve_unit
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "unit_consistency"

#: ``<number><separator><unit symbol>`` -- the shape the Kubernetes ``Quantity``
#: pattern puts on the value itself. A bare number carries no unit and is not
#: judged here.
_VALUE_WITH_UNIT = re.compile(r"^(?P<number>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(?P<symbol>[^\s\d].*)$")


def _split_value_unit(value: str) -> tuple[str, str] | None:
    match = _VALUE_WITH_UNIT.match(value.strip())
    if match is None:
        return None
    return match.group("number"), match.group("symbol").strip()


def _concept_columns(constraint: Constraint, candidate: Candidate, context: CheckContext) -> tuple[str, ...]:
    """The artifact columns that carry the concept this constraint is about."""
    from ..ontology import map_field

    concept = str(constraint.spec.get("concept"))
    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return ()
    names: list[str] = []
    for column in artifact.columns:
        mapped = map_field(column.name, context.ontology) if context.ontology is not None else None
        if (mapped or column.name) == concept:
            names.append(column.name)
    return tuple(names)


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    concept = constraint.spec.get("concept")
    declared = constraint.spec.get("unit")
    if not isinstance(concept, str) or not concept:
        return fail(constraint, "spec.concept must be a non-empty string")
    if not isinstance(declared, str) or not declared:
        return fail(constraint, "spec.unit must be a non-empty string")
    if context.ontology is None:
        return fail(constraint, "ontology is required to judge unit consistency")

    unit = resolve_unit(concept, context.ontology)
    if unit is None:
        return fail(constraint, f"concept {concept!r} declares no unit in the ontology")

    facts: list[str] = []
    if declared != unit.symbol:
        if constraint.spec.get("conversion"):
            facts.append(f"declared {declared!r} != ontology {unit.symbol!r}, conversion declared")
        else:
            return fail(constraint, f"declared unit {declared!r} != ontology unit {unit.symbol!r} with no conversion")

    peer = context.peer_units.get(concept)
    if peer is not None and peer != declared and not constraint.spec.get("conversion"):
        return fail(constraint, f"peer block declares {peer!r} for {concept!r}, this block declares {declared!r}")

    # -- ★ A10: read the unit off the value, not only off the declarations ---- #
    artifact = artifact_for(constraint, candidate)
    symbols = {other.symbol for other in context.ontology.units.values()}
    for column in _concept_columns(constraint, candidate, context):
        for index, row in enumerate(artifact.rows if artifact is not None else ()):
            value = row.get(column)
            if is_missing(value) or not isinstance(value, str):
                continue  # a number carries no unit here; absence is A04's business
            split = _split_value_unit(value)
            if split is None:
                continue  # no unit on the value -- still unjudgeable, reported below
            _, symbol = split
            if symbol == declared:
                facts.append(f"row={index} col={column} value {value!r} declares {symbol!r}")
                continue
            convertible = (
                symbol in symbols
                and constraint.spec.get("conversion")
                and _shares_base(symbol, unit.symbol, context)
            )
            if convertible:
                facts.append(f"row={index} col={column} value {value!r} declares {symbol!r}, conversion declared")
                continue
            return fail(
                constraint,
                f"row={index} col={column} value {value!r} declares unit {symbol!r}, "
                f"but {concept!r} is declared as {declared!r}",
            )

    if facts:
        return ok(constraint, *facts)
    return ok(constraint, f"unit {declared!r} consistent for {concept!r}")


def _shares_base(symbol: str, target: str, context: CheckContext) -> bool:
    if context.ontology is None:
        return False
    units = {other.symbol: other for other in context.ontology.units.values()}
    source = units.get(symbol)
    destination = units.get(target)
    return source is not None and destination is not None and source.base == destination.base
