"""Shared plumbing for the per-kind checkers.

A checker is a pure function ``(constraint, candidate, context) -> CheckOutcome``.
It never raises for malformed input: an unknown kind, a missing check function
or a checker that blows up all become a failing outcome with a visible fact.
Silently swallowing a failure would be worse than failing loudly -- the kernel
must be able to answer "why was this block rejected".
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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
    #: ★ `#33`: `ref -> (冻结时的摘要, 工作区里那份的摘要)`，由外侧算好（core 不读盘）。
    #: 缺一项 = 那一份没被观测过 —— `frozen_files` 判据会具名失败，不静默通过。
    frozen_files: Mapping[str, tuple[str, str]] = field(default_factory=dict)
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
    #: ★ `#44`: 这一条失败**该记在谁头上**（空串 = 检查器没声明，交给归因的兜底顺序）。
    #: 目前只有一种：判据命令【自己】没跑起来（解释器/包缺失）⇒ ``"tool"``。
    #: 这是"谁来『说』自己的错"那条纪律的具体化：检查器知道，就由它声明。
    cause: str = ""


Checker = Callable[[Constraint, Candidate, CheckContext], CheckOutcome]


def ok(constraint: Constraint, *facts: str, divergences: tuple[Disagreement, ...] = (), soft_score: float | None = None) -> CheckOutcome:
    return CheckOutcome(
        constraint_id=constraint.id,
        ok=True,
        facts=tuple(facts),
        divergences=divergences,
        soft_score=soft_score,
    )


def fail(constraint: Constraint, *facts: str, cause: str = "") -> CheckOutcome:
    return CheckOutcome(constraint_id=constraint.id, ok=False, facts=tuple(facts), cause=cause)


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
        # ★ A08: `*` 说的是【装配后的全部产物】。以前只在"候选恰好一个产物"时才解析，
        #   于是它在多块装配下永远报 not found（#23 复核：与块数无关），全局不变量
        #   只能逐字段点名交付物表达 —— 表达力被削掉一半。
        #   这里把【同构】的多个产物合成一个：列名一致才合（部件 + 合并产物就是这种）；
        #   列名不一致时不猜（那不是同一张表，合并语义没有定义），仍然返回 None。
        return _combined_artifact(candidate.artifacts)
    return None


def _combined_artifact(artifacts: Sequence[Artifact]) -> Artifact | None:
    """All pieces as one table, when they really are one table (same columns)."""
    if not artifacts:
        return None
    first = tuple(column.name for column in artifacts[0].columns)
    if not first or any(tuple(column.name for column in a.columns) != first for a in artifacts):
        return None
    return Artifact(
        name="*",
        columns=artifacts[0].columns,
        rows=tuple(row for a in artifacts for row in a.rows),
        text=None,
    )
