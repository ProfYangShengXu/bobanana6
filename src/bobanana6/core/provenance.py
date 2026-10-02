"""Provenance chain validation.

A point is trustworthy only if it can be walked back to a snapshot that the
system actually holds. Two failure shapes are distinguished:

* a **break** -- the point cites a snapshot that is not in the registry
* an **unreachable point** -- the point has no citation at all

Removing one link in the middle therefore names exactly the missing snapshot
and the point that depended on it, which is what makes a broken chain
repairable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .model import Provenance, Snapshot

__all__ = [
    "ProvenanceBreak",
    "ProvenanceReport",
    "coverage",
    "unreachable_points",
    "validate_chain",
    "validate_points",
]


@dataclass(frozen=True)
class ProvenanceBreak:
    value_ref: str
    missing_snapshot: str


@dataclass(frozen=True)
class ProvenanceReport:
    breaks: tuple[ProvenanceBreak, ...] = ()
    unreachable_points: tuple[str, ...] = ()
    coverage: float = 1.0

    @property
    def ok(self) -> bool:
        return not self.breaks and not self.unreachable_points


def _reachable(provenance: Provenance, snapshots: Mapping[str, Snapshot]) -> bool:
    return provenance.snapshot_ref in snapshots


def validate_chain(
    provenances: Iterable[Provenance],
    snapshots: Mapping[str, Snapshot],
) -> ProvenanceReport:
    breaks: list[ProvenanceBreak] = []
    for provenance in sorted(provenances, key=lambda p: (p.value_ref, p.snapshot_ref)):
        if not _reachable(provenance, snapshots):
            breaks.append(ProvenanceBreak(value_ref=provenance.value_ref, missing_snapshot=provenance.snapshot_ref))
    return ProvenanceReport(breaks=tuple(breaks))


def unreachable_points(
    required: Iterable[str],
    provenances: Iterable[Provenance],
    snapshots: Mapping[str, Snapshot],
) -> tuple[str, ...]:
    """Points that no *reachable* provenance citation covers."""
    proven = {
        provenance.value_ref
        for provenance in provenances
        if _reachable(provenance, snapshots)
    }
    return tuple(point for point in required if point not in proven)


def coverage(
    required: Iterable[str],
    provenances: Iterable[Provenance],
    snapshots: Mapping[str, Snapshot],
) -> float:
    points = tuple(required)
    if not points:
        # No points is not full coverage; it is absence of evidence.
        return 0.0
    missing = unreachable_points(points, provenances, snapshots)
    return (len(points) - len(missing)) / len(points)


def validate_points(
    required: Iterable[str],
    provenances: Iterable[Provenance],
    snapshots: Mapping[str, Snapshot],
) -> ProvenanceReport:
    """Full check: breaks plus points that cannot be traced at all."""
    provenances = tuple(provenances)
    report = validate_chain(provenances, snapshots)
    points = tuple(required)
    return ProvenanceReport(
        breaks=report.breaks,
        unreachable_points=unreachable_points(points, provenances, snapshots),
        coverage=coverage(points, provenances, snapshots),
    )
