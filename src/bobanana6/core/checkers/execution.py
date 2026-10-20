"""`execution` -- the artifact must **behave**, not merely exist.

Every other static kind answers a question about the candidate's data: a column
is present, a value is in range, a citation resolves. None of them can answer
"does this program run?", so a contract could only say "the file exists and is
big enough" -- and a complete-looking skeleton that never answers a single request
passed.

The acceptance condition this kind adds is deliberately narrow:

```
spec.command       the one command to run (contract-authored, never model text)
spec.expect_stdout the one marker that must appear in the command's **stdout**
spec.timeout_s     optional, seconds
```

★ **The exit code is not part of the verdict.** It is the command's own status,
not the acceptance conclusion -- `cat test.py` exits 0 while testing nothing.
A command that prints the marker exits nonzero still passes; a command that exits
0 without the marker still fails. The runner still reports the exit code as a
fact, so a human can see it.

``fail_to_pass`` / ``pass_to_pass`` are accepted as authoring declarations (the
skeleton must be red; the regression set must stay green). They describe how the
check itself was validated, not a property of *this* candidate, so they do not
decide it -- ``expect_stdout`` is the only evidence. They are still shape-checked
here so a typo fails loudly instead of sitting in the contract unread.

The command is run by the host, outside ``core/``: this module is a pure
dispatcher, exactly like ``custom``. No registered runner means the constraint
cannot be decided -- a failing outcome, never a silent pass.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..model import Candidate, Constraint
from .base import CheckContext, CheckOutcome, fail

KIND = "execution"

#: The name a host registers in ``CheckContext.custom_checks`` to run the command.
RUNNER = "execution"


def _shape_problem(constraint: Constraint) -> str:
    command = constraint.spec.get("command")
    if not isinstance(command, str) or not command.strip():
        return "spec.command must be a non-empty string"
    marker = constraint.spec.get("expect_stdout")
    if not isinstance(marker, str) or not marker:
        return "spec.expect_stdout must be a non-empty string (an empty marker is contained in everything)"
    for key in ("fail_to_pass", "pass_to_pass"):
        raw = constraint.spec.get(key)
        if raw is None:
            continue
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return f"spec.{key} must be a list of non-empty strings"
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                return f"spec.{key} must be a list of non-empty strings"
    return ""


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    problem = _shape_problem(constraint)
    if problem:
        return fail(constraint, problem)
    runner = context.custom_checks.get(RUNNER)
    if runner is None:
        # ★ Fail closed: "nobody ran it" is not "it passed". The host that builds
        #   the context must register the runner (``services/checks.py``).
        return fail(
            constraint,
            "no execution runner is registered on this host; refusing to pass a check that never ran",
            cause="tool",
        )
    try:
        outcome = runner(constraint, candidate, context)
    except Exception as exc:  # noqa: BLE001 - a broken runner is a visible failure, never a silent pass
        return fail(constraint, f"execution runner raised {type(exc).__name__}: {exc}", cause="tool")
    if not isinstance(outcome, CheckOutcome):
        return fail(
            constraint,
            f"execution runner returned {type(outcome).__name__}, not a CheckOutcome",
            cause="tool",
        )
    return outcome
