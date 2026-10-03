"""Provenance chain validation.

A point is trustworthy only if it can be walked back to a snapshot that the
system actually holds. Three failure shapes are distinguished:

* a **break** -- the point cites a snapshot that is not in the registry
* a **content break** -- the cited row does not exist in that snapshot, or the
  value does not match it (A11): the name being known is not the same as the
  reference being real
* an **unreachable point** -- the point has no citation at all

Removing one link in the middle therefore names exactly the missing snapshot
and the point that depended on it, which is what makes a broken chain
repairable.

The content half needs the frozen body, which core never opens. The reader
outside hands it over as data (:class:`SnapshotBody`); without a body the check
honestly degrades to the name check rather than pretending to have verified
something.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .model import Provenance, Snapshot, SnapshotBody

__all__ = [
    "ProvenanceBreak",
    "ProvenanceReport",
    "content_break",
    "coverage",
    "row_of",
    "same_value",
    "unreachable_points",
    "validate_chain",
    "validate_points",
]


@dataclass(frozen=True)
class ProvenanceBreak:
    value_ref: str
    #: The snapshot the break is about -- unknown to the registry, or a body that
    #: does not hold the cited row.
    missing_snapshot: str
    #: Why it broke, in words. Empty for the plain "snapshot not in the registry".
    detail: str = ""


@dataclass(frozen=True)
class ProvenanceReport:
    breaks: tuple[ProvenanceBreak, ...] = ()
    unreachable_points: tuple[str, ...] = ()
    coverage: float = 1.0

    @property
    def ok(self) -> bool:
        return not self.breaks and not self.unreachable_points


def row_of(value_ref: str) -> int | None:
    """The row index a ``<artifact>:row=<n>`` reference names, when it names one."""
    marker = ":row="
    if marker not in value_ref:
        return None
    _, _, raw = value_ref.partition(marker)
    try:
        return int(raw)
    except ValueError:
        return None


def same_value(left: object, right: object) -> bool:
    """Whether a candidate's value and the snapshot's are the same value.

    The carrier keeps strings (a CSV cell) while an answer may carry a number,
    so ``"0.011"`` and ``0.011`` are the same value. Anything that is not a
    number on both sides is compared as text. One rule, used both by the check
    and by the producer when it inherits a merge's citations.
    """
    if left == right:
        return True
    try:
        return float(str(left)) == float(str(right))
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


def content_break(provenance: Provenance, bodies: Mapping[str, SnapshotBody]) -> str:
    """Why this citation does not replay against the snapshot's rows (``""`` = it does).

    The cited row must exist. When the point also names the artifact row it
    supplies, the row index is the artifact's own; the snapshot row is
    ``source_row`` when the producer said which one it read, and otherwise the
    same index (the read-block convention).
    """
    body = bodies.get(provenance.snapshot_ref)
    if body is None:
        return ""  # no content on hand: the name check is all we can honestly claim
    row = provenance.source_row if provenance.source_row is not None else row_of(provenance.value_ref)
    if row is None:
        return ""
    if row < 0 or row >= len(body.rows):
        return (
            f"{provenance.value_ref!r} cites row {row} of {provenance.snapshot_ref!r}, "
            f"which holds {len(body.rows)} row(s)"
        )
    return ""


def _reachable(
    provenance: Provenance,
    snapshots: Mapping[str, Snapshot],
    bodies: Mapping[str, SnapshotBody],
) -> bool:
    return provenance.snapshot_ref in snapshots and content_break(provenance, bodies) == ""


def validate_chain(
    provenances: Iterable[Provenance],
    snapshots: Mapping[str, Snapshot],
    bodies: Mapping[str, SnapshotBody] | None = None,
) -> ProvenanceReport:
    bodies = bodies or {}
    breaks: list[ProvenanceBreak] = []
    for provenance in sorted(provenances, key=lambda p: (p.value_ref, p.snapshot_ref)):
        if provenance.snapshot_ref not in snapshots:
            breaks.append(ProvenanceBreak(value_ref=provenance.value_ref, missing_snapshot=provenance.snapshot_ref))
            continue
        detail = content_break(provenance, bodies)
        if detail:
            breaks.append(
                ProvenanceBreak(value_ref=provenance.value_ref, missing_snapshot=provenance.snapshot_ref, detail=detail)
            )
    return ProvenanceReport(breaks=tuple(breaks))


def unreachable_points(
    required: Iterable[str],
    provenances: Iterable[Provenance],
    snapshots: Mapping[str, Snapshot],
    bodies: Mapping[str, SnapshotBody] | None = None,
) -> tuple[str, ...]:
    """Points that no *reachable* provenance citation covers."""
    bodies = bodies or {}
    proven = {
        provenance.value_ref
        for provenance in provenances
        if _reachable(provenance, snapshots, bodies)
    }
    return tuple(point for point in required if point not in proven)


def coverage(
    required: Iterable[str],
    provenances: Iterable[Provenance],
    snapshots: Mapping[str, Snapshot],
    bodies: Mapping[str, SnapshotBody] | None = None,
) -> float:
    points = tuple(required)
    if not points:
        # No points is not full coverage; it is absence of evidence.
        return 0.0
    missing = unreachable_points(points, provenances, snapshots, bodies)
    return (len(points) - len(missing)) / len(points)


def validate_points(
    required: Iterable[str],
    provenances: Iterable[Provenance],
    snapshots: Mapping[str, Snapshot],
    bodies: Mapping[str, SnapshotBody] | None = None,
) -> ProvenanceReport:
    """Full check: breaks (name and content) plus points that cannot be traced."""
    provenances = tuple(provenances)
    bodies = bodies or {}
    report = validate_chain(provenances, snapshots, bodies)
    points = tuple(required)
    return ProvenanceReport(
        breaks=report.breaks,
        unreachable_points=unreachable_points(points, provenances, snapshots, bodies),
        coverage=coverage(points, provenances, snapshots, bodies),
    )
