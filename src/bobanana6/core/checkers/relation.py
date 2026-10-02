"""`relation` -- a comparison across columns or across files."""

from __future__ import annotations

from typing import Any

from ..model import Artifact, Candidate, Constraint
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "relation"

_OPS = ("eq", "ne", "lt", "le", "gt", "ge")


def _apply(op: str, left: Any, right: Any) -> bool:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    return left >= right


def _operand(candidate: Candidate, spec: Any) -> tuple[Artifact, str] | str:
    if not isinstance(spec, dict) or not isinstance(spec.get("column"), str):
        return "operand must be an object with a 'column'"
    name = spec.get("artifact")
    artifact = candidate.artifact(name) if isinstance(name, str) else None
    if artifact is None:
        if name is not None:
            return f"artifact {name!r} not found"
        if len(candidate.artifacts) != 1:
            return "operand must name an artifact when the block has multiple outputs"
        artifact = candidate.artifacts[0]
    if artifact.column(spec["column"]) is None:
        return f"column {spec['column']!r} not found in {artifact.name!r}"
    return artifact, spec["column"]


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    op = constraint.spec.get("op")
    if op not in _OPS:
        return fail(constraint, f"spec.op must be one of {_OPS}")
    left = _operand(candidate, constraint.spec.get("left"))
    if isinstance(left, str):
        return fail(constraint, f"left {left}")
    right = _operand(candidate, constraint.spec.get("right"))
    if isinstance(right, str):
        return fail(constraint, f"right {right}")
    left_artifact, left_column = left
    right_artifact, right_column = right
    if left_artifact.row_count != right_artifact.row_count:
        return fail(
            constraint,
            f"row counts differ: {left_artifact.name!r}={left_artifact.row_count} "
            f"{right_artifact.name!r}={right_artifact.row_count}",
        )
    tol = constraint.spec.get("tol", context.limits.tol)
    for index in range(left_artifact.row_count):
        left_value = left_artifact.rows[index].get(left_column)
        right_value = right_artifact.rows[index].get(right_column)
        if op in {"eq", "ne"} and isinstance(left_value, (int, float)) and isinstance(right_value, (int, float)):
            if not isinstance(tol, (int, float)) or tol < 0:
                return fail(constraint, f"tol must be a non-negative number, got {tol!r}")
            close = abs(left_value - right_value) <= tol
            holds = close if op == "eq" else not close
        else:
            try:
                holds = _apply(op, left_value, right_value)
            except TypeError as exc:
                return fail(constraint, f"row={index} cannot compare {left_value!r} {op} {right_value!r}: {exc}")
        if not holds:
            return fail(
                constraint,
                f"row={index} {left_artifact.name}.{left_column}={left_value!r} "
                f"{op} {right_artifact.name}.{right_column}={right_value!r} is false",
            )
    return ok(constraint, f"relation {op} holds for {left_artifact.row_count} rows")
