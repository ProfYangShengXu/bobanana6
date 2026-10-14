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

import os
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
from ..agentloop.tool_output import ToolOutputPolicy
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
from ..core.ports import EscalationPort, LLMClient, ToolSet
from ..services.checks import BlockJudge
from ..services.registry import build_registry
from ..services.tools import BoundTraces
from ..services.traces import TraceStore, upstream_closure
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


#: ★★ 重试退避的默认秒数。**默认 0 = 不退避**：
#: 单测与离线跑测走的都是同一条装配路径（`acceptance/harness.py` 也走这里），
#: 默认给 60 会让**每一个**驱动服务端失败的用例真睡一分钟 —— 既有读数就不再是"一字不变"了。
#: ⇒ 真跑由入口显式打开：`.bobanana6/ab/run_bb6.py` 里 `BB6_RETRY_BACKOFF_S=60`（用户口径）。
DEFAULT_RETRY_BACKOFF_S = 0.0


def retry_backoff_from_env(
    source: Mapping[str, str] | None = None,  # blame: whitelist — 只为可测注入（假 env），生产传 None 即读 os.environ
) -> float:
    """`BB6_RETRY_BACKOFF_S`（秒）—— 缺省 0（不睡）；真跑入口把它设成 60。

    ★ 与 `ToolOutputPolicy.from_env` 同一条规矩：**env 只在这一个装配点读**，
    loop 自己不读 env（否则单测会依赖环境状态）。
    ★ 读不出来 / 是负数 ⇒ 回默认值（不静默变成别的秒数）。
    """
    raw = str((source if source is not None else os.environ).get("BB6_RETRY_BACKOFF_S", "")).strip()  # blame: fail-closed — 取不到就回默认（不睡），不静默变成别的秒数
    if not raw:
        return DEFAULT_RETRY_BACKOFF_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_RETRY_BACKOFF_S
    return value if value >= 0 else DEFAULT_RETRY_BACKOFF_S


def escalation_from_env(
    source: Mapping[str, str] | None = None,  # blame: whitelist — 只为可测注入（假 env），生产传 None 即读 os.environ
) -> EscalationPort | None:
    """`BB6_ESCALATE` 非空且不是 `0`/`false` ⇒ 建一个走本机 agent 桥的出口；否则 `None`。

    ★ 为什么**默认关**：出口是"最后一步"，它会把整跑的墙钟拉长（等的是外面，不是自己）。
      真实跑测要显式打开（`.bobanana6/ab/run_bb6.py` 里设 `BB6_ESCALATE=1`），
      单测与离线路径**一个字都不变**。
    ★ 与 `ToolOutputPolicy.from_env` / `retry_backoff_from_env` 同一条规矩：
      **env 只在这一个装配点读**，loop 自己不读 env。
    """
    # blame: fail-closed — 缺 env 即"不开出口"⇒ 行为与改前逐字相同（默认关）
    raw = str((source if source is not None else os.environ).get("BB6_ESCALATE", "")).strip().lower()
    # blame: fail-closed — 空/0/false/no/off 一律当作"不开"
    if not raw or raw in {"0", "false", "no", "off"}:
        return None
    from ..services.escalation import ContactorEscalation

    return ContactorEscalation()


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
    #: ★★ 契约声明的判据（`content_checks.invariants`，原样）—— 交给块装配器做**归属**：
    #: 约束哪份产物（`applies_to`），就给产出那份产物的块。判据里带**规格常量**
    #: （容差 `tol` 等），不交下去的话模型只能按常识写，判定器按契约的尺子量 ⇒ 拒收，
    #: 且模型不知道自己错在哪。★ 空元组 = 拿不到契约 ⇒ 一条都不给（fail-closed）。
    content_checks: tuple[Mapping[str, Any], ...] = ()  # blame: fail-closed — 省略即"拿不到契约"⇒一条都不给（既有读数一字不变）；方向保守，不会凭空放宽判定
    #: ★★ 契约声明的**消费方式**（`content_checks.comparisons`，原样）—— 同上，交给装配器做归属。
    #: 判据说"值该满足什么"，这里说"产物会被怎么对待"（逐字段机器比对 / 容差多大）。
    #: ★ 判分器的实现细节（分隔符白名单、哪列开恩）**不从这条路进来** —— 那是偷看口。
    comparisons: tuple[Mapping[str, Any], ...] = ()  # blame: fail-closed — 省略即"不知道会被怎么比对"⇒不加要求（既有读数一字不变）
    #: ★ `#33`: 工作区（代码类产物落盘在这里）；`None` = 这个部署没有工作区。
    workspace: Path | None = None
    #: ★ `#43`: 继承守卫的观测目录（拒收时把池子与交上来的行落盘）；`None` = 不观测。
    observe_dir: Path | None = None
    #: Replaced by the runner with the per-run context (snapshots, injected
    #: ``now``, limits, custom checks) as soon as they exist.
    context: CheckContext = field(default_factory=CheckContext)
    budget: Budget = field(default_factory=lambda: Budget(max_calls=64, max_wall_s=3600, max_output=8192))  # blame: fail-closed — 省略即给一组**有上限**的预算（64 调用 / 3600s / 8192 输出），方向保守；真正的风险是它被复制成第二份
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
    # blame: fail-closed — 省略即"这份产物的最新改动时间是【无限新】"⇒ 任何既有验证都算过期（is_stale 恒真）。改前是 `lambda: 0.0`，省略即恒假 ⇒ 旧验证永久有效（危险方向）。
    edit_clock: Callable[[Subtask], float] = lambda _subtask: float("inf")
    #: Monotonic seconds for tool evidence timestamps; injected, never read here.
    epoch: Callable[[], float] = time.monotonic
    #: Structured-output support, learned once per run and shared by every block.
    response_format: ResponseFormatSupport = field(default_factory=ResponseFormatSupport)
    last_candidate: Candidate | None = None
    last_judge: BlockJudge | None = None
    #: ★★ 过程级信息交换（ARTEX 机制 ②）：整个 run **共用一份**过程记录 ——
    #: 每个块都往同一份里写，下游按【上游闭包】从同一份里读。runner 持有它，
    #: 块跑完时把它落进 git-backed 痕迹仓库（与产物同一个 commit）。
    traces: TraceStore = field(default_factory=TraceStore)  # blame: fail-closed — 省略即给一份**空**的过程记录（没有可检索内容），不会凭空造

    # -- the port ---------------------------------------------------------- #

    def run(
        self,
        subtask: Subtask,
        bundle: ConstraintBundle,
        ontology: Ontology,
        tools: ToolSet,
        budget: Budget,
        #: ★★ 规格 ①：全 DAG 的 subtask 清单 —— 「哪些键进 system」要按**全 DAG**算，
        #: 不是按「跑到哪算到哪」。拿不到就一个字都不动（fail-closed）。
        dag_subtasks: tuple[Subtask, ...] = (),
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
        # ★★ `frozen_paths`：**谁都不许写**的路径 = 所有块的交付物 ∪ 任务声明的来源。
        #   写区互斥改判据之后，它就是唯一挡住"撞别人东西"的那道门（见
        #   `produce.py::_reconcile_workspace` 与 `services/workspace.py::_check_region`）。
        #   用途：块可以写临时文件，但碰不了别人的交付物、也改不了声明的载体。
        frozen = tuple(
            sorted(
                {deliverable.name for deliverable in self.contract.deliverables}
                | {source.locator for source in self.contract.sources if source.locator}
            )
        )
        ledger = WorkspaceLedger(root=self.workspace, region=region)
        workspace = BoundWorkspace(
            root=self.workspace, write_region=region, frozen_paths=frozen, ledger=ledger
        )
        tools = replace(tools, workspace=workspace)
        # ★★ 过程级信息交换（ARTEX 机制 ②）：把【上游闭包】内的过程检索交给这个块。
        #   可见范围从 DAG 依赖**现算**（`upstream_closure`），不硬编码任何块名。
        #   ★ 闭包为空（入口块 / 拿不到全 DAG）⇒ `traces.*` **不广告** ——
        #     与 `sources`/`workspace` 同一条规则：拿不到的能力不该被广告出来。
        visible_upstream = upstream_closure(subtask, dag_subtasks)
        if visible_upstream:
            tools = replace(
                tools,
                traces=BoundTraces(store=self.traces, visible=visible_upstream, block=subtask.id),
            )
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
            frozen_paths=frozen,
            # ★★ 契约声明的判据（约束哪份产物，就给产出那份产物的块）——
            #   判定器拿什么尺子量你，你就得看到那把尺子（见 `_content_checks_entry`）。
            content_checks=self.content_checks,
            comparisons=self.comparisons,
            # ★ 写工具的账本与答案 `files` 的账本是**同一个**对象：一块的总写入量
            #   要一起累加（见 `services/workspace.py::WriteBudget`）。
            write_budget=workspace.write_budget,
            ledger=ledger,
            epoch=self.epoch,
            response_format=self.response_format,
            dag_subtasks=dag_subtasks,
            # ★ 同一个单点（`LoopIOImpl.execute`）既写 `tool_result` 事件，也写过程记录。
            traces=self.traces,
        )
        loop = WorkerLoop(
            io,
            # ★★ 轮次上限跟着**声明的 budget** 走，不是这个 adapter 自己留的数。
            #   原来这里写死 `LoopConfig()`（`max_turns=6`），与契约声明的
            #   `max_calls=64` 打架 —— 6 悄悄赢，块永远够不到自己的预算。
            #   实测（13f `alive` 轮）：块读 3 份来源 + 连摸 4 次数据，然后
            #   `exit=budget_exhausted` —— 死在 6 轮上，不是死在能力上。
            #   本文件上方那句注释本来就是这个意思："that declaration is what
            #   caps the model call -- not a number this adapter keeps for itself"。
            config=LoopConfig(
                required_scope=self.required_scope,
                max_turns=budget.max_calls,
                # ★★ 重试退避（用户口径：**失败后等一分钟再打**）——
                #   类里默认 0（单测/离线不该被真睡的秒数拖住），**生产值在这个装配点给**，
                #   与 `ToolOutputPolicy.from_env` 同一条规矩：env 只在这里读，loop 自己不读 env。
                retry_backoff_s=retry_backoff_from_env(),
            ),
            # ★★ 外部出口：`BB6_ESCALATE=1` 才建（默认 `None` ⇒ 行为与改前逐字相同）。
            #   与上面那条同一条规矩：env 只在这个装配点读。
            escalation=escalation_from_env(),
            store=self.checkpoints,
            gate=self.gate,
            routes=self.route_table,
            sessions=SessionRegistry(),
            log=self.event_log,
            taint=TaintTracker(),
            compression=self.compression,
            summarize=self.summarize,
            # ★ 工具返回进上下文的那道门：阈值/头/尾从 env 读（默认 4000/800/800），
            #   全文落进工作区的忽略区 `_shell/`（不是产物）。都从这里注入，loop 自己
            #   不读 env、不碰磁盘、不认识 `services/`（依赖方向不反）。
            tool_output=ToolOutputPolicy.from_env(),
            spill_tool_output=workspace.spill_tool_output,
        )
        try:
            result = loop.run(task_id=self.contract.task_id, worker_id=subtask.id, messages=(), route_key=subtask.axis)
        finally:
            # ★ No orphan processes: the code session and every background job the
            #   block started are owned by this block and die with it.
            workspace.close()
        self.last_judge = judge
        # ★ 规格 ②：块**结束时**判（不是在整跑结束后）—— 只有这时候判出来，
        #   才能进【下一个块的 brief】。扫的是这一块自己的 transcript。
        self.last_cleanliness = io.cleanliness(tuple(result.messages))
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

    #: ★ 规格 ②：这一块结束时算出来的 cleanliness（干净 / 污染）。
    #: 它是【产物的属性】，不是块的成功/失败 —— 所以放在这里，不放进判定。
    last_cleanliness: dict[str, Any] = field(default_factory=dict)

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
