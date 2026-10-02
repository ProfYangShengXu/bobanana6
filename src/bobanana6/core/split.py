"""Split legality: is this cut a sound one?

The checks here answer "may this split be shipped", never "how should the task
be cut" -- deciding a strategy is the decomposer's job and lives outside
``core/``. Every violation names the block(s) it is about, so a failed split can
be repaired locally instead of re-cut from scratch.

Structural guards that the spec's check list does not name are still enforced
(empty plan, duplicate ids, duplicate outputs). A shape with zero blocks would
otherwise satisfy every rule vacuously, and a duplicated id would make
"which block failed" unanswerable.
"""

from __future__ import annotations

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from .model import (
    DEFAULT_LIMITS,
    ConstraintBundle,
    Limits,
    Ontology,
    SplitPlan,
    Subtask,
    TaskContract,
)
from .ontology import map_field

__all__ = ["CHECKS", "SplitReport", "SplitViolation", "check_split", "subtask_layers"]

#: The eleven checks the spec enumerates, in reporting order.
CHECKS: tuple[str, ...] = (
    "star",
    "iface_complete",
    "iface_match",
    "verifiable",
    "axis_uniform",
    "no_orphan",
    "write_serial",
    "merge_cheap",
    "concept_mapped",
    "round_budget",
    "semantics_frozen",
)

#: Structural guards outside the spec list, kept separate so the check list
#: stays exactly as specified.
STRUCTURAL_CHECKS: tuple[str, ...] = ("empty_plan", "duplicate_id", "duplicate_output", "cycle")


@dataclass(frozen=True)
class SplitViolation:
    check: str
    detail: str
    subtask_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SplitReport:
    violations: tuple[SplitViolation, ...] = ()
    checks_run: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    def codes(self) -> tuple[str, ...]:
        return tuple(v.check for v in self.violations)


def _by_output(plan: SplitPlan) -> dict[str, Subtask]:
    mapping: dict[str, Subtask] = {}
    for subtask in sorted(plan.subtasks, key=lambda s: s.id):
        mapping.setdefault(subtask.output, subtask)
    return mapping


def subtask_layers(plan: SplitPlan) -> tuple[dict[str, int], tuple[str, ...]]:
    """Dependency depth per subtask, plus a cycle report.

    Nodes and edges are inserted in sorted order: ``TopologicalSorter`` keeps
    insertion order, so unsorted input would make the result depend on dict
    ordering rather than on the plan.
    """
    producer = _by_output(plan)
    nodes = sorted(subtask.id for subtask in plan.subtasks)
    graph: dict[str, list[str]] = {node: [] for node in nodes}
    for subtask in sorted(plan.subtasks, key=lambda s: s.id):
        for name in sorted(set(subtask.inputs)):
            upstream = producer.get(name)
            if upstream is not None and upstream.id != subtask.id and upstream.id in graph:
                graph[subtask.id].append(upstream.id)

    sorter = TopologicalSorter({node: graph[node] for node in nodes})
    try:
        order = list(sorter.static_order())
    except CycleError as exc:
        cycle = tuple(str(part) for part in exc.args[1]) if len(exc.args) > 1 else ()
        return ({node: 0 for node in nodes}, cycle)

    depth: dict[str, int] = {node: 0 for node in nodes}
    for node in order:
        for upstream in graph[node]:
            depth[node] = max(depth[node], depth[upstream] + 1)
    return depth, ()


def check_split(
    plan: SplitPlan,
    contract: TaskContract,
    bundle: ConstraintBundle,
    ontology: Ontology | None,
    *,
    frozen_hash: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> SplitReport:
    violations: list[SplitViolation] = []

    def add(check: str, detail: str, *ids: str) -> None:
        violations.append(SplitViolation(check=check, detail=detail, subtask_ids=tuple(sorted(ids))))

    # -- structural guards -------------------------------------------------
    if not plan.subtasks:
        add("empty_plan", "a plan with no blocks cannot be shipped")
        return SplitReport(violations=tuple(violations), checks_run=(*STRUCTURAL_CHECKS, *CHECKS))

    ids = [subtask.id for subtask in plan.subtasks]
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        add("duplicate_id", f"duplicate subtask ids {duplicates!r}", *duplicates)

    outputs: list[str] = [subtask.output for subtask in plan.subtasks]
    duplicate_outputs = sorted({value for value in outputs if outputs.count(value) > 1})
    if duplicate_outputs:
        add("duplicate_output", f"duplicate outputs {duplicate_outputs!r}")

    depth, cycle = subtask_layers(plan)
    if cycle:
        add("cycle", f"block graph has a cycle: {cycle}", *cycle)

    produced = set(outputs)
    consumed = {name for subtask in plan.subtasks for name in subtask.inputs}

    # -- star: no lateral dependency --------------------------------------
    if plan.shape == "star":
        for subtask in sorted(plan.subtasks, key=lambda s: s.id):
            bad = sorted(name for name in set(subtask.inputs) if name in produced)
            if bad:
                add("star", f"block {subtask.id!r} depends on block outputs {bad!r}", subtask.id)

    # -- iface_complete ----------------------------------------------------
    known_interfaces = set(plan.interfaces)
    contract_deliverables = set(contract.deliverable_names)
    for subtask in sorted(plan.subtasks, key=lambda s: s.id):
        if subtask.output not in known_interfaces:
            add("iface_complete", f"output {subtask.output!r} has no interface declaration", subtask.id)
        for name in sorted(set(subtask.inputs)):
            if name not in known_interfaces and name not in contract_deliverables:
                add("iface_complete", f"input {name!r} resolves to no interface", subtask.id)

    # -- iface_match -------------------------------------------------------
    for subtask in sorted(plan.subtasks, key=lambda s: s.id):
        for name, expected in sorted(subtask.expects):
            actual = plan.interfaces.get(name)
            if actual != expected:
                add("iface_match", f"input {name!r} expects {expected!r} but producer declares {actual!r}", subtask.id)

    # -- verifiable --------------------------------------------------------
    targets_by_id: dict[str, list[str]] = {subtask.id: [] for subtask in plan.subtasks}
    for constraint in bundle.hard:
        for subtask in plan.subtasks:
            if constraint.target in (subtask.id, subtask.output):
                targets_by_id[subtask.id].append(constraint.kind)
    for subtask in sorted(plan.subtasks, key=lambda s: s.id):
        if not targets_by_id[subtask.id]:
            add("verifiable", f"block {subtask.id!r} has no decidable hard constraint", subtask.id)

    # -- axis_uniform ------------------------------------------------------
    axis_by_layer: dict[int, set[str]] = {}
    for subtask in sorted(plan.subtasks, key=lambda s: s.id):
        axis_by_layer.setdefault(depth.get(subtask.id, 0), set()).add(subtask.axis)
    for layer in sorted(axis_by_layer):
        if len(axis_by_layer[layer]) > 1:
            members = sorted(s.id for s in plan.subtasks if depth.get(s.id, 0) == layer)
            add("axis_uniform", f"layer {layer} mixes axes {sorted(axis_by_layer[layer])!r}", *members)

    # -- no_orphan ---------------------------------------------------------
    for subtask in sorted(plan.subtasks, key=lambda s: s.id):
        if subtask.output not in contract_deliverables and subtask.output not in consumed:
            add("no_orphan", f"output {subtask.output!r} is neither delivered nor consumed", subtask.id)

    # -- write_serial ------------------------------------------------------
    writes_by_layer: dict[int, list[Subtask]] = {}
    for subtask in plan.subtasks:
        if subtask.mode == "write":
            writes_by_layer.setdefault(depth.get(subtask.id, 0), []).append(subtask)
    for layer in sorted(writes_by_layer):
        writers = sorted(writes_by_layer[layer], key=lambda s: s.id)
        if len(writers) <= 1:
            continue
        partitions = [subtask.partition for subtask in writers]
        if any(partition is None for partition in partitions):
            add("write_serial", f"layer {layer} has concurrent writes without partitions", *(s.id for s in writers))
        else:
            counts: dict[str, int] = {}
            for partition in partitions:
                counts[str(partition)] = counts.get(str(partition), 0) + 1
            shared = sorted(name for name, count in counts.items() if count > 1)
            if shared:
                add("write_serial", f"layer {layer} writes share partitions {shared!r}", *(s.id for s in writers))

    # -- merge_cheap -------------------------------------------------------
    if plan.costs is None:
        add("merge_cheap", "merge/execute cost estimates are not declared; cheapness cannot be proven")
    else:
        if plan.costs.execute <= 0.0:
            add("merge_cheap", "execution cost estimate must be > 0")
        else:
            ratio = plan.costs.merge / plan.costs.execute
            if ratio >= limits.merge_cost_ratio:
                add("merge_cheap", f"merge/execute ratio {ratio:.4f} >= limit {limits.merge_cost_ratio}")

    # -- concept_mapped ----------------------------------------------------
    if ontology is None:
        add("concept_mapped", "ontology is required to prove output fields map to shared semantics")
    else:
        for subtask in sorted(plan.subtasks, key=lambda s: s.id):
            unmapped = sorted(
                field_name
                for field_name in set(subtask.output_fields)
                if map_field(field_name, ontology) is None and field_name not in bundle.unmapped_fields
            )
            if unmapped:
                add("concept_mapped", f"output fields {unmapped!r} map to no concept and are not declared unmapped", subtask.id)

    # -- round_budget ------------------------------------------------------
    if plan.round > limits.max_rounds:
        add("round_budget", f"plan.round {plan.round} exceeds max {limits.max_rounds}")

    # -- semantics_frozen --------------------------------------------------
    if ontology is None:
        add("semantics_frozen", "ontology is required to prove shared semantics are frozen")
    elif not ontology.is_frozen:
        add("semantics_frozen", "ontology was not frozen before the first worker starts")
    elif frozen_hash is not None and frozen_hash != ontology.content_hash():
        add("semantics_frozen", f"frozen hash {frozen_hash!r} != ontology content hash {ontology.content_hash()!r}")

    ordered = sorted(violations, key=lambda v: (v.check, v.subtask_ids, v.detail))
    return SplitReport(violations=tuple(ordered), checks_run=(*STRUCTURAL_CHECKS, *CHECKS))
