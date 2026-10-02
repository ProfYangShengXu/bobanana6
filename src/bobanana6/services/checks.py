"""Independent judgment for one block, and the evidence a real command produces.

``LoopIO.verify`` needs a :class:`GateVerdict`, and the whole point of the
completion gate is that the verdict does **not** come from the worker's own
report (架构设计 §7.3). Two independent sources feed it here:

  * ``core/kernel.py::admit`` -- the constraint bundle judged against the
    candidate the worker submitted;
  * an optional shell command declared by a ``custom`` constraint, whose exit
    status is recorded through ``gate.record_evidence`` and turned into a
    verdict by ``gate.evaluate``. Recording is what makes "checked, then
    changed" detectable, and ``attribute_exit`` is what stops a piped or
    backgrounded command from donating its exit code to the pipeline.

The command comes from the contract (a human wrote it), never from model output:
``custom`` supplies a *name*, the host supplies the code.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..agentloop.gate import (
    GateVerdict,
    VerificationEvidence,
    evaluate,
    record_evidence,
)
from ..core.checkers.base import CheckContext
from ..core.kernel import admit
from ..core.model import Candidate, Constraint, ConstraintBundle, Subtask, Verdict

__all__ = ["BlockJudge", "RecordedEvidence", "shell_command_check"]

#: The name a host registers to let a ``custom`` constraint run a shell command.
SHELL_COMMAND_CHECK = "shell_command"


@dataclass
class RecordedEvidence:
    """Evidence recorded during one verification, in order."""

    records: list[VerificationEvidence] = field(default_factory=list)

    def append(self, evidence: VerificationEvidence) -> None:
        self.records.append(evidence)

    def last(self) -> VerificationEvidence | None:
        return self.records[-1] if self.records else None


def shell_command_check(
    evidence: RecordedEvidence,
    clock: Callable[[], float],
    *,
    timeout_s: float = 120.0,
) -> Callable[[Constraint, Candidate, CheckContext], tuple[bool, list[str]]]:
    """Build the ``custom`` check that runs a command and records what happened."""

    def run(constraint: Constraint, candidate: Candidate, context: CheckContext) -> tuple[bool, list[str]]:
        command = constraint.spec.get("command")
        if not isinstance(command, str) or not command:
            return False, ["spec.command must be a non-empty string"]
        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, [f"command timed out after {timeout_s}s: {command}"]
        output = (completed.stdout or "") + (completed.stderr or "")
        recorded = record_evidence(command, int(completed.returncode), output, created_at=clock())
        evidence.append(recorded)
        facts = [f"{command!r} exited {recorded.exit_code}"]
        if completed.returncode != 0:
            return False, [*facts, output.strip()[:400] or "<no output>"]
        if not recorded.attributable:
            # ★ An exit status that a pipe or a background job could have donated
            #   is not evidence that this command succeeded.
            return False, [*facts, "exit status is not attributable to a single command"]
        return True, facts

    return run


@dataclass
class BlockJudge:
    """Judge one block, and say whether the system may call it done."""

    bundle: ConstraintBundle
    subtask: Subtask
    context: CheckContext
    required_scope: str = "full"
    #: Newest edit time of the block's produced files; injected, never read here
    #: (``gate.is_stale`` compares timestamps, and a judge that reads the clock
    #: cannot be replayed).
    last_edit_at: float = 0.0
    evidence: RecordedEvidence = field(default_factory=RecordedEvidence)
    #: The verdict the loop blocked on, so the runner can report it without
    #: re-running the checks (a command check must not execute twice).
    last_verdict: Verdict | None = None

    def admit(self, candidate: Candidate) -> Verdict:
        return admit(self.bundle, candidate, self.context)

    def verify(self, candidate: Candidate) -> GateVerdict:
        """The gate the loop blocks on. Without evidence this is the kernel's call."""
        verdict = self.admit(candidate)
        self.last_verdict = verdict
        if not verdict.ok:
            return GateVerdict(status="failed", scope=self.required_scope)
        recorded = self.evidence.last()
        if recorded is None:
            return GateVerdict(status="passed", scope=self.required_scope)
        return evaluate(recorded, required_scope=self.required_scope, last_edit_at=self.last_edit_at)


def verdict_summary(verdict: Verdict, judge: BlockJudge) -> dict[str, Any]:
    """A report-shaped view of one verdict (``Verdict.evidence`` is external:report)."""
    return {
        "ok": verdict.ok,
        "failed": list(verdict.failed),
        "evidence": dict(verdict.evidence),
        "soft_scores": dict(verdict.soft_scores),
        "gate": judge.required_scope,
    }
