"""`unit_consistency` -- a quantity passed between blocks keeps one unit, or the
conversion is declared. An undeclared disagreement is a conflict, not a pass.

★ A10: the unit lives *with the value* (``实现规格 §5.0.1`` ②, the Kubernetes
``Quantity`` pattern: ``"100m"`` = 0.1). Declarations alone cannot tell whether a
candidate's numbers were normalised, so the check also reads the unit a value
carries. A value with no unit is still unjudgeable -- that residual is reported,
never silently treated as consistent.
"""

from __future__ import annotations

from typing import Any

from ..model import Candidate, Constraint, is_missing
from ..ontology import resolve_unit, split_quantity
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "unit_consistency"

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
    #: A value nobody labelled is not "in the declared unit" -- it is a value whose
    #: unit rests on the ontology's concept declaration. Being explicit about that
    #: is the whole point: a schema may accept the assumption, a task that needs
    #: the unit stated must not have it made silently.
    unlabelled = 0
    for column in _concept_columns(constraint, candidate, context):
        column_unit = _column_unit(artifact, column)
        if column_unit:
            if column_unit == declared:
                facts.append(f"column {column!r} declares its unit {column_unit!r}")
            elif constraint.spec.get("conversion") and _shares_base(column_unit, unit.symbol, context):
                facts.append(f"column {column!r} declares {column_unit!r}, conversion declared")
            else:
                return fail(
                    constraint,
                    f"column {column!r} declares unit {column_unit!r}, but {concept!r} is declared as {declared!r}",
                )
        for index, row in enumerate(artifact.rows if artifact is not None else ()):
            value = row.get(column)
            if is_missing(value):
                continue  # absence is A04's business, not a unit question
            split = split_quantity(value) if isinstance(value, str) else None
            if split is None:
                # ★ A bare number is the common case here: the value carries no unit
                #   and the column declared none, so this cell's unit would rest on
                #   the ontology. Counted, never silently accepted.
                if not column_unit:
                    unlabelled += 1
                continue
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

    if unlabelled:
        if constraint.spec.get("unit_source") == "explicit":
            # ★ The unit may not be *assumed* here: the task demanded it be stated.
            #   This is (b) -- refuse rather than present an unlabelled number as if
            #   its unit were known.
            return fail(
                constraint,
                f"{unlabelled} value(s) in {concept!r} carry no unit and the column declares none; "
                "this constraint requires the unit to be explicit",
            )
        # ★ (c): the assumption is *said*, never silent -- and it is deliberately not
        #   called a declaration, because nobody declared it.
        facts.append(
            f"{unlabelled} value(s) in {concept!r} carry no unit; the ontology declaration "
            f"{declared!r} applies to them (assumption, not a declaration)"
        )

    if facts:
        return ok(constraint, *facts)
    return ok(constraint, f"unit {declared!r} consistent for {concept!r}")


def _column_unit(artifact: Any, column: str) -> str:
    """The unit the producer declared for this column, or ``""``."""
    if artifact is None:
        return ""
    declared_column = artifact.column(column)
    return declared_column.unit if declared_column is not None else ""


def _shares_base(symbol: str, target: str, context: CheckContext) -> bool:
    if context.ontology is None:
        return False
    units = {other.symbol: other for other in context.ontology.units.values()}
    source = units.get(symbol)
    destination = units.get(target)
    return source is not None and destination is not None and source.base == destination.base
