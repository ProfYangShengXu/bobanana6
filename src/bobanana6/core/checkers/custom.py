"""`custom` -- a named function from a registry.

Only functions the host registered are callable. Nothing in the constraint
bundle is executable: the bundle supplies a *name*, the host supplies the code.
That is the difference between a data constraint and running generated code.
"""

from __future__ import annotations

from typing import Any

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, fail, ok

KIND = "custom"


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    name = constraint.spec.get("check")
    if not isinstance(name, str) or not name:
        return fail(constraint, "spec.check must be the name of a registered function")
    function = context.custom_checks.get(name)
    if function is None:
        return fail(constraint, f"custom check {name!r} is not registered")
    try:
        result: Any = function(constraint, candidate, context)
    except Exception as exc:  # noqa: BLE001 - a broken checker is a visible failure, never a silent pass
        return fail(constraint, f"custom check {name!r} raised {type(exc).__name__}: {exc}")
    if isinstance(result, CheckOutcome):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        passed, facts = result
        if passed:
            return ok(constraint, *(str(f) for f in facts))
        return fail(constraint, *(str(f) for f in facts))
    if result:
        return ok(constraint, f"custom check {name!r} passed")
    return fail(constraint, f"custom check {name!r} failed")
