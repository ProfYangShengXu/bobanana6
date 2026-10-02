"""Data structures for the judgment kernel.

Everything here is a frozen value object: no logic beyond construction-time
validation, no I/O, no environment access. All ordering that can leak into a
decision is explicit (tuples, or sorted iteration over mappings) so that the
same input always yields the same output regardless of process hash seed.

Inference notes (the spec names these concepts but does not pin every field;
they are marked so a later doc sync can confirm them):
  * ``Limits`` / ``WorkerLimits`` / ``ResourceUse`` / ``Budget`` carry the
    capacity ceilings that the spec references but never defines.
  * ``DeliverableSpec`` / ``PlanCosts`` / ``Subtask.expects`` /
    ``Subtask.output_fields`` supply the data the split and assemble checks
    need but the spec's field lists omit.
  * ``Artifact`` is the in-memory view of a produced file. The kernel never
    reads paths: a loader outside ``core/`` fills this in, which is what keeps
    the kernel deterministic and environment free.
  * ``Claim`` is the (source, value, provenance) carrier that ``Conflict``
    refers to but never declares a producer for.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from json import dumps
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any

__all__ = [
    "KNOWN_KINDS",
    "MAX_ROUNDS",
    "Artifact",
    "BlindSpot",
    "Budget",
    "BundleDelta",
    "Candidate",
    "Claim",
    "Classification",
    "Column",
    "Concept",
    "Conflict",
    "Constraint",
    "ConstraintBundle",
    "ConvergeDecision",
    "Convergence",
    "Definition",
    "DefinitionMismatch",
    "DeliverableSpec",
    "DisagreementTypes",
    "InputSurvey",
    "Limits",
    "ModelResponse",
    "Ontology",
    "PlanCosts",
    "Provenance",
    "Recon",
    "Relation",
    "ResourceUse",
    "Snapshot",
    "SourceRef",
    "SplitPlan",
    "Subtask",
    "TaskContract",
    "Unit",
    "Verdict",
]

#: The fourteen constraint kinds from the spec.
KNOWN_KINDS: frozenset[str] = frozenset(
    {
        "schema",
        "presence",
        "range",
        "invariant",
        "relation",
        "provenance",
        "corroboration",
        "freshness",
        "coverage",
        "concept_ref",
        "unit_consistency",
        "definition_pin",
        "code_valid",
        "custom",
    }
)

#: Iterative splitting is bounded; the default is a hard limit, not a hint.
MAX_ROUNDS = 3


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_non_empty(value: str, message: str) -> None:
    _require(isinstance(value, str) and value != "", message)


def canonical_json(value: Any) -> str:
    """Render a value as order-stable JSON.

    Only used for content hashing and evidence strings. ``sort_keys=True`` plus
    the absence of float formatting surprises keeps the digest identical across
    runs and platforms.
    """
    return dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def content_digest(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Ontology
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Concept:
    id: str
    label: str
    kind: str
    domain: str
    parent: str | None = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "Concept.id must not be empty")
        _require_non_empty(self.label, "Concept.label must not be empty")
        _require(self.kind in {"entity", "attribute", "event", "measure"}, f"unknown concept kind: {self.kind}")


@dataclass(frozen=True)
class Definition:
    concept_id: str
    version: str
    text: str
    formula: str | None = None
    edge_cases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.concept_id, "Definition.concept_id must not be empty")
        _require_non_empty(self.version, "Definition.version must not be empty")


@dataclass(frozen=True)
class Unit:
    concept_id: str
    symbol: str
    scale: float
    base: str

    def __post_init__(self) -> None:
        _require_non_empty(self.concept_id, "Unit.concept_id must not be empty")
        _require_non_empty(self.symbol, "Unit.symbol must not be empty")
        _require(
            isinstance(self.scale, (int, float)) and isfinite(float(self.scale)) and self.scale != 0.0,
            "Unit.scale must be a finite non-zero number",
        )


@dataclass(frozen=True)
class Relation:
    src: str
    dst: str
    kind: str
    cardinality: str

    def __post_init__(self) -> None:
        _require_non_empty(self.src, "Relation.src must not be empty")
        _require_non_empty(self.dst, "Relation.dst must not be empty")
        _require(
            self.kind in {"belongs_to", "subtype_of", "measured_in", "derived_from"},
            f"unknown relation kind: {self.kind}",
        )


@dataclass(frozen=True)
class Ontology:
    domain: str
    version: str
    concepts: Mapping[str, Concept]
    definitions: Mapping[str, Definition]
    units: Mapping[str, Unit]
    relations: tuple[Relation, ...] = ()
    code_systems: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    calendar: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.domain, "Ontology.domain must not be empty")
        _require_non_empty(self.version, "Ontology.version must not be empty")
        _require(self.version != "*", "Ontology.version must be a concrete version")
        for key, concept in self.concepts.items():
            _require(key == concept.id, f"concepts key {key!r} != Concept.id {concept.id!r}")

    # -- immutability ------------------------------------------------------ #

    @property
    def is_frozen(self) -> bool:
        """True once the container mappings can no longer be mutated."""
        return isinstance(self.concepts, MappingProxyType)

    def freeze(self) -> Ontology:
        """Return an immutable copy.

        ``frozen=True`` stops attribute rebinding but not mutation of the nested
        dicts, so the "shared semantics frozen before the first worker starts"
        requirement needs an explicit deep freeze.
        """
        return Ontology(
            domain=self.domain,
            version=self.version,
            concepts=_deep_freeze_mapping(self.concepts),
            definitions=_deep_freeze_mapping(self.definitions),
            units=_deep_freeze_mapping(self.units),
            relations=self.relations,
            code_systems=_deep_freeze_mapping(self.code_systems),
            calendar=_deep_freeze_mapping(self.calendar),
        )

    def content_hash(self) -> str:
        """Deterministic digest of the whole ontology -- the freeze witness."""
        return content_digest(
            {
                "domain": self.domain,
                "version": self.version,
                "concepts": {
                    key: {
                        "id": concept.id,
                        "label": concept.label,
                        "kind": concept.kind,
                        "domain": concept.domain,
                        "parent": concept.parent,
                        "aliases": list(concept.aliases),
                    }
                    for key, concept in self.concepts.items()
                },
                "definitions": {
                    key: {
                        "concept_id": definition.concept_id,
                        "version": definition.version,
                        "text": definition.text,
                        "formula": definition.formula,
                        "edge_cases": list(definition.edge_cases),
                    }
                    for key, definition in self.definitions.items()
                },
                "units": {
                    key: {
                        "concept_id": unit.concept_id,
                        "symbol": unit.symbol,
                        "scale": unit.scale,
                        "base": unit.base,
                    }
                    for key, unit in self.units.items()
                },
                "relations": [
                    {"src": r.src, "dst": r.dst, "kind": r.kind, "cardinality": r.cardinality}
                    for r in self.relations
                ],
                "code_systems": {k: list(v) for k, v in self.code_systems.items()},
                "calendar": dict(self.calendar),
            }
        )


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _deep_freeze_mapping(value: Mapping[Any, Any]) -> Mapping[Any, Any]:
    frozen = _deep_freeze(value)
    assert isinstance(frozen, MappingProxyType)
    return frozen


# --------------------------------------------------------------------------- #
# Contract / split
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeliverableSpec:
    name: str
    schema: Mapping[str, str] = field(default_factory=dict)
    required: bool = True

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "DeliverableSpec.name must not be empty")


@dataclass(frozen=True)
class SourceRef:
    ref: str
    locator: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.ref, "SourceRef.ref must not be empty")


@dataclass(frozen=True)
class TaskContract:
    """The task as data. Parsing a task file is a later unit's job."""

    task_id: str
    goal: str
    deliverables: tuple[DeliverableSpec, ...] = ()
    sources: tuple[SourceRef, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "TaskContract.task_id must not be empty")

    @property
    def deliverable_names(self) -> frozenset[str]:
        return frozenset(d.name for d in self.deliverables)


@dataclass(frozen=True)
class Subtask:
    id: str
    goal: str
    inputs: tuple[str, ...]
    output: str
    contract_ref: str
    axis: str
    mode: str
    partition: str | None = None
    # Inference: the schema the consumer expects for each named input, so
    # `iface_match` has something to compare against the producer declaration.
    expects: tuple[tuple[str, str], ...] = ()
    # Inference: which ontology-mapped fields this block produces, so
    # `concept_mapped` is checkable.
    output_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "Subtask.id must not be empty")
        _require_non_empty(self.output, "Subtask.output must not be empty")
        _require(self.mode in {"read", "write"}, f"unknown subtask mode: {self.mode}")
        _require(
            self.axis in {"source", "entity", "window", "field", "module"},
            f"unknown subtask axis: {self.axis}",
        )


@dataclass(frozen=True)
class PlanCosts:
    """Estimated merge cost and execution cost, in the same abstract unit."""

    merge: float
    execute: float

    def __post_init__(self) -> None:
        _require(self.merge >= 0.0, "PlanCosts.merge must be >= 0")
        _require(self.execute >= 0.0, "PlanCosts.execute must be >= 0")


@dataclass(frozen=True)
class SplitPlan:
    task_id: str
    version: int
    round: int
    subtasks: tuple[Subtask, ...]
    shape: str
    interfaces: Mapping[str, str] = field(default_factory=dict)
    recon_evidence: str = ""
    costs: PlanCosts | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "SplitPlan.task_id must not be empty")
        _require(self.version >= 1, "SplitPlan.version must be >= 1")
        _require(self.round >= 0, "SplitPlan.round must be >= 0")
        _require(self.shape in {"star", "pipeline", "grid"}, f"unknown plan shape: {self.shape}")

    @property
    def subtask_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.subtasks)


@dataclass(frozen=True)
class InputSurvey:
    source_refs: tuple[str, ...] = ()
    byte_size: int = 0
    shape: str = ""
    notes: str = ""


@dataclass(frozen=True)
class Recon:
    evidence: str = ""
    source_ids: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Constraints
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Constraint:
    id: str
    kind: str
    target: str
    spec: Mapping[str, Any]
    rationale: str

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "Constraint.id must not be empty")
        _require_non_empty(self.kind, "Constraint.kind must not be empty")
        _require_non_empty(self.rationale, "Constraint.rationale must not be empty")
        _require(isinstance(self.spec, Mapping), "Constraint.spec must be a mapping")


@dataclass(frozen=True)
class Limits:
    """Capacity ceilings and thresholds. No unit is interpreted by ``core``."""

    max_rounds: int = MAX_ROUNDS
    merge_cost_ratio: float = 0.5
    tol: float = 1e-9
    provenance_min_coverage: float = 0.95
    corroboration_min_sources: int = 2
    cost_unit: str = "declared"
    output_unit: str = "declared"

    def __post_init__(self) -> None:
        _require(self.max_rounds >= 0, "Limits.max_rounds must be >= 0")
        _require(self.merge_cost_ratio > 0.0, "Limits.merge_cost_ratio must be > 0")
        _require(self.tol >= 0.0, "Limits.tol must be >= 0")
        _require(0.0 <= self.provenance_min_coverage <= 1.0, "provenance_min_coverage must be in [0, 1]")
        _require(self.corroboration_min_sources >= 1, "corroboration_min_sources must be >= 1")


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class ConstraintBundle:
    task_id: str
    version: int
    hard: tuple[Constraint, ...] = ()
    soft: tuple[Constraint, ...] = ()
    blind: tuple[str, ...] = ()
    unmapped: tuple[tuple[str, str], ...] = ()
    limits: Limits = DEFAULT_LIMITS
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "ConstraintBundle.task_id must not be empty")
        _require(self.version >= 1, "ConstraintBundle.version must be >= 1")
        ids = [c.id for c in self.hard] + [c.id for c in self.soft]
        _require(len(ids) == len(set(ids)), "constraint ids must be unique within a bundle")

    @property
    def unmapped_fields(self) -> frozenset[str]:
        return frozenset(name for name, _reason in self.unmapped)


# --------------------------------------------------------------------------- #
# Divergence -- three distinct types, deliberately not merged
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlindSpot:
    """Divergence caused by constraint silence -> add a constraint."""

    path: str
    left: Any
    right: Any
    magnitude: float | None = None


@dataclass(frozen=True)
class Conflict:
    """Same definition, different sources, different values -> record both."""

    concept_id: str
    definition_version: str
    claims: tuple[tuple[str, Any, Provenance], ...]


@dataclass(frozen=True)
class DefinitionMismatch:
    """Different definition versions -> a defect, normalize before comparing."""

    concept_id: str
    versions: tuple[str, ...]
    parties: tuple[str, ...]


Disagreement = BlindSpot | Conflict | DefinitionMismatch

#: The three divergence classes, kept as a tuple so a guard can assert that
#: they remain three distinct, non-overlapping types.
DisagreementTypes: tuple[type, ...] = (BlindSpot, Conflict, DefinitionMismatch)


# --------------------------------------------------------------------------- #
# Provenance / artifacts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Snapshot:
    ref: str
    body_path: Path
    captured_at: str
    source_kind: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.ref, "Snapshot.ref must not be empty")


@dataclass(frozen=True)
class Provenance:
    value_ref: str
    snapshot_ref: str
    locator: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.value_ref, "Provenance.value_ref must not be empty")
        _require_non_empty(self.snapshot_ref, "Provenance.snapshot_ref must not be empty")


@dataclass(frozen=True)
class Column:
    name: str
    dtype: str
    nullable: bool = True

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "Column.name must not be empty")
        _require_non_empty(self.dtype, "Column.dtype must not be empty")


@dataclass(frozen=True)
class Artifact:
    """An in-memory view of one produced deliverable.

    Loaders live outside ``core/``; the kernel only reasons over this value, so
    no path is ever opened here.
    """

    name: str
    columns: tuple[Column, ...] = ()
    rows: tuple[Mapping[str, Any], ...] = ()
    text: str | None = None
    path: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "Artifact.name must not be empty")

    def column(self, name: str) -> Column | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class Claim:
    """One source's asserted value for a field, with its provenance."""

    field: str
    value: Any
    source_id: str
    provenance: Provenance

    def __post_init__(self) -> None:
        _require_non_empty(self.field, "Claim.field must not be empty")
        _require_non_empty(self.source_id, "Claim.source_id must not be empty")


# --------------------------------------------------------------------------- #
# Runtime-facing value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResourceUse:
    calls: int = 0
    wall_s: float = 0.0
    output: int = 0


@dataclass(frozen=True)
class ModelResponse:
    """One model call's result, as data. Returned by the ``LLMClient`` port."""

    text: str = ""
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Budget:
    max_calls: int
    max_wall_s: int
    max_output: int

    def __post_init__(self) -> None:
        _require(self.max_calls >= 0, "Budget.max_calls must be >= 0")
        _require(self.max_wall_s >= 0, "Budget.max_wall_s must be >= 0")
        _require(self.max_output >= 0, "Budget.max_output must be >= 0")


@dataclass(frozen=True)
class Candidate:
    subtask_id: str
    source_id: str
    hint: str = ""
    files: Mapping[str, Path] = field(default_factory=dict)
    provenance: tuple[Provenance, ...] = ()
    cost: ResourceUse = ResourceUse()
    # In-memory content of `files`, injected by the loader outside core/.
    artifacts: tuple[Artifact, ...] = ()
    claims: tuple[Claim, ...] = ()
    # Inference: which definition version this candidate used per concept, so
    # `classify` can tell a definition mismatch from a real conflict.
    definition_versions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.subtask_id, "Candidate.subtask_id must not be empty")
        _require_non_empty(self.source_id, "Candidate.source_id must not be empty")

    def artifact(self, name: str) -> Artifact | None:
        for artifact in self.artifacts:
            if artifact.name == name:
                return artifact
        return None

    @property
    def artifact_names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.artifacts)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    failed: tuple[str, ...] = ()
    evidence: Mapping[str, str] = field(default_factory=dict)
    soft_scores: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Classification:
    blind_spots: tuple[BlindSpot, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    definition_mismatches: tuple[DefinitionMismatch, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.blind_spots or self.conflicts or self.definition_mismatches)


class ConvergeDecision(StrEnum):
    DELIVER = "deliver"
    ARBITRATE = "arbitrate"
    NORMALIZE_DEFINITION = "normalize_definition"
    RECORD_CONFLICT = "record_conflict"
    REFINE_SPLIT = "refine_split"


@dataclass(frozen=True)
class Convergence:
    decision: ConvergeDecision
    verdicts: tuple[Verdict, ...] = ()
    classification: Classification = Classification()
    note: str = ""


@dataclass(frozen=True)
class BundleDelta:
    added_hard: tuple[Constraint, ...] = ()
    added_blind: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class AssemblyOutcome:
    passed: bool
    failed_blocks: tuple[str, ...] = ()
    coverage: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""
