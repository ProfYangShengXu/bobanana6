"""Adapters that make the wiring real: ``AgentRuntimePort`` and ``ConstraintSource``.

``WorkerLoop`` is not an ``AgentRuntimePort``: the port takes
``(subtask, bundle, ontology, tools, budget) -> Candidate`` while the loop takes
``(task_id, worker_id, messages, route_key)`` and returns a ``LoopResult`` that
carries a verdict, an exit reason and possibly no candidate at all. That gap is
exactly what the step-13 checklist flagged, and :class:`BlockRuntime` closes it
*without* touching the loop's judgment:

  * the loop's three collaborators come from ``assemble_impl/produce.py`` and
    ``services/``;
  * a decline is a legal third exit (``gate.GateVerdict``), so it is reported as
    a decline rather than being flattened into "failed";
  * everything the loop needs to reach a model goes through the ports it was
    handed, so this module never constructs a client.

``PerWorkerCheckpointStore`` closes a second, smaller gap: ``CheckpointStore`` is
keyed by ``task_id`` alone, so two blocks of one task would overwrite each
other's checkpoint and a resume would read the wrong turn.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..agentloop.concurrency import RouteTable, RuntimeGate, SessionRegistry
from ..agentloop.events import EventLog
from ..agentloop.loop import LoopConfig, WorkerLoop
from ..agentloop.recovery import TaintTracker
from ..agentloop.state import Checkpoint
from ..core.checkers.base import CheckContext
from ..core.model import (
    Budget,
    Candidate,
    Constraint,
    ConstraintBundle,
    Ontology,
    SplitPlan,
    Subtask,
    TaskContract,
)
from ..core.ontology import map_field
from ..core.ports import LLMClient, ToolSet
from ..services.checks import BlockJudge
from ..services.registry import build_registry
from ..services.workspace import BoundWorkspace, WorkspaceLedger
from ..sources import SnapshotStore
from ..sources.form import forms_for
from .produce import LoopIOImpl, ResponseFormatSupport, write_region

__all__ = [
    "BlockDeclined",
    "BlockRuntime",
    "BlockRuntimeError",
    "ContractConstraintSource",
    "PerWorkerCheckpointStore",
    "block_bundle",
    "llm_summarizer",
]


#: ★ `#66` (+「引用+变换」那条 / A02)：这三类是【字段级】规则 —— 名称说的是
#: 某个字段/概念该怎么长（形状、单位、口径版本），中间块的产出也该按同一条规则判。
#: 以前它们只点名交付物，于是脏值要等到合并块才爆（`#19` run 4：读块的 `amount`
#: 是嵌套 dict 也全绿），而合并块若替它换算又会被继承守卫拒（"引用+变换"表达不了）。
_FIELD_KINDS: tuple[str, ...] = ("schema", "unit_consistency", "definition_pin")


def block_bundle(
    bundle: ConstraintBundle,
    subtask: Subtask,
    ontology: Ontology | None = None,
) -> ConstraintBundle:
    """The slice of a bundle that judges *this* block.

    ★ ``kernel.admit`` judges one candidate against a bundle. Handing it the full
    bundle would fail every block for its neighbours' missing artifacts -- each
    block would be judged against ``AUTO-T-02`` while only holding its own
    output. Constraints targeting ``*`` are kept: they are global invariants, and
    ``assemble.check_assembly`` re-checks them over the merged pieces.

    ★ `#66`/A02/变换：字段级规则（``_FIELD_KINDS``）在**中间块**上重指向本块的产出 ——
    同一条规则、换个目标。只有规则点名的字段确实由本块声明时才重指向，
    否则会把"邻居的字段"算到它头上。
    """

    declared = set(subtask.output_fields)

    def keep(constraint: Constraint) -> bool:
        return constraint.target in ("*", subtask.id, subtask.output)

    def retarget(constraint: Constraint) -> Constraint | None:
        if constraint.target in ("*", subtask.id, subtask.output) or constraint.kind not in _FIELD_KINDS:
            return None
        if not declared or not _names_a_declared_field(constraint, declared, ontology):
            return None
        spec = dict(constraint.spec)
        if constraint.kind == "schema":
            # ★ 中间产物是【文本】：dtype（float/int）是交付物的约定，读取块交出来的
            #   还是字符串（`#26` 的第一版把 dtype 一起投过去 → 压力题 24 个读块全挂在
            #   `C-fields@T-xx` 上，真模型一跑就现形）。投的是**形状**（列在不在 / 能不能空），
            #   不是最终类型 —— 类型仍由交付物那一关判。
            spec = {
                **spec,
                "columns": [
                    {**column, "dtype": "str"} for column in spec.get("columns", []) if isinstance(column, dict)
                ],
            }
        return replace(constraint, id=f"{constraint.id}@{subtask.id}", target=subtask.output, spec=spec)

    hard = tuple(c for c in bundle.hard if keep(c)) + tuple(
        r for r in (retarget(c) for c in bundle.hard) if r is not None
    )
    soft = tuple(c for c in bundle.soft if keep(c)) + tuple(
        r for r in (retarget(c) for c in bundle.soft) if r is not None
    )
    return replace(bundle, hard=hard, soft=soft)


def _names_a_declared_field(constraint: Constraint, declared: set[str], ontology: Ontology | None) -> bool:
    """Does this field-level rule talk about a field this block produces?"""
    spec = constraint.spec
    columns = spec.get("columns")
    if isinstance(columns, list):  # schema
        names = {str(c.get("name")) for c in columns if isinstance(c, Mapping) and "name" in c}
        return bool(names) and names <= declared
    concept = spec.get("concept")
    if isinstance(concept, str) and concept:  # unit_consistency / definition_pin
        if ontology is None:
            return False
        return any(
            field == concept or map_field(field, ontology) == concept for field in declared
        )
    return False


class BlockRuntimeError(RuntimeError):
    """The runtime produced no candidate. ``exit_reason`` says where it stopped."""

    def __init__(self, message: str, *, exit_reason: str = "unknown", errors: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.exit_reason = exit_reason
        self.errors = errors


class BlockDeclined(BlockRuntimeError):
    """A decline. Not a failure: it is the third legal exit and never retried."""


@dataclass
class PerWorkerCheckpointStore:
    """Key by ``(task_id, worker_id)`` so one task's blocks stop overwriting each other.

    ``CheckpointStore.load(task_id)`` still answers -- with the most recently
    written checkpoint for that task, in stable order -- because the protocol has
    no worker argument. The per-worker view is what a resume actually needs.
    """

    by_worker: dict[tuple[str, str], Checkpoint] = field(default_factory=dict)

    def save(self, checkpoint: Checkpoint) -> None:
        self.by_worker[(checkpoint.task_id, checkpoint.worker_id)] = checkpoint

    def load(self, task_id: str) -> Checkpoint | None:
        matches = sorted(
            (c for (task, _worker), c in self.by_worker.items() if task == task_id),
            key=lambda c: c.worker_id,
        )
        return matches[-1] if matches else None

    def for_worker(self, task_id: str, worker_id: str) -> Checkpoint | None:
        return self.by_worker.get((task_id, worker_id))


def llm_summarizer(
    client: LLMClient,
    *,
    max_tokens: int = 400,
    gate: RuntimeGate | None = None,
    route: str = "default",
) -> Callable[[tuple[Mapping[str, Any], ...]], str]:
    """Summarize dropped messages with one model call (the compress callback).

    Without this the compressor can only drop; with it the dropped span is
    replaced by a summary. Both are recoverable, so this changes context quality,
    not correctness -- which is why it is a callable and not a loop feature.

    ★ It is a *second* model call path, so it goes through the same
    ``RuntimeGate`` as the loop's call. Over budget it returns an empty summary
    rather than sending the request: dropping without summarizing is honest,
    "throttled but sent anyway" is not.
    """

    def summarize(dropped: tuple[Mapping[str, Any], ...]) -> str:
        acquired = gate is None or gate.acquire(route)
        if not acquired:
            return ""
        try:
            body = "\n".join(f"[{message.get('role')}] {str(message.get('content'))[:2000]}" for message in dropped)
            response = client.complete(
                [
                    {
                        "role": "system",
                        "content": "Summarize the following conversation span in at most five sentences. Keep facts and identifiers.",
                    },
                    {"role": "user", "content": body},
                ],
                [],
                max_tokens,
            )
            return response.text.strip()
        finally:
            if gate is not None and acquired:
                gate.release(route)

    return summarize


@dataclass
class BlockRuntime:
    """``core/ports.py::AgentRuntimePort`` implemented on top of ``WorkerLoop``."""

    client: LLMClient
    store: SnapshotStore
    contract: TaskContract
    #: ★ `#33`: 工作区（代码类产物落盘在这里）；`None` = 这个部署没有工作区。
    workspace: Path | None = None
    #: ★ `#43`: 继承守卫的观测目录（拒收时把池子与交上来的行落盘）；`None` = 不观测。
    observe_dir: Path | None = None
    #: Replaced by the runner with the per-run context (snapshots, injected
    #: ``now``, limits, custom checks) as soon as they exist.
    context: CheckContext = field(default_factory=CheckContext)
    budget: Budget = field(default_factory=lambda: Budget(max_calls=64, max_wall_s=3600, max_output=8192))
    event_log: EventLog = field(default_factory=EventLog)
    checkpoints: PerWorkerCheckpointStore = field(default_factory=PerWorkerCheckpointStore)
    route_table: RouteTable = field(default_factory=lambda: RouteTable(routes={}))
    #: One limiter for the whole block, shared with the summarizer callback, so a
    #: second model path cannot quietly bypass the throttle.
    gate: RuntimeGate = field(default_factory=RuntimeGate)
    required_scope: str = "full"
    compression: Any = None
    summarize: Callable[[tuple[Mapping[str, Any], ...]], str] | None = None
    #: ``subtask -> newest edit time of its outputs`` (0.0 when nothing is on disk).
    edit_clock: Callable[[Subtask], float] = lambda _subtask: 0.0
    #: Monotonic seconds for tool evidence timestamps; injected, never read here.
    epoch: Callable[[], float] = time.monotonic
    #: Structured-output support, learned once per run and shared by every block.
    response_format: ResponseFormatSupport = field(default_factory=ResponseFormatSupport)
    last_candidate: Candidate | None = None
    last_judge: BlockJudge | None = None

    # -- the port ---------------------------------------------------------- #

    def run(
        self,
        subtask: Subtask,
        bundle: ConstraintBundle,
        ontology: Ontology,
        tools: ToolSet,
        budget: Budget,
    ) -> Candidate:
        judge = BlockJudge(
            bundle=block_bundle(bundle, subtask, ontology),
            subtask=subtask,
            context=self.context,
            required_scope=self.required_scope,
            last_edit_at=self.edit_clock(subtask),
        )
        # ★ `#47`: the block's own working area, plus the ledger that turns its
        #   direct writes into traceable products. Both are per block; the ledger
        #   only starts observing when the block first uses channel 2, so a block
        #   that never does pays nothing and its reading is unchanged.
        region = write_region(self.contract, subtask)
        ledger = WorkspaceLedger(root=self.workspace, region=region)
        workspace = BoundWorkspace(root=self.workspace, write_region=region, ledger=ledger)
        tools = replace(tools, workspace=workspace)
        # ★ `#32`: 派活前按任务声明的形式规整载体（encoding / eol）—— 规整清单来自契约，动作只有一套。
        registry = build_registry(tools, self.store.read, forms=forms_for(self.contract.sources))
        io = LoopIOImpl(
            client=self.client,
            toolset=tools,
            registry=registry,
            judge=judge,
            subtask=subtask,
            # ★ The port handed us the budget; that declaration is what caps the
            #   model call -- not a number this adapter keeps for itself.
            budget=budget,
            source_id=subtask.id,
            log=self.event_log,
            task_id=self.contract.task_id,
            workspace=self.workspace,
            observe_dir=self.observe_dir,
            write_region=region,
            ledger=ledger,
            epoch=self.epoch,
            response_format=self.response_format,
        )
        loop = WorkerLoop(
            io,
            config=LoopConfig(required_scope=self.required_scope),
            store=self.checkpoints,
            gate=self.gate,
            routes=self.route_table,
            sessions=SessionRegistry(),
            log=self.event_log,
            taint=TaintTracker(),
            compression=self.compression,
            summarize=self.summarize,
        )
        try:
            result = loop.run(task_id=self.contract.task_id, worker_id=subtask.id, messages=(), route_key=subtask.axis)
        finally:
            # ★ No orphan processes: the code session and every background job the
            #   block started are owned by this block and die with it.
            workspace.close()
        self.last_judge = judge
        if result.candidate is not None:
            self.last_candidate = result.candidate
            return result.candidate
        raise (
            BlockDeclined(f"block {subtask.id} declined", exit_reason=result.exit_reason, errors=result.errors)
            if result.declined
            else BlockRuntimeError(
                f"block {subtask.id} produced no candidate ({result.exit_reason})",
                exit_reason=result.exit_reason,
                errors=result.errors,
            )
        )

    def _last_edit_at(self, subtask: Subtask) -> float:
        """When this block's outputs were last touched, for ``gate.is_stale``.

        Injected: the loop must not read the clock, and the runner knows the
        output paths while this adapter does not.
        """
        return self.edit_clock(subtask)


@dataclass(frozen=True)
class ContractConstraintSource:
    """``ConstraintSource`` over the parsed contract document.

    The port takes a ``TaskContract`` while per-block constraints can only be
    compiled once a plan exists, so the document is injected here and the passed
    contract is checked for identity instead of being re-parsed.
    """

    document: Any

    def compile(self, contract: TaskContract, plan: SplitPlan, ontology: Ontology) -> ConstraintBundle:
        if contract.task_id != self.document.contract.task_id:
            raise ValueError(
                f"contract {contract.task_id!r} does not match the parsed document {self.document.contract.task_id!r}"
            )
        from ..contract import compile_bundle

        return compile_bundle(self.document, plan)
