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

__all__ = ["CHECKS", "SplitReport", "SplitViolation", "check_split", "declared_layers", "subtask_layers"]

#: The checks the spec enumerates, in reporting order.
CHECKS: tuple[str, ...] = (
    "star",
    "layer_lateral",
    "layer_wiring",
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


def _depth(
    nodes: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> tuple[dict[str, int], tuple[str, ...]]:
    """Longest-path depth over `(id, output, inputs)` nodes, plus a cycle report.

    One implementation for two callers: the plan's own blocks (``subtask_layers``)
    and the **contract's declared deliverable graph** (used by the layered check to
    have a layering that does not come from the plan itself). Nodes and edges are
    inserted in sorted order: ``TopologicalSorter`` keeps insertion order, so
    unsorted input would make the result depend on dict ordering.
    """
    order_of = sorted(nodes, key=lambda item: item[0])
    producer: dict[str, str] = {}
    for node_id, output, _inputs in order_of:
        producer.setdefault(output, node_id)
    ids = [node_id for node_id, _output, _inputs in order_of]
    graph: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for node_id, _output, inputs in order_of:
        for name in sorted(set(inputs)):
            upstream = producer.get(name)
            if upstream is not None and upstream != node_id and upstream in graph:
                graph[node_id].append(upstream)

    sorter = TopologicalSorter({node_id: graph[node_id] for node_id in ids})
    try:
        order = list(sorter.static_order())
    except CycleError as exc:
        cycle = tuple(str(part) for part in exc.args[1]) if len(exc.args) > 1 else ()
        return ({node_id: 0 for node_id in ids}, cycle)

    depth: dict[str, int] = {node_id: 0 for node_id in ids}
    for node_id in order:
        for upstream in graph[node_id]:
            depth[node_id] = max(depth[node_id], depth[upstream] + 1)
    return depth, ()


def subtask_layers(plan: SplitPlan) -> tuple[dict[str, int], tuple[str, ...]]:
    """Dependency depth per subtask, plus a cycle report."""
    nodes = tuple((subtask.id, subtask.output, tuple(subtask.inputs)) for subtask in plan.subtasks)
    return _depth(nodes)


def declared_layers(contract: TaskContract) -> dict[str, int]:
    """Layer per **declared** deliverable, from the contract's `depends_on` graph.

    This is the independent side of the layered check: the plan is one statement of
    the graph, the contract is another, and `layer_lateral` compares them. An
    undeclared deliverable has no edge, so it sits at layer 0.
    """
    nodes = tuple(
        (deliverable.name, deliverable.name, tuple(deliverable.depends_on))
        for deliverable in contract.deliverables
    )
    depth, _cycle = _depth(nodes)
    return depth


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

    # -- layered: same-layer independence + wiring exactly as declared ------
    # ★ 层次切分新增的两条约束：
    #   ① `layer_lateral` —— 同层内不许有横向依赖。层取**契约声明的图**
    #      （`declared_layers`），不是计划自己的分层 —— 两边是两份独立声明，比较才有意义：
    #      一个块把"契约里与它同层的产物"当输入，就是横向依赖。
    #   ③ `layer_wiring`  —— 接口数纪律：一个块**只许**声明它真正需要的上游，
    #      即契约为它声明的那几条边。禁止"把上层的全部产物都当输入"（§4.3 的
    #      N(N-1)/2 灾难）；自依赖也在这里拦（自己吃自己的产物不是接线，是环）。
    #   ② 跨层无环由既有的 `cycle` 覆盖（它在分层之前就算，对每种形状都跑）。
    if plan.shape == "layered":
        producer_by_output = _by_output(plan)
        declared_deps = {deliverable.name: tuple(deliverable.depends_on) for deliverable in contract.deliverables}
        layer_of = declared_layers(contract)
        position = {block.id: index for index, block in enumerate(plan.subtasks)}
        for subtask in sorted(plan.subtasks, key=lambda s: s.id):
            consumer_layer = layer_of.get(subtask.output)
            for name in sorted(set(subtask.inputs)):
                upstream = producer_by_output.get(name)
                if upstream is None:
                    continue  # iface_complete 已经就"指向不存在的接口"报过
                if upstream.id == subtask.id:
                    add("layer_wiring", f"block {subtask.id!r} declares its own output {name!r} as an input", subtask.id)
                    continue
                # ★ 计划【写下来的顺序】必须能被执行：块按 subtasks 顺序串行跑，上游排在
                #   下游后面 ⇒ 下游开工时输入还没有（前置门 `inputs_unavailable`，0 次调用）。
                #   这不是"运行时运气"，是计划本身的合法性。
                if position.get(upstream.id, 0) > position.get(subtask.id, 0):
                    add(
                        "layer_wiring",
                        f"block {subtask.id!r} is listed before its upstream {upstream.id!r} ({name!r})",
                        subtask.id,
                        upstream.id,
                    )
                producer_layer = layer_of.get(name)
                if consumer_layer is not None and producer_layer is not None and producer_layer == consumer_layer:
                    add(
                        "layer_lateral",
                        f"block {subtask.id!r} depends on same-layer block {upstream.id!r} ({name!r})",
                        subtask.id,
                        upstream.id,
                    )
            if subtask.output in declared_deps:
                # 只比【交付物之间】的边：读块产出的部件不在契约的 depends_on 里
                # （契约声明的是产物图，不是"读哪个源"），它由 ① 的同层约束管。
                deliverable_inputs = {name for name in subtask.inputs if name in contract.deliverable_names}
                want = set(declared_deps[subtask.output])
                if deliverable_inputs != want:
                    add(
                        "layer_wiring",
                        f"block {subtask.id!r} declares inputs {sorted(deliverable_inputs)!r} "
                        f"but the contract declares {sorted(want)!r} for {subtask.output!r}",
                        subtask.id,
                    )

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
    # `contract_deliverables` deliberately includes optional deliverables: the
    # check asks "is this output wanted by anyone", and a declared optional
    # deliverable is wanted by definition. Being unconsumed by another block is
    # fine for a deliverable -- it is the terminal artifact of the task, not an
    # orphan. (Contrast assemble.cut_complete, which only *requires* the
    # `required=True` ones to actually exist.)
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

    # -- shared_state ------------------------------------------------------
    # ★ ③: 契约说"这些来源共享同一份状态"，而计划仍然把它们切成多块 ——
    #   这就是 fake parallelism 的形状（每块单独看都合格，整体拼不上）。
    #   判据是【声明】，不是"来源数"：5 个来源声明了也一样拦。
    if contract.sources_share_state and len(plan.subtasks) > 1:
        add(
            "shared_state",
            "the contract declares the sources share one state, but the plan splits them "
            f"across {len(plan.subtasks)} blocks",
            *(subtask.id for subtask in plan.subtasks),
        )

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
