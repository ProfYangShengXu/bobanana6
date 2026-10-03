"""`invariant` -- a row-local expression over the row's fields.

The expression is parsed and evaluated against an allow-list of node types:
no attribute access, no subscripting into objects, no imports, no lambdas, no
calls other than a small set of pure builtins. Model-generated code is never
executed; only a data expression the caller already owns.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping
from typing import Any

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "invariant"

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}
_COMPARE_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_CALLABLE = {"abs": abs, "min": min, "max": max, "len": len, "round": round, "sum": sum}
_CONSTANTS = {"true": True, "false": False, "none": None}


class InvariantError(ValueError):
    """The expression is outside the allowed subset or references unknown data."""

    #: The *constraint* is malformed, not the data it is judging: the request
    #: shape has to change before anything is worth re-running.
    #: Declared here rather than inferred from wording (see agentloop/errors.py).
    reason = "format_error"


def evaluate(expression: str, row: Mapping[str, Any]) -> Any:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise InvariantError(f"cannot parse expression: {exc}") from exc
    return _eval(tree.body, row)


def _eval(node: ast.AST, row: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in row:
            return row[node.id]
        lowered = node.id.lower()
        if lowered in _CONSTANTS:
            return _CONSTANTS[lowered]
        raise InvariantError(f"unknown identifier {node.id!r}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise InvariantError(f"operator {type(node.op).__name__} is not allowed")
        return op(_eval(node.left, row), _eval(node.right, row))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise InvariantError(f"unary operator {type(node.op).__name__} is not allowed")
        return op(_eval(node.operand, row))
    if isinstance(node, ast.BoolOp):
        values = [_eval(value, row) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise InvariantError("boolean operator is not allowed")
    if isinstance(node, ast.Compare):
        left = _eval(node.left, row)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _COMPARE_OPS.get(type(op_node))
            if op is None:
                raise InvariantError(f"comparison {type(op_node).__name__} is not allowed")
            right = _eval(comparator, row)
            if not op(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, row) if _eval(node.test, row) else _eval(node.orelse, row)
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_eval(element, row) for element in node.elts]
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _CALLABLE:
            raise InvariantError("only a fixed set of pure builtins may be called")
        if node.keywords:
            raise InvariantError("keyword arguments are not allowed")
        return _CALLABLE[node.func.id](*[_eval(arg, row) for arg in node.args])
    raise InvariantError(f"expression node {type(node).__name__} is not allowed")


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    expression = constraint.spec.get("expr")
    if not isinstance(expression, str) or not expression:
        return fail(constraint, "spec.expr must be a non-empty string")
    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return fail(constraint, f"artifact for target {constraint.target!r} not found")
    for index, row in enumerate(artifact.rows):
        try:
            result = evaluate(expression, row)
        except InvariantError as exc:
            return fail(constraint, f"row={index} {exc}")
        if not result:
            return fail(constraint, f"row={index} invariant {expression!r} is false")
    return ok(constraint, f"invariant {expression!r} holds for {artifact.row_count} rows")
