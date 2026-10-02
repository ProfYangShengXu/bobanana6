"""Assembly checking: do the pieces hold together as a whole?

Five things are checked, and every violation names the block(s) responsible --
an assembly failure that cannot name a block makes local retry impossible.

  1. global invariants   hard constraints whose target is ``*``
  2. cut completeness    missing block, unexpected piece, uncovered interface
  3. provenance chain    every point of the merged whole reaches a snapshot
  4. merge cost          merge estimate vs execution estimate
  5. partition conflict  same-level writes must be serial or partitioned

Capability matching (:func:`match_blocks`) is re-exported here as the spec's
assembly entry point, but the rule itself lives once, in ``capabilities.py``.
Duplicating it would let the two drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .capabilities import (
    BlockSpec,
    MatchResult,
    WorkerCapabilities,
    match,
)
from .checkers import check_kind
from .checkers.base import CheckContext
from .checkers.provenance import required_refs
from .model import (
    DEFAULT_LIMITS,
    Artifact,
    Candidate,
    ConstraintBundle,
    Limits,
    Provenance,
    Snapshot,
    SplitPlan,
    TaskContract,
)
from .provenance import unreachable_points
from .split import subtask_layers

__all__ = ["AssemblyReport", "AssemblyViolation", "check_assembly", "match_blocks"]

CHECKS: tuple[str, ...] = (
    "global_invariant",
    "cut_complete",
    "provenance_connected",
    "merge_cost",
    "partition_conflict",
    "capability_gap",
    "capability_short",
    "capability_over",
)


@dataclass(frozen=True)
class AssemblyViolation:
    check: str
    detail: str
    block_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssemblyReport:
    violations: tuple[AssemblyViolation, ...] = ()
    matched: MatchResult | None = None
    coverage: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.violations

    def codes(self) -> tuple[str, ...]:
        return tuple(v.check for v in self.violations)


def match_blocks(blocks: tuple[BlockSpec, ...], workers: tuple[WorkerCapabilities, ...]) -> MatchResult:
    return match(blocks, workers)


def check_assembly(
    plan: SplitPlan,
    contract: TaskContract,
    bundle: ConstraintBundle,
    pieces: Mapping[str, Artifact],
    context: CheckContext,
    *,
    provenance: tuple[Provenance, ...] = (),
    snapshots: Mapping[str, Snapshot] | None = None,
    limits: Limits = DEFAULT_LIMITS,
    blocks: tuple[BlockSpec, ...] | None = None,
    workers: tuple[WorkerCapabilities, ...] | None = None,
) -> AssemblyReport:
    snapshots = snapshots if snapshots is not None else context.snapshots
    violations: list[AssemblyViolation] = []

    def add(check: str, detail: str, *ids: str) -> None:
        violations.append(AssemblyViolation(check=check, detail=detail, block_ids=tuple(sorted(ids))))

    declared_outputs = {subtask.output: subtask.id for subtask in plan.subtasks}

    # -- 1. global invariants ---------------------------------------------
    synthetic = Candidate(
        subtask_id="*",
        source_id="assembly",
        artifacts=tuple(pieces[name] for name in sorted(pieces)),
        provenance=provenance,
    )
    for constraint in sorted(bundle.hard, key=lambda c: c.id):
        if constraint.target != "*":
            continue
        outcome = check_kind(constraint, synthetic, context)
        if not outcome.ok:
            add("global_invariant", f"{constraint.id}: {'; '.join(outcome.facts)}")

    # -- 2. cut completeness ----------------------------------------------
    for output in sorted(declared_outputs):
        if output not in pieces:
            add("cut_complete", f"output {output!r} is missing from the assembly", declared_outputs[output])
    for name in sorted(pieces):
        if name not in declared_outputs:
            add("cut_complete", f"piece {name!r} was not declared by any block")
    produced = set(declared_outputs)
    for deliverable in sorted(contract.required_deliverable_names):
        if deliverable not in produced:
            add("cut_complete", f"deliverable {deliverable!r} is produced by no block")

    # -- 3. provenance connectivity ---------------------------------------
    all_provenance = tuple(provenance)
    merged_points: list[str] = []
    for name in sorted(pieces):
        merged_points.extend(required_refs(pieces[name]))
    unreachable = unreachable_points(merged_points, all_provenance, snapshots)
    merged_coverage = 0.0 if not merged_points else (len(merged_points) - len(unreachable)) / len(merged_points)
    if unreachable:
        offenders = tuple(
            sorted(
                {
                    declared_outputs.get(name, name)
                    for name in pieces
                    if any(point.startswith(f"{name}:") for point in unreachable)
                }
            )
        )
        add(
            "provenance_connected",
            f"{len(unreachable)} points cannot reach a snapshot; first {unreachable[0]!r}",
            *offenders,
        )

    # -- 4. merge cost -----------------------------------------------------
    if plan.costs is None:
        add("merge_cost", "merge/execute cost estimates are not declared; cheapness cannot be proven")
    elif plan.costs.execute <= 0.0:
        add("merge_cost", "execution cost estimate must be > 0")
    else:
        ratio = plan.costs.merge / plan.costs.execute
        if ratio >= limits.merge_cost_ratio:
            add("merge_cost", f"merge/execute ratio {ratio:.4f} >= limit {limits.merge_cost_ratio}")

    # -- 5. partition conflict --------------------------------------------
    depth, _cycle = subtask_layers(plan)
    writes_by_layer: dict[int, list[str]] = {}
    partition_by_id: dict[str, str | None] = {}
    for subtask in sorted(plan.subtasks, key=lambda s: s.id):
        partition_by_id[subtask.id] = subtask.partition
        if subtask.mode == "write":
            writes_by_layer.setdefault(depth.get(subtask.id, 0), []).append(subtask.id)
    for layer in sorted(writes_by_layer):
        writers = writes_by_layer[layer]
        if len(writers) <= 1:
            continue
        partitions = [partition_by_id[writer] for writer in writers]
        if any(partition is None for partition in partitions) or len(set(partitions)) != len(partitions):
            add("partition_conflict", f"layer {layer} writes are not partition-isolated", *writers)

    # -- capability matching (single implementation in capabilities.py) ----
    matched: MatchResult | None = None
    if blocks is not None or workers is not None:
        matched = match(blocks or (), workers or ())
        for issue in matched.gaps:
            add("capability_gap", issue.detail, issue.block_id)
        for issue in matched.shorts:
            add("capability_short", issue.detail)
        for issue in matched.overs:
            add("capability_over", issue.detail, issue.block_id)

    ordered = sorted(violations, key=lambda v: (v.check, v.block_ids, v.detail))
    return AssemblyReport(violations=tuple(ordered), matched=matched, coverage=merged_coverage)
