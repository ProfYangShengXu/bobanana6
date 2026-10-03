"""Shared plumbing for the per-kind checkers.

A checker is a pure function ``(constraint, candidate, context) -> CheckOutcome``.
It never raises for malformed input: an unknown kind, a missing check function
or a checker that blows up all become a failing outcome with a visible fact.
Silently swallowing a failure would be worse than failing loudly -- the kernel
must be able to answer "why was this block rejected".
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..model import (
    DEFAULT_LIMITS,
    Artifact,
    Candidate,
    Constraint,
    Disagreement,
    Limits,
    Ontology,
    Snapshot,
    SnapshotBody,
)

__all__ = [
    "CheckContext",
    "CheckOutcome",
    "Checker",
    "artifact_for",
    "fail",
    "ok",
]


@dataclass(frozen=True)
class CheckContext:
    """Everything a checker may read that does not live on the candidate."""

    ontology: Ontology | None = None
    snapshots: Mapping[str, Snapshot] = field(default_factory=dict)
    #: ref -> the frozen rows, read by the outside reader (A11). Empty means the
    #: content check degrades to the name check; it never means "verified".
    snapshot_bodies: Mapping[str, SnapshotBody] = field(default_factory=dict)
    unmapped_fields: frozenset[str] = frozenset()
    # concept_id -> unit symbol declared by a peer block, for cross-block units.
    peer_units: Mapping[str, str] = field(default_factory=dict)
    now: str | None = None
    limits: Limits = DEFAULT_LIMITS
    custom_checks: Mapping[str, Callable[..., Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckOutcome:
    constraint_id: str
    ok: bool
    facts: tuple[str, ...] = ()
    divergences: tuple[Disagreement, ...] = ()
    soft_score: float | None = None


Checker = Callable[[Constraint, Candidate, CheckContext], CheckOutcome]


def ok(constraint: Constraint, *facts: str, divergences: tuple[Disagreement, ...] = (), soft_score: float | None = None) -> CheckOutcome:
    return CheckOutcome(
        constraint_id=constraint.id,
        ok=True,
        facts=tuple(facts),
        divergences=divergences,
        soft_score=soft_score,
    )


def fail(constraint: Constraint, *facts: str) -> CheckOutcome:
    return CheckOutcome(constraint_id=constraint.id, ok=False, facts=tuple(facts))


def artifact_for(constraint: Constraint, candidate: Candidate) -> Artifact | None:
    """Locate the artifact a constraint is about.

    ``Constraint.target`` names either a deliverable, a subtask id or ``*``.
    Exactly-one-artifact fallback keeps single-output blocks usable while still
    refusing to guess when several outputs exist.
    """
    target = constraint.target
    named = candidate.artifact(target)
    if named is not None:
        return named
    if target in ("*", candidate.subtask_id):
        if len(candidate.artifacts) == 1:
            return candidate.artifacts[0]
        return None
    return None
