"""Capability dictionary and the zero-model matching judgment.

The dictionary converges three spec sections (§2.1 read/write ratio, §6.4 split
dimension, §7.6 tool concurrency) onto one place: a capability belongs to one of
three tiers, and the write tier is the hard ceiling on parallelism.

Discipline: only *capabilities* live here, never *identities*. No role name
("extractor", "auditor", ...) may appear, and no function in this module reads
a block or worker name for anything other than deterministic ordering.

Determinism: every set is iterated through ``sorted()``. Python randomizes str
hashing per process, so an unsorted frozenset iteration would assign blocks to
different workers across runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .model import DeliverableSpec

__all__ = [
    "CAPABILITY_TIERS",
    "Assignment",
    "BlockSpec",
    "Capability",
    "CapabilityTier",
    "MatchIssue",
    "MatchIssueRecord",
    "MatchResult",
    "WorkerCapabilities",
    "WorkerLimits",
    "check_over_permission",
    "coverage_holds",
    "match",
    "tier_of",
]


class CapabilityTier(StrEnum):
    READ_ONLY = "read_only"  # parallel by nature
    COMPUTE = "compute"  # parallel, no shared state
    WRITE = "write"  # needs a partition: the parallelism ceiling


class Capability(StrEnum):
    # Tier 1 -- read only
    FETCH = "fetch"
    READ = "read"
    SEARCH = "search"
    # Tier 2 -- compute
    COMPUTE = "compute"
    TRANSFORM = "transform"
    SELF_CHECK = "self_check"
    # Tier 3 -- write
    WRITE_DELIVERABLE = "write_deliverable"
    EDIT = "edit"


#: The single source of truth for which tier a capability sits in.
CAPABILITY_TIERS: dict[Capability, CapabilityTier] = {
    Capability.FETCH: CapabilityTier.READ_ONLY,
    Capability.READ: CapabilityTier.READ_ONLY,
    Capability.SEARCH: CapabilityTier.READ_ONLY,
    Capability.COMPUTE: CapabilityTier.COMPUTE,
    Capability.TRANSFORM: CapabilityTier.COMPUTE,
    Capability.SELF_CHECK: CapabilityTier.COMPUTE,
    Capability.WRITE_DELIVERABLE: CapabilityTier.WRITE,
    Capability.EDIT: CapabilityTier.WRITE,
}

_TIER_RANK = {CapabilityTier.READ_ONLY: 0, CapabilityTier.COMPUTE: 1, CapabilityTier.WRITE: 2}


def tier_of(capability: Capability) -> CapabilityTier:
    return CAPABILITY_TIERS[capability]


class MatchIssue(StrEnum):
    """Three disjoint outcomes, isomorphic to the divergence classification."""

    CAPABILITY_GAP = "gap"  # nobody can provide it -> configuration problem
    CAPABILITY_SHORT = "short"  # a provider exists but concurrency is short
    CAPABILITY_OVER = "over"  # a provider exists but is over-qualified -> cost


@dataclass(frozen=True)
class WorkerLimits:
    max_output: int = 0
    concurrency: int = 1
    context: int = 0

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("WorkerLimits.concurrency must be >= 1")


@dataclass(frozen=True)
class BlockSpec:
    block_id: str
    dimension: str
    needs: frozenset[Capability]
    produces: DeliverableSpec
    partition: str | None = None


@dataclass(frozen=True)
class WorkerCapabilities:
    worker_id: str
    provides: frozenset[Capability]
    limits: WorkerLimits = WorkerLimits()


@dataclass(frozen=True)
class Assignment:
    block_id: str
    worker_id: str
    surplus: tuple[Capability, ...] = ()


@dataclass(frozen=True)
class MatchIssueRecord:
    issue: MatchIssue
    block_id: str
    detail: str
    capabilities: tuple[Capability, ...] = ()


@dataclass(frozen=True)
class MatchResult:
    assignments: tuple[Assignment, ...] = ()
    issues: tuple[MatchIssueRecord, ...] = ()

    @property
    def gaps(self) -> tuple[MatchIssueRecord, ...]:
        return tuple(i for i in self.issues if i.issue is MatchIssue.CAPABILITY_GAP)

    @property
    def shorts(self) -> tuple[MatchIssueRecord, ...]:
        return tuple(i for i in self.issues if i.issue is MatchIssue.CAPABILITY_SHORT)

    @property
    def overs(self) -> tuple[MatchIssueRecord, ...]:
        return tuple(i for i in self.issues if i.issue is MatchIssue.CAPABILITY_OVER)

    @property
    def coverage_ok(self) -> bool:
        return not self.gaps

    @property
    def over_rate(self) -> float:
        if not self.assignments:
            return 0.0
        return len(self.overs) / len(self.assignments)


def _tier_rank(worker: WorkerCapabilities) -> int:
    if not worker.provides:
        return -1
    return max(_TIER_RANK[tier_of(cap)] for cap in sorted(worker.provides, key=lambda c: c.value))


def coverage_holds(blocks: tuple[BlockSpec, ...], workers: tuple[WorkerCapabilities, ...]) -> bool:
    """Criterion 1 from the spec: every block's needs are covered by the union."""
    provided: set[Capability] = set()
    for worker in workers:
        provided |= worker.provides
    return all(set(block.needs) <= provided for block in blocks)


def check_over_permission(
    declared: frozenset[Capability], used: frozenset[Capability]
) -> tuple[Capability, ...]:
    """Criterion 2: did the worker act beyond what it declared it provides?

    The runtime caller lives outside ``core/``; the judgment itself is pure and
    lives here so it can be asserted deterministically.
    """
    return tuple(sorted(set(used) - set(declared), key=lambda c: c.value))


def _concurrency_capacity(
    capability: Capability,
    workers: tuple[WorkerCapabilities, ...],
    blocks: tuple[BlockSpec, ...],
) -> int:
    providers = [w for w in workers if capability in w.provides]
    raw = sum(w.limits.concurrency for w in providers)
    if tier_of(capability) is not CapabilityTier.WRITE:
        return raw
    # Same-level writes must serialize unless their partitions are mutually
    # exclusive, so at most one concurrent writer per distinct partition.
    needing = [b for b in blocks if capability in b.needs]
    partitions = {b.partition for b in needing}
    if None in partitions or len(partitions) <= 1:
        return min(raw, 1)
    return min(raw, len(partitions))


def match(
    blocks: tuple[BlockSpec, ...],
    workers: tuple[WorkerCapabilities, ...],
) -> MatchResult:
    """Pure, deterministic needs<->provides matching with three issue classes."""
    ordered_blocks = sorted(blocks, key=lambda b: b.block_id)
    ordered_workers = sorted(workers, key=lambda w: w.worker_id)

    issues: list[MatchIssueRecord] = []
    assignments: list[Assignment] = []
    provided_union: set[Capability] = set()
    for worker in ordered_workers:
        provided_union |= worker.provides

    for block in ordered_blocks:
        candidates = [w for w in ordered_workers if set(block.needs) <= w.provides]
        if not candidates:
            missing = tuple(sorted(set(block.needs) - provided_union, key=lambda c: c.value))
            issues.append(
                MatchIssueRecord(
                    issue=MatchIssue.CAPABILITY_GAP,
                    block_id=block.block_id,
                    detail="no worker provides the required capabilities",
                    capabilities=missing,
                )
            )
            continue
        best = min(
            candidates,
            key=lambda w: (len(set(w.provides) - set(block.needs)), _tier_rank(w), w.worker_id),
        )
        surplus = tuple(sorted(set(best.provides) - set(block.needs), key=lambda c: c.value))
        assignments.append(Assignment(block_id=block.block_id, worker_id=best.worker_id, surplus=surplus))
        if surplus:
            issues.append(
                MatchIssueRecord(
                    issue=MatchIssue.CAPABILITY_OVER,
                    block_id=block.block_id,
                    detail=f"assigned over-qualified worker {best.worker_id!r}",
                    capabilities=surplus,
                )
            )

    # Concurrency shortfall is a property of a capability, not of a block.
    needed_caps: set[Capability] = set()
    for block in ordered_blocks:
        needed_caps |= set(block.needs)
    for capability in sorted(needed_caps, key=lambda c: c.value):
        if not any(capability in w.provides for w in ordered_workers):
            # No provider at all is a gap, already reported above. Keeping the
            # classes disjoint matters: "short" must mean a provider exists.
            continue
        demand = sum(1 for b in ordered_blocks if capability in b.needs)
        capacity = _concurrency_capacity(capability, ordered_workers, ordered_blocks)
        if demand > capacity:
            issues.append(
                MatchIssueRecord(
                    issue=MatchIssue.CAPABILITY_SHORT,
                    block_id="*",
                    detail=f"{capability.value}: demand {demand} > capacity {capacity}",
                    capabilities=(capability,),
                )
            )

    return MatchResult(assignments=tuple(assignments), issues=tuple(issues))
