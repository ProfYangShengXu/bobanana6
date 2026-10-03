"""`provenance` -- can every point be traced back to a snapshot?

Missing rate above the limit is a failure. A block with no points at all is
also a failure: "nothing to trace" is not the same as "everything traced".

★ A11: when the outside reader has handed over the frozen bodies, a citation is
also checked for *content* -- the cited row must exist and the value must match
it. The name being in the registry proves nothing about whether the reference is
real, and the fabricated answer this gap was found by (30 rows citing a 3-row
snapshot) passes the name check with coverage 1.0.
"""

from __future__ import annotations

from ..model import Artifact, Candidate, Constraint, Ontology, SnapshotBody, is_missing
from ..ontology import base_unit, convert_value, map_field, split_quantity
from ..provenance import row_of, same_value, validate_points
from .base import CheckContext, CheckOutcome, artifact_for, fail, ok

KIND = "provenance"


def required_refs(artifact: Artifact) -> tuple[str, ...]:
    """The set of traceable points a deliverable is expected to expose."""
    refs = [f"{artifact.name}:row={index}" for index in range(artifact.row_count)]
    if artifact.text is not None:
        refs.extend(f"{artifact.name}:line={index}" for index, line in enumerate(artifact.text.splitlines()) if line)
    return tuple(refs)


def same_quantity(left: object, right: object, concept: str, ontology: Ontology | None) -> bool:
    """Whether two values are the same *quantity*, allowing a declared conversion.

    ★ ①: a worker that understood its source may hand back the same quantity in
    the field's own unit (``"250 千元"`` read, ``250000.0`` 元 answered). The
    conversion is not a guess -- the factor and the base come from the ontology's
    declared ``Unit`` table (``实现规格 §5.0.1`` 判据②③), and a value that is not
    the same quantity is still a mismatch: dropping the unit without converting
    (250 instead of 250000) stays caught.
    """
    if same_value(left, right):
        return True
    if ontology is None:
        return False
    target = base_unit(concept, ontology)
    if target is None:
        return False
    left_quantity = split_quantity(left) if isinstance(left, str) else None
    right_quantity = split_quantity(right) if isinstance(right, str) else None

    def in_base(value: object, quantity: tuple[float, str] | None) -> float | None:
        if quantity is not None:
            number, symbol = quantity
        else:
            try:
                number, symbol = float(str(value)), target.symbol
            except (TypeError, ValueError):
                return None
        try:
            return convert_value(number, symbol, target.symbol, ontology)
        except (KeyError, ValueError):
            return None

    left_base = in_base(left, left_quantity)
    right_base = in_base(right, right_quantity)
    return left_base is not None and left_base == right_base


def value_mismatches(
    artifact: Artifact,
    candidate: Candidate,
    bodies: dict[str, SnapshotBody] | None = None,
    ontology: Ontology | None = None,
) -> tuple[str, ...]:
    """Values that do not match the row they claim to come from."""
    facts: list[str] = []
    for provenance in sorted(candidate.provenance, key=lambda p: (p.value_ref, p.snapshot_ref)):
        body = (bodies or {}).get(provenance.snapshot_ref)
        index = row_of(provenance.value_ref)
        if body is None or index is None or index >= artifact.row_count:
            continue
        source_row = provenance.source_row if provenance.source_row is not None else index
        snapshot_row = body.row(source_row)
        if snapshot_row is None:
            continue  # a missing row is the chain's business, not a value mismatch
        row = artifact.rows[index]
        for column in sorted(set(row) & set(snapshot_row)):
            value = row[column]
            if is_missing(value):
                continue
            if not same_quantity(value, snapshot_row[column], map_field(column, ontology) if ontology else column, ontology):
                facts.append(
                    f"row={index} col={column} value {value!r} does not match snapshot "
                    f"{provenance.snapshot_ref!r} row {source_row}: {snapshot_row[column]!r}"
                )
    return tuple(facts)


def check(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
    artifact = artifact_for(constraint, candidate)
    if artifact is None:
        return fail(constraint, f"artifact for target {constraint.target!r} not found")
    minimum = constraint.spec.get("min_coverage", context.limits.provenance_min_coverage)
    if not isinstance(minimum, (int, float)) or not 0.0 <= minimum <= 1.0:
        return fail(constraint, f"min_coverage must be in [0, 1], got {minimum!r}")

    points = required_refs(artifact)
    if not points:
        return fail(constraint, f"{artifact.name!r} exposes no traceable points")

    bodies = dict(context.snapshot_bodies)
    report = validate_points(points, candidate.provenance, context.snapshots, bodies)
    coverage = report.coverage

    content = tuple(break_.detail for break_ in report.breaks if break_.detail)
    mismatches = value_mismatches(artifact, candidate, bodies, context.ontology)

    if coverage < minimum or content or mismatches:
        detail = f"coverage {coverage:.4f} < required {minimum}"
        if coverage >= minimum:
            detail = f"coverage {coverage:.4f} reaches {minimum}, but the citations do not replay"
        unknown = tuple(
            sorted(
                {
                    provenance.snapshot_ref
                    for provenance in candidate.provenance
                    if provenance.snapshot_ref not in context.snapshots
                }
            )
        )
        if unknown:
            detail += f"; unknown snapshots {unknown!r}"
        if content:
            detail += f"; {content[0]}"
        if mismatches:
            detail += f"; {mismatches[0]}"
        return fail(constraint, detail)
    return ok(constraint, f"coverage {coverage:.4f} >= required {minimum}")
