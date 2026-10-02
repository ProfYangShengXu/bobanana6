"""Fakes for the kernel unit tests.

Everything here is in-memory: no model call, no network, no ontology file. The
mini ontology and the tiny artifacts are enough to exercise every branch.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bobanana6.core.capabilities import (
    BlockSpec,
    Capability,
    WorkerCapabilities,
    WorkerLimits,
)
from bobanana6.core.model import (
    DEFAULT_LIMITS,
    Artifact,
    Budget,
    Candidate,
    Claim,
    Column,
    Concept,
    Constraint,
    ConstraintBundle,
    Definition,
    DeliverableSpec,
    Limits,
    Ontology,
    PlanCosts,
    Provenance,
    Relation,
    Snapshot,
    SplitPlan,
    Subtask,
    TaskContract,
    Unit,
)
from bobanana6.core.ports import ToolSet

# --------------------------------------------------------------------------- #
# Mini ontology -- a stand-in for whatever domain the task uses
# --------------------------------------------------------------------------- #


def mini_ontology(*, frozen: bool = True) -> Ontology:
    ontology = Ontology(
        domain="credit",
        version="2026.1",
        concepts={
            "credit.pd": Concept(
                id="credit.pd",
                label="PD",
                kind="measure",
                domain="credit",
                parent="credit.rating",
                aliases=("pd_3y", "probability_of_default"),
            ),
            "credit.rating": Concept(id="credit.rating", label="Rating", kind="attribute", domain="credit"),
            "credit.amount": Concept(id="credit.amount", label="Amount", kind="measure", domain="credit"),
        },
        definitions={
            "credit.pd:v1": Definition(concept_id="credit.pd", version="v1", text="point-in-time PD"),
            "credit.rating:v1": Definition(concept_id="credit.rating", version="v1", text="issuer rating"),
        },
        units={
            "credit.amount:CNY": Unit(concept_id="credit.amount", symbol="CNY", scale=1.0, base="CNY"),
            "credit.amount:USD": Unit(concept_id="credit.amount", symbol="USD", scale=7.0, base="CNY"),
        },
        relations=(Relation(src="credit.pd", dst="credit.rating", kind="belongs_to", cardinality="1:1"),),
        code_systems={"rating_scale": ("AAA", "AA", "A", "BBB")},
    )
    return ontology.freeze() if frozen else ontology


def unfrozen_ontology() -> Ontology:
    return mini_ontology(frozen=False)


# --------------------------------------------------------------------------- #
# Artifacts / candidates
# --------------------------------------------------------------------------- #


def artifact(name: str = "out.csv", rows: tuple[Mapping[str, Any], ...] = (), *, columns: tuple[Column, ...] | None = None) -> Artifact:
    if columns is None:
        names = sorted({key for row in rows for key in row})
        columns = tuple(Column(name=n, dtype="float" if all(isinstance(r.get(n), (int, float)) for r in rows) else "str") for n in names)
    return Artifact(name=name, columns=columns, rows=rows)


def candidate(
    subtask_id: str = "T-01",
    source_id: str = "src-1",
    *,
    artifacts: tuple[Artifact, ...] = (),
    provenance: tuple[Provenance, ...] = (),
    claims: tuple[Claim, ...] = (),
    definition_versions: Mapping[str, str] | None = None,
    hint: str = "",
) -> Candidate:
    return Candidate(
        subtask_id=subtask_id,
        source_id=source_id,
        hint=hint,
        files={a.name: Path("fake") / a.name for a in artifacts},
        provenance=provenance,
        artifacts=artifacts,
        claims=claims,
        definition_versions=definition_versions or {},
    )


def provenance(value_ref: str, snapshot_ref: str = "snap-1") -> Provenance:
    return Provenance(value_ref=value_ref, snapshot_ref=snapshot_ref, locator="L1")


def snapshot(ref: str = "snap-1", captured_at: str = "2026-01-01T00:00:00", source_kind: str = "src-1") -> Snapshot:
    return Snapshot(ref=ref, body_path=Path("fake") / f"{ref}.bin", captured_at=captured_at, source_kind=source_kind)


# --------------------------------------------------------------------------- #
# Constraints / bundles
# --------------------------------------------------------------------------- #


def constraint(
    cid: str = "C-001",
    kind: str = "presence",
    target: str = "*",
    spec: Mapping[str, Any] | None = None,
    rationale: str = "because the contract says so",
) -> Constraint:
    return Constraint(id=cid, kind=kind, target=target, spec=dict(spec or {}), rationale=rationale)


def bundle(
    hard: tuple[Constraint, ...] = (),
    *,
    soft: tuple[Constraint, ...] = (),
    blind: tuple[str, ...] = (),
    unmapped: tuple[tuple[str, str], ...] = (),
    limits: Limits = DEFAULT_LIMITS,
    task_id: str = "task-1",
    version: int = 1,
) -> ConstraintBundle:
    return ConstraintBundle(
        task_id=task_id,
        version=version,
        hard=hard,
        soft=soft,
        blind=blind,
        unmapped=unmapped,
        limits=limits,
    )


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


def subtask(
    sid: str = "T-01",
    *,
    output: str = "out.csv",
    inputs: tuple[str, ...] = (),
    axis: str = "source",
    mode: str = "read",
    partition: str | None = None,
    expects: tuple[tuple[str, str], ...] = (),
    output_fields: tuple[str, ...] = (),
) -> Subtask:
    return Subtask(
        id=sid,
        goal=f"produce {output}",
        inputs=inputs,
        output=output,
        contract_ref="contract-1",
        axis=axis,
        mode=mode,
        partition=partition,
        expects=expects,
        output_fields=output_fields,
    )


def plan(
    subtasks: tuple[Subtask, ...] = (),
    *,
    shape: str = "star",
    interfaces: Mapping[str, str] | None = None,
    round: int = 1,
    version: int = 1,
    recon_evidence: str = "surveyed",
    costs: PlanCosts | None = None,
) -> SplitPlan:
    return SplitPlan(
        task_id="task-1",
        version=version,
        round=round,
        subtasks=subtasks,
        shape=shape,
        interfaces=dict(interfaces or {}),
        recon_evidence=recon_evidence,
        costs=costs,
    )


def contract(deliverables: tuple[str, ...] = ("out.csv",), optional: tuple[str, ...] = ()) -> TaskContract:
    return TaskContract(
        task_id="task-1",
        goal="assemble the panel",
        deliverables=tuple(DeliverableSpec(name=name, required=name not in optional) for name in deliverables),
        sources=(),
    )


# --------------------------------------------------------------------------- #
# Capability fakes
# --------------------------------------------------------------------------- #


def block(block_id: str = "B-1", needs: frozenset[Capability] | None = None, partition: str | None = None) -> BlockSpec:
    return BlockSpec(
        block_id=block_id,
        dimension="source",
        needs=needs if needs is not None else frozenset({Capability.READ}),
        produces=DeliverableSpec(name=f"{block_id}.csv"),
        partition=partition,
    )


def worker(worker_id: str = "W-1", provides: frozenset[Capability] | None = None, concurrency: int = 1) -> WorkerCapabilities:
    return WorkerCapabilities(
        worker_id=worker_id,
        provides=provides if provides is not None else frozenset({Capability.READ}),
        limits=WorkerLimits(concurrency=concurrency),
    )


# --------------------------------------------------------------------------- #
# Port fakes
# --------------------------------------------------------------------------- #


class FakeOntologyPort:
    def __init__(self, ontology: Ontology | None = None) -> None:
        self._ontology = ontology or mini_ontology()
        self.calls = 0

    def obtain(self, contract: TaskContract, survey: Any) -> Ontology:
        self.calls += 1
        return self._ontology


class FakeRuntime:
    """A worker that claims success unconditionally.

    The kernel must ignore the claim and judge the artifact itself.
    """

    def __init__(self, candidate: Candidate) -> None:
        self._candidate = candidate
        self.self_report_ok = True

    def run(self, subtask: Subtask, bundle: ConstraintBundle, ontology: Ontology, tools: ToolSet, budget: Budget) -> Candidate:
        return self._candidate


class FakeArbiter:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, bundle: ConstraintBundle, ontology: Ontology, items: tuple[Any, ...]) -> Any:
        self.calls += 1
        from bobanana6.core.model import BundleDelta

        return BundleDelta(note=f"resolved {len(items)} items")


class FakeSourcePort:
    def __init__(self, snapshots: Mapping[str, Snapshot] | None = None) -> None:
        self._snapshots = dict(snapshots or {"snap-1": snapshot()})

    def fetch(self, ref: Any) -> Snapshot:
        return self._snapshots[ref.ref]


class FakeSemantics:
    def __init__(self, ontology: Ontology) -> None:
        self._ontology = ontology

    def lookup(self, term: str) -> Mapping[str, Any]:
        return {"term": term}

    def map(self, field_name: str) -> str:
        from bobanana6.core.ontology import map_field

        return map_field(field_name, self._ontology) or "unmapped"

    def check(self, usage: Any) -> tuple[bool, str]:
        return True, "ok"


class FakeTask:
    def __init__(self, allowed: tuple[str, ...] = ()) -> None:
        self._allowed = allowed

    def mysubtask(self) -> Mapping[str, Any]:
        return {}

    def upstream(self, name: str) -> Mapping[str, Any]:
        if name not in self._allowed:
            raise PermissionError(name)
        return {}

    def contract(self) -> TaskContract:
        return contract()


class FakeCheck:
    def local(self) -> Any:
        return None

    def provenance(self) -> Any:
        return None

    def assemble_dryrun(self) -> Any:
        return None


def toolset_for(subtask_id: str = "T-03", allowed_inputs: tuple[str, ...] = (), sources: bool = False) -> ToolSet:
    ontology = mini_ontology()
    return ToolSet(
        subtask=subtask_id,
        semantics=FakeSemantics(ontology),
        task=FakeTask(allowed_inputs),
        check=FakeCheck(),
        sources=None if not sources else object(),  # type: ignore[arg-type]
        allowed_inputs=allowed_inputs,
    )
