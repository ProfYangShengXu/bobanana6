"""The judgment kernel: admit, classify three ways, converge.

Two properties matter more than anything else here.

*Determinism.* ``admit`` is a pure function of its arguments. Every candidate
list is sorted before use, and no set is iterated without ``sorted``. Nothing
reads the clock, the filesystem or the network.

*Fail loudly, never silently.* All hard constraints are evaluated, not just
until the first failure, so ``Verdict.failed`` is the complete list. An unknown
kind, a malformed spec or a raising checker becomes a failing constraint with a
fact attached; it is never skipped.

Divergence classification follows one rule: **check the definition first, then
the conflict**. A version drift is a defect to normalize, not a signal to
record. The classification is exhaustive over the three cases the spec defines
and the three classes are disjoint types.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .checkers import JUDGMENT_ORDER, check_kind
from .checkers.base import CheckContext, CheckOutcome
from .model import (
    BlindSpot,
    Candidate,
    Classification,
    Conflict,
    Constraint,
    ConstraintBundle,
    ConvergeDecision,
    Convergence,
    DefinitionMismatch,
    Provenance,
    Verdict,
    content_digest,
)
from .ontology import map_field

__all__ = ["admit", "candidate_fingerprint", "classify", "converge", "run_hard"]

_ORDER_INDEX = {kind: index for index, kind in enumerate(JUDGMENT_ORDER)}


def _ordered(constraints: Iterable[Constraint]) -> tuple[Constraint, ...]:
    """Judgment order first (cheap shape errors before value errors), id as the
    stable tie-break. Unknown kinds sort last but are never dropped."""
    return tuple(
        sorted(
            constraints,
            key=lambda c: (_ORDER_INDEX.get(c.kind, len(JUDGMENT_ORDER)), c.id),
        )
    )


def run_hard(
    bundle: ConstraintBundle,
    candidate: Candidate,
    context: CheckContext,
) -> tuple[CheckOutcome, ...]:
    return tuple(check_kind(constraint, candidate, context) for constraint in _ordered(bundle.hard))


def admit(bundle: ConstraintBundle, candidate: Candidate, context: CheckContext) -> Verdict:
    """Independently decide whether one block is admissible.

    A runtime's own self-report never enters this function: only the bundle,
    the candidate and the injected context are read.
    """
    outcomes = run_hard(bundle, candidate, context)
    failed = tuple(outcome.constraint_id for outcome in outcomes if not outcome.ok)
    evidence: dict[str, str] = {}
    for outcome in outcomes:
        evidence[outcome.constraint_id] = "; ".join(outcome.facts)
    soft_scores: dict[str, float] = {}
    for constraint in _ordered(bundle.soft):
        outcome = check_kind(constraint, candidate, context)
        evidence[constraint.id] = "; ".join(outcome.facts)
        if outcome.soft_score is not None:
            soft_scores[constraint.id] = outcome.soft_score
    return Verdict(ok=not failed, failed=failed, evidence=evidence, soft_scores=soft_scores)


def candidate_fingerprint(candidate: Candidate) -> str:
    """Content fingerprint, not an index or an object id.

    Positional identifiers are not trustworthy once collections are rebuilt,
    so convergence compares content.
    """
    payload = {
        "hint": candidate.hint,
        "files": {name: str(path) for name, path in sorted(candidate.files.items())},
        "artifacts": [
            {
                "name": artifact.name,
                "columns": [(c.name, c.dtype, c.nullable) for c in artifact.columns],
                "rows": [{k: artifact.rows[i][k] for k in sorted(artifact.rows[i])} for i in range(len(artifact.rows))],
                "text": artifact.text,
            }
            for artifact in sorted(candidate.artifacts, key=lambda a: a.name)
        ],
    }
    return content_digest(payload)


def _covering(constraints: Iterable[Constraint], artifact_name: str, column: str, subtask_id: str) -> tuple[Constraint, ...]:
    covering = []
    for constraint in constraints:
        target_ok = constraint.target in (artifact_name, subtask_id, "*")
        spec = constraint.spec
        field_ok = spec.get("column") == column or spec.get("field") == column or constraint.kind == "schema"
        if target_ok and field_ok:
            covering.append(constraint)
    return tuple(covering)


def _differences(left: Candidate, right: Candidate) -> tuple[tuple[str, str, int, Any, Any], ...]:
    """(artifact, column, row_index, left_value, right_value), in sorted order."""
    found: list[tuple[str, str, int, Any, Any]] = []
    right_by_name = {artifact.name: artifact for artifact in right.artifacts}
    for left_artifact in sorted(left.artifacts, key=lambda a: a.name):
        right_artifact = right_by_name.get(left_artifact.name)
        if right_artifact is None:
            continue
        right_columns = [column.name for column in right_artifact.columns]
        for column in sorted({c.name for c in left_artifact.columns} & set(right_columns)):
            limit = min(left_artifact.row_count, right_artifact.row_count)
            for index in range(limit):
                left_value = left_artifact.rows[index].get(column)
                right_value = right_artifact.rows[index].get(column)
                if left_value != right_value:
                    found.append((left_artifact.name, column, index, left_value, right_value))
    return tuple(found)


def _claims_for(candidate: Candidate, column: str, value: Any) -> tuple[tuple[str, Any, Provenance], ...]:
    claims = tuple(
        (claim.source_id, claim.value, claim.provenance)
        for claim in sorted(candidate.claims, key=lambda c: (c.source_id, repr(c.value)))
        if claim.field == column
    )
    if claims:
        return claims
    return ((candidate.source_id, value, _fallback_provenance(candidate, column)),)


def _fallback_provenance(candidate: Candidate, column: str) -> Provenance:
    for provenance in candidate.provenance:
        if provenance.value_ref.endswith(column) or column in provenance.value_ref:
            return provenance
    if candidate.provenance:
        return candidate.provenance[0]
    return Provenance(value_ref=f"{candidate.subtask_id}:{column}", snapshot_ref=candidate.source_id)


def classify(
    bundle: ConstraintBundle,
    left: Candidate,
    right: Candidate,
    context: CheckContext,
) -> Classification:
    """Compare two admissible candidates and sort each difference into exactly
    one of the three divergence classes. Definition first, conflict second."""
    blind: list[BlindSpot] = []
    conflicts: list[Conflict] = []
    mismatches: list[DefinitionMismatch] = []

    for artifact_name, column, index, left_value, right_value in _differences(left, right):
        concept = column
        if context.ontology is not None:
            concept = map_field(column, context.ontology) or column
        versions = {
            candidate.definition_versions.get(concept)
            for candidate in (left, right)
            if candidate.definition_versions.get(concept)
        }
        if len(versions) > 1:
            mismatches.append(
                DefinitionMismatch(
                    concept_id=concept,
                    versions=tuple(sorted(str(v) for v in versions)),
                    parties=tuple(sorted({left.source_id, right.source_id})),
                )
            )
            continue
        covering = _covering((*bundle.hard, *bundle.soft), artifact_name, column, left.subtask_id)
        if not covering:
            magnitude = None
            if isinstance(left_value, (int, float)) and isinstance(right_value, (int, float)):
                magnitude = abs(float(left_value) - float(right_value))
            blind.append(
                BlindSpot(
                    path=f"{artifact_name}:row={index}:{column}",
                    left=left_value,
                    right=right_value,
                    magnitude=magnitude,
                )
            )
            continue
        version = next(iter(sorted(v for v in versions))) if versions else ""
        if left.source_id != right.source_id:
            claims = _claims_for(left, column, left_value) + _claims_for(right, column, right_value)
            conflicts.append(Conflict(concept_id=concept, definition_version=version, claims=claims))
        else:
            blind.append(
                BlindSpot(
                    path=f"{artifact_name}:row={index}:{column}",
                    left=left_value,
                    right=right_value,
                    magnitude=None,
                )
            )

    return Classification(
        blind_spots=tuple(_dedupe_blind(blind)),
        conflicts=tuple(_dedupe_conflicts(conflicts)),
        definition_mismatches=tuple(_dedupe_mismatches(mismatches)),
    )


def _dedupe_blind(items: list[BlindSpot]) -> list[BlindSpot]:
    seen: dict[tuple[str, str], BlindSpot] = {}
    for item in sorted(items, key=lambda b: (b.path, repr(b.left), repr(b.right))):
        seen.setdefault(item.path, item)
    return list(seen.values())


def _dedupe_conflicts(items: list[Conflict]) -> list[Conflict]:
    seen: dict[str, Conflict] = {}
    for item in sorted(items, key=lambda c: c.concept_id):
        seen.setdefault(item.concept_id, item)
    return list(seen.values())


def _dedupe_mismatches(items: list[DefinitionMismatch]) -> list[DefinitionMismatch]:
    seen: dict[str, DefinitionMismatch] = {}
    for item in sorted(items, key=lambda m: m.concept_id):
        seen.setdefault(item.concept_id, item)
    return list(seen.values())


def converge(
    bundle: ConstraintBundle,
    candidates: Iterable[Candidate],
    context: CheckContext,
) -> Convergence:
    """The four branches from the spec, plus the three-way classification when
    more than one candidate passes."""
    ordered = tuple(sorted(candidates, key=lambda c: (c.source_id, c.subtask_id, candidate_fingerprint(c))))
    verdicts = tuple(admit(bundle, candidate, context) for candidate in ordered)
    passed = tuple(candidate for candidate, verdict in zip(ordered, verdicts) if verdict.ok)

    if not passed:
        return Convergence(
            decision=ConvergeDecision.ARBITRATE,
            verdicts=verdicts,
            note="no candidate passed; change the constraints, not the artifacts",
        )
    if len(passed) == 1:
        return Convergence(decision=ConvergeDecision.DELIVER, verdicts=verdicts, note="exactly one admissible candidate")
    if len({candidate_fingerprint(candidate) for candidate in passed}) == 1:
        return Convergence(decision=ConvergeDecision.DELIVER, verdicts=verdicts, note="all admissible candidates are identical")

    classifications = [
        classify(bundle, passed[index], passed[index + 1], context) for index in range(len(passed) - 1)
    ]
    merged = Classification(
        blind_spots=tuple(_dedupe_blind([item for c in classifications for item in c.blind_spots])),
        conflicts=tuple(_dedupe_conflicts([item for c in classifications for item in c.conflicts])),
        definition_mismatches=tuple(_dedupe_mismatches([item for c in classifications for item in c.definition_mismatches])),
    )
    if merged.definition_mismatches:
        decision = ConvergeDecision.NORMALIZE_DEFINITION
        note = "definition versions differ; normalize and compare again"
    elif merged.blind_spots:
        decision = ConvergeDecision.REFINE_SPLIT
        note = "difference falls on a field no constraint covers; add a constraint and re-run"
    else:
        decision = ConvergeDecision.RECORD_CONFLICT
        note = "same definition, different sources, different values; record both"
    return Convergence(decision=decision, verdicts=verdicts, classification=merged, note=note)
