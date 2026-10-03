"""`schema` -- column names, types and nullability. Column order is not load
bearing unless the constraint explicitly asks for it."""

from __future__ import annotations

from ..model import Candidate, Constraint, is_missing
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "schema"


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    expected = constraint.spec.get("columns")
    if not isinstance(expected, list):
        return fail(constraint, "spec.columns must be a list of column descriptions")
    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return fail(constraint, f"artifact for target {constraint.target!r} not found")

    facts: list[str] = []
    for raw in expected:
        if not isinstance(raw, dict) or "name" not in raw:
            facts.append(f"malformed column spec: {raw!r}")
            continue
        name = str(raw["name"])
        column = artifact.column(name)
        if column is None:
            facts.append(f"missing column {name!r}")
            continue
        if "dtype" in raw and str(raw["dtype"]) != column.dtype:
            facts.append(f"column {name!r} dtype {column.dtype!r} != {raw['dtype']!r}")
        if raw.get("nullable", True) is False:
            if column.nullable:
                facts.append(f"column {name!r} is nullable but must not be")
            # ★ A04: "must not be null" is judged on the *cells*, not just on the
            #   declaration. A missing cell is a first-class state, so this is the
            #   judge that can tell "no value here" from a filled-in zero.
            for index, row in enumerate(artifact.rows):
                value = row.get(name)
                if is_missing(value) or value is None:
                    facts.append(f"row={index} col={name} holds no value but the column must not be nullable")
                    break

    if constraint.spec.get("order_sensitive") is True:
        expected_order = [str(raw["name"]) for raw in expected if isinstance(raw, dict) and "name" in raw]
        actual_order = [c.name for c in artifact.columns]
        if expected_order != actual_order:
            facts.append(f"column order {actual_order!r} != {expected_order!r}")

    if facts:
        return fail(constraint, *facts)
    return ok(constraint, f"schema of {artifact.name!r} satisfied")
