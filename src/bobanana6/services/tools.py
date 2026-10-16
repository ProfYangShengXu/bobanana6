"""The tool surface, implemented.

``core/ports.py`` declares ``SemanticsTools`` / ``TaskTools`` / ``CheckTools`` /
``SourceTools`` and the ``ToolSet`` that binds them to one subtask. This module
supplies the code; the *permission* rule stays on the stable side
(``ToolSet.require_upstream``), so a worker cannot widen its own input set by
asking differently.

Every tool answers with plain data derived from the bound values. None of them
decides whether a block passed -- that is ``core/kernel.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.checkers import check_kind
from ..core.checkers.base import CheckContext
from ..core.model import (
    Candidate,
    Constraint,
    Ontology,
    Snapshot,
    SourceRef,
    SplitPlan,
    Subtask,
    TaskContract,
)
from ..core.ontology import map_field, pinned_version, resolve_unit, validate_code
from ..core.ports import (
    CheckTools,
    SemanticsTools,
    SourceTools,
    TaskTools,
    ToolSet,
    TraceTools,
    WorkspaceTools,
)
from .traces import TRACE_SEARCH_LIMIT, TraceError, TraceStore

__all__ = [
    "BoundChecks",
    "BoundSemantics",
    "BoundSources",
    "BoundTask",
    "BoundTraces",
    "build_toolset",
]


@dataclass(frozen=True)
class BoundSemantics(SemanticsTools):
    """Answers about the shared semantics. Advice only -- the gate is elsewhere."""

    ontology: Ontology

    def lookup(self, term: str) -> Mapping[str, Any]:
        concept = self.ontology.concepts.get(term)
        if concept is None:
            return {"term": term, "found": False}
        unit = resolve_unit(concept.id, self.ontology)
        return {
            "term": term,
            "found": True,
            "concept": concept.id,
            "label": concept.label,
            "kind": concept.kind,
            "domain": concept.domain,
            "parent": concept.parent,
            "aliases": list(concept.aliases),
            "unit": None if unit is None else {"symbol": unit.symbol, "base": unit.base, "scale": unit.scale},
            "definition_version": pinned_version(concept.id, self.ontology),
        }

    def map(self, field_name: str) -> str:
        return map_field(field_name, self.ontology) or "unmapped"

    def check(self, usage: Any) -> tuple[bool, str]:
        """Check one claimed usage: does the concept exist, and does the unit/code fit?"""
        if not isinstance(usage, Mapping):
            return False, "usage must be a mapping with a 'concept' key"
        concept_id = usage.get("concept")
        if not isinstance(concept_id, str) or not concept_id:
            return False, "usage.concept must be a non-empty string"
        if concept_id not in self.ontology.concepts:
            return False, f"concept {concept_id!r} is not declared in this ontology"
        unit = usage.get("unit")
        if isinstance(unit, str) and unit:
            declared = resolve_unit(concept_id, self.ontology)
            if declared is None:
                return False, f"concept {concept_id!r} has no declared unit"
            if declared.symbol != unit:
                return False, f"concept {concept_id!r} is declared in {declared.symbol!r}, not {unit!r}"
        code_system = usage.get("code_system")
        code = usage.get("code")
        if isinstance(code_system, str) and isinstance(code, str) and not validate_code(
            code, code_system, self.ontology
        ):
            return False, f"code {code!r} is not in code system {code_system!r}"
        return True, "declared in the ontology"


@dataclass(frozen=True)
class BoundTask(TaskTools):
    """The task as this block may see it.

    ``allowed_inputs`` mirrors the stable-side permission rule; the registry
    enforces it through ``ToolSet.require_upstream``, so the two cannot drift
    silently.
    """

    subtask: Subtask
    #: ★ `#39`: 这个字段原来叫 `contract`，把同名方法 `contract()` **遮蔽**了 —— dataclass 把字段
    #:   写进实例 `__dict__`，而类体里的 `def` 只是非数据描述符，优先级低于实例字典。
    #:   后果：`task.contract` 这个工具调到的是一份 `TaskContract` **实例**，于是永远抛
    #:   `TypeError: 'TaskContract' object is not callable`（`#36`/`#38` 的归档里能看到它）。
    #:   字段改名即可，端口 `core/ports.py::TaskTools.contract()` 与工具名都不动。
    contract_document: TaskContract
    upstream_parts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    allowed_inputs: tuple[str, ...] = ()
    #: ★ 契约声明的「哪份来源是题面」（已声明的 source ref；空 = 不声明）。
    #: `produce.py` 用它把题面**全文**内联进每个块的 brief —— 八个块读同一份原文，
    #: 而不是各自去取、各自理解。这里只搬运，不读盘、不判定。
    statement: str = ""

    def mysubtask(self) -> Mapping[str, Any]:
        block = self.subtask
        return {
            "id": block.id,
            "goal": block.goal,
            "inputs": list(block.inputs),
            "output": block.output,
            "axis": block.axis,
            "mode": block.mode,
            "output_fields": list(block.output_fields),
            # ★★ 本块要读的来源窗口（`core/model.py::Subtask.reads`）：`(ref, 起始行, 结束行)`，
            #   0 起、右开。空 = 不按窗口读。没有这一格，窗口就只是 `goal` 里的一句散文 ——
            #   这一格把它变成模型能直接照着调的 `sources.read(ref, offset, limit)`。
            "reads": [{"ref": ref, "start": start, "end": end} for ref, start, end in block.reads],
        }

    def upstream(self, name: str) -> Mapping[str, Any]:
        if name not in self.allowed_inputs:
            raise PermissionError(f"input {name!r} is not declared by subtask {self.subtask.id!r}")
        return dict(self.upstream_parts.get(name, {}))

    def contract(self) -> TaskContract:
        return self.contract_document


@dataclass(frozen=True)
class BoundSources(SourceTools):
    """Snapshots this block is authorized to read.

    ``snapshot("")`` means "the block's own authorized source": the registry
    binds the call, the caller never names a resource it was not granted. With
    more than one authorized source an explicit ref is required, because picking
    one for the caller is exactly the kind of silent choice this project avoids.
    """

    authorized: tuple[SourceRef, ...]
    fetched: Mapping[str, Snapshot] = field(default_factory=dict)

    def refs(self) -> tuple[str, ...]:
        return tuple(source.ref for source in self.authorized)

    def locator(self, ref: str) -> str:
        """The carrier's declared locator (a hint for the payload, never a permission)."""
        for source in self.authorized:
            if source.ref == ref:
                return source.locator
        return ""

    def resolve(self, ref: str) -> str:
        if ref:
            if ref not in self.refs():
                raise PermissionError(f"source {ref!r} is not authorized for this block")
            return ref
        if len(self.authorized) == 1:
            return self.authorized[0].ref
        raise PermissionError(
            f"this block has {len(self.authorized)} authorized sources; the call must name one of {self.refs()!r}"
        )

    def snapshot(self, ref: str) -> Snapshot:
        resolved = self.resolve(ref)
        snapshot = self.fetched.get(resolved)
        if snapshot is None:
            raise LookupError(f"source {resolved!r} was authorized but not fetched")
        return snapshot


#: ``check.local`` keeps at most this many characters of any one constraint's
#: judge sentence. A judge command's stdout is unbounded; a self-check that floods
#: the block's context costs more than the reading is worth.
CHECK_LOCAL_DETAIL_LIMIT = 400

#: What a machine detail (a shell command, a regex, ...) is replaced with before
#: it could reach the block.
WITHHELD_DETAIL = "<machine detail withheld>"


def _spec_strings(spec: Any) -> tuple[str, ...]:
    """Every non-empty string nested anywhere in a constraint ``spec``.

    Used only to *withhold* machine detail from ``check.local``: a value the
    contract declared in ``spec`` (``command`` / a regex / a nested
    ``columns[].pattern`` / ...) is never echoed back, so the block cannot learn
    to satisfy the checker instead of the rule. Recursion covers mappings inside
    sequences, not just sequences of strings.
    """
    found: list[str] = []
    if isinstance(spec, str):
        if spec:
            found.append(spec)
    elif isinstance(spec, Mapping):
        for value in spec.values():
            found.extend(_spec_strings(value))
    elif isinstance(spec, (list, tuple, set, frozenset)):
        for item in spec:
            found.extend(_spec_strings(item))
    return tuple(found)


def _check_local_detail(constraint: Constraint, facts: Sequence[str]) -> str:
    """The judge's own sentence, machine detail withheld and length-bounded."""
    detail = "; ".join(str(fact) for fact in facts)
    for machine in _spec_strings(constraint.spec):
        detail = detail.replace(machine, WITHHELD_DETAIL)
    # A judge command prints freely; collapse whitespace and cap the length so one
    # verbose checker cannot take over the block's context.
    detail = " ".join(detail.split())
    if len(detail) > CHECK_LOCAL_DETAIL_LIMIT:
        detail = detail[:CHECK_LOCAL_DETAIL_LIMIT] + "…"
    return detail


@dataclass(frozen=True)
class BoundChecks(CheckTools):
    """``check.*`` tools. The verdict comes from ``services.checks.BlockJudge``.

    ``local`` is *not* that verdict: it is a read-only self-check the block may
    run before submitting (see :meth:`local`). ``check.provenance`` /
    ``assemble_dryrun`` keep their existing, unrelated payloads.
    """

    block: str
    verdict: Any = None
    provenance_report: Any = None
    split_report: Any = None
    plan: SplitPlan | None = None
    #: ★ The **contract-declared** constraints (``document.hard + document.soft``)
    #: -- the same source ``produce._constraints_entry`` reads, never
    #: ``judge.bundle`` (which carries the synthesized ``AUTO-*`` / ``C-*@T-xx``
    #: retarget copies that no contract declared).
    constraints: tuple[Constraint, ...] = ()  # blame: fail-closed — 省略即"拿不到契约声明的约束"⇒自查答 null，方向保守
    #: The one belonging rule (``produce.constraint_applies_to``), injected so
    #: this module reuses it instead of growing a second copy. ``None`` = not
    #: wired ⇒ ``local`` is fail-closed (``None``).
    applies_to: Callable[[Constraint, Subtask], bool] | None = None  # blame: fail-closed — 省略即"归属规则没接上"⇒自查答 null，绝不猜
    subtask: Subtask | None = None  # blame: fail-closed — 省略即"不知道本块是谁"⇒自查答 null
    #: A **fresh** check context per call -- its own ``RecordedEvidence``, so a
    #: self-check can never append to the judge's evidence stream. ``None`` =
    #: not wired ⇒ ``local`` is fail-closed.
    local_context: Callable[[], CheckContext] | None = None  # blame: fail-closed — 省略即"没有隔离的判据上下文"⇒自查答 null（绝不共用判官的证据流）
    #: Lazily reads the block's current product (before it is submitted). ``None``
    #: = no product reader wired; a missing on-disk deliverable is reported by the
    #: checkers themselves rather than passed off as green.
    product: Callable[[], Candidate | None] | None = None  # blame: fail-closed — 省略即"读不到产物"⇒判据照实报缺失，不当通过

    def local(self) -> Any:
        """★ Read-only self-check: what the declared constraints say about this block now.

        A block calls this *before* it submits, and the judge's ``verdict`` does
        not exist yet -- returning it answered ``null`` at exactly the moment the
        block needed an answer. This recomputes the constraints the judge will
        apply, against the block's current product, and reports each one:

            ``id``       the declared constraint name (e.g. ``C-length``)
            ``expects``  the contract's own ``rationale`` -- the same sentence the
                         brief already shows the block (same source, one path)
            ``status``   ``passed`` / ``failed`` / ``unverified`` -- three states
            ``passed``   ``True`` (it holds now) / ``False`` (the check ran and
                         said no) / ``None`` (not decidable yet, i.e. ``unverified``)
            ``detail``   the judge's own sentence, machine detail withheld and
                         truncated to ``CHECK_LOCAL_DETAIL_LIMIT`` characters

        Three properties are load-bearing:

        * **Only declared constraints.** The source is ``self.constraints``
          (declared hard/soft), never the synthesized ``AUTO-*`` / ``C-*@T-xx``
          copies in ``judge.bundle``; belonging reuses ``constraint_applies_to``.
        * **Never a verdict.** Nothing here writes ``verdict``, rejects a
          candidate, or changes any judgment -- this is advice the block asked for.
        * **No machine detail.** Every string found in the constraint's ``spec``
          (the shell command, a regex, ...) is replaced before it reaches the
          block, so the block sees the judge's human sentence only.

        And one distinction this report must keep: **"cannot be checked yet" is
        not "failed".** ``status="unverified"`` is a third state -- the check ran
        and did not pass, but it declared (``CheckOutcome.pending_input``, set at
        its own failure point) that the input it reads is not on hand yet: the
        producer builds it at block end, e.g. the citation points. Its ``detail``
        says why and when it will be checked. The third state can never swallow a
        real failure -- only a check that itself attributes its failure to a
        missing input is reported this way, and the judge still fails such a row
        at submission.

        Side effect, named: a ``custom``/``shell_command`` constraint can only be
        answered by running it, so this runs the contract's own judge command --
        the same command, in the same cwd, the judge runs anyway; nothing
        model-supplied is executed, and a fresh evidence recorder per call keeps
        the judge's gate untouched (``BlockJudge.verify`` reads ``evidence.last()``).

        Fail-closed: no declared constraints, none that belong to this block, or
        an unwired contract/context ⇒ ``None`` (the pre-change shape).
        """
        if (
            not self.constraints
            or self.subtask is None
            or self.applies_to is None
            or self.local_context is None
        ):
            return None
        applicable = [
            constraint
            for constraint in self.constraints
            if self.applies_to(constraint, self.subtask)
        ]
        if not applicable:
            return None
        candidate = self.product() if self.product is not None else None
        if candidate is None:
            # No product on disk yet: the checkers still answer honestly (a shell
            # command reports the missing file; a content check reports no artifact).
            candidate = Candidate(subtask_id=self.subtask.id, source_id=self.subtask.id)
        context = self.local_context()
        rows: list[dict[str, Any]] = []
        for constraint in applicable:
            reason = ""
            try:
                outcome = check_kind(constraint, candidate, context)
                ok_now = bool(outcome.ok)
                facts: Sequence[str] = outcome.facts
                # ★ The check declares *why* it failed. If its input is not on
                #   hand yet (produced at block end), that is the third state, not
                #   a failure. An exception, a pass, or a failure the check did
                #   not attribute to a missing input stays passed/failed.
                if not ok_now:
                    reason = outcome.pending_input
            except Exception as exc:  # noqa: BLE001 - a broken self-check must not kill the block
                ok_now = False
                facts = (f"self-check raised {type(exc).__name__}: {exc}",)
            if reason:
                status, passed = "unverified", None
                detail = _check_local_detail(constraint, (reason,))
            else:
                status, passed = ("passed" if ok_now else "failed"), ok_now
                detail = _check_local_detail(constraint, facts)
            rows.append(
                {
                    "id": constraint.id,
                    "expects": constraint.rationale,
                    # ``status`` is the authoritative three-valued field (the same
                    # vocabulary the completion gate already uses); ``passed``
                    # stays for compatibility -- ``None`` exactly when the row is
                    # not decidable yet. Both are set in this one place.
                    "status": status,
                    "passed": passed,
                    "detail": detail,
                }
            )
        return {
            "block": self.block,
            "applies_to": str(self.subtask.output or ""),
            "constraints": rows,
        }

    def provenance(self) -> Any:
        return self.provenance_report

    def assemble_dryrun(self) -> Mapping[str, Any]:
        """A *dry run* of the split-legality checks that concern this block.

        The full assembly needs the other blocks' artifacts, which do not exist
        yet; the interface/merge-cost half applies to the plan itself and can be
        run now. Whatever the runner could not run is reported as such rather
        than being passed off as a result.
        """
        if self.split_report is None:
            return {"ok": None, "reason": "no split report was supplied to this tools instance"}
        relevant = [v for v in self.split_report.violations if self.block in v.subtask_ids]
        return {
            "ok": not relevant,
            "block": self.block,
            "codes": sorted({v.check for v in relevant}),
            "details": [f"{v.check}: {v.detail}" for v in relevant],
        }


#: ★ 块拿不拿得到 `sources`，由【契约声明了来源】决定，与 `axis` 的名字无关。
#: 曾经这里是一张按轴名写的白名单（`("source", "module")`），于是每新增一个切分维度
#: 都要回来补一次 —— `#33` 补过 `"module"`，加 `"product"` 时又漏了，而漏掉的后果是
#: **静默的**：块照常派下去，只是模型手上没有任何来源，只能交空表（13f 实测 0/8）。
#: 轴名是**切口**，权限是**能力**，两者本来就不该互相决定。删除白名单即治本。


@dataclass
class BoundTraces(TraceTools):
    """★ 上游**过程**的检索入口 —— 可见范围与调用上限都在这里（执行层，不是提示词）。

    为什么把这两件事放在执行层：

    ```
    可见范围  process 里常有半成品与错路，扩散到平行块/下游只会污染 ⇒ 只给【上游闭包】
    调用上限  过程是补充不是替代 ⇒ 每块每次跑测 12 次封顶，超了**具名拒**，不静默截断
    ```
    """

    store: TraceStore
    #: 本块的**上游闭包**（已跑完的块 id）。空 ⇒ `traces.*` 根本不会被广告。
    visible: tuple[str, ...] = ()  # blame: fail-closed — 省略即"看不到任何上游"⇒ 检索只会被具名拒
    block: str = ""  # blame: whitelist — 只用于报文里的自我标识，不参与任何判定
    #: 一次跑测里这个块能调多少次 `traces.*`（search 与 read **合并**计）。
    limit: int = TRACE_SEARCH_LIMIT  # blame: fail-closed — 省略即给一个**有上限**的调用次数，方向保守
    used: int = 0  # blame: fail-closed — 省略即"还没花过额度"，只会更早触发上限

    def _spend(self, tool: str) -> None:
        if self.used >= self.limit:
            raise TraceError(
                f"{tool} refused: this block already used {self.used}/{self.limit} trace calls "
                f"(violated: per-block trace-call limit; expected: <= {self.limit}; got: {self.used + 1}). "
                "Process retrieval is a supplement -- work from what you already have."
            )
        self.used += 1

    def search(self, query: str) -> Mapping[str, Any]:
        # ★ 先校验、后计费：被拒的坏调用**不该吃掉**这个块本来就有限的检索额度
        #   （否则 12 次空 query 就能把它锁死 —— 那是壳的 bug，不是它的错）。
        text = str(query or "").strip()
        if not text:
            raise TraceError(
                "traces.search needs a non-empty query (violated: query is empty; "
                "expected: a keyword such as a tool name, an error word, or a file name)"
            )
        if not self.visible:
            raise TraceError("traces.search refused: this block has no completed upstream blocks")
        self._spend("traces.search")
        found = self.store.search(text, self.visible)
        return {**found, "searched_blocks": list(self.visible)}

    def read(self, block_id: str, seq: str) -> Mapping[str, Any]:
        name = str(block_id or "")
        if name not in set(self.visible):
            raise TraceError(
                f"traces.read refused: {name!r} is not in this block's upstream closure "
                f"(violated: visibility; expected: one of {list(self.visible)!r}; got: {name!r})"
            )
        wanted = _seq_numbers(seq)
        self._spend("traces.read")
        steps = self.store.read(name, wanted)
        if not steps:
            recorded = [entry.seq for entry in self.store.entries(name)]
            raise TraceError(
                f"traces.read refused: block {name!r} has no recorded step {list(wanted)!r} "
                f"(violated: unknown step; expected: one of {recorded!r}; got: {list(wanted)!r})"
            )
        return {"steps": list(steps), "count": len(steps)}


def _seq_numbers(raw: Any) -> tuple[int, ...]:
    """`seq` 既收 `3` / `3,5,7` 这种文本，也收真正的数组（宽松端点/模型写法不一）。"""
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        text = str(raw or "").strip()
        if not text:
            raise TraceError("traces.read needs seq (e.g. `3` or `3,5,7`); violated: seq is empty")
        values = [piece.strip() for piece in text.split(",")]
    out: list[int] = []
    for value in values:
        token = str(value).strip()
        if not token.isdigit():
            raise TraceError(
                f"traces.read needs numeric seq values; got {token!r} "
                "(violated: not a number; expected: e.g. `3` or `3,5,7`)"
            )
        out.append(int(token))
    return tuple(out)


def build_toolset(
    *,
    plan: SplitPlan,
    subtask: Subtask,
    contract: TaskContract,
    ontology: Ontology,
    authorized: tuple[SourceRef, ...],
    fetched: Mapping[str, Snapshot],
    upstream: Mapping[str, Mapping[str, Any]],
    checks: BoundChecks,
    inherited_sources: tuple[str, ...] = (),
    workspace: WorkspaceTools | None = None,
    statement: str = "",
    #: ★ ARTEX 机制 ②：上游块的**过程**。`None` = 本块没有可检索的上游（入口块）
    #: ⇒ `traces.*` 不广告。装配方按 DAG 依赖算出的上游闭包构造它。
    traces: TraceTools | None = None,  # blame: fail-closed — 省略即"本块没有可检索的上游"，traces.* 不广告
) -> ToolSet:
    """Bind the tool surface to one block.

    ``sources.snapshot`` is granted whenever the contract declares any source
    (``authorized`` is non-empty) —— ★ 它**不再**看 ``axis``：轴名只说明块是怎么切出来的
    （切口），不说明它该不该有读来源的能力。按 ``axis`` 过滤曾让新维度（``field`` / ``product``）
    的块静默失去权限 —— 照着被派活，手上却没有任何素材。the declared-input
    permission is ``subtask.inputs`` and nothing else. ``inherited_sources`` are the snapshots
    the declared inputs came from (a merge block cites those, never its own).

    ★ `#47`: ``workspace`` is the block's own working area (or ``None`` when the
    deployment declared none) -- the write/execute tools are advertised only when
    it is present, so a capability the deployment never granted cannot be asked for.

    ★★ 工具面收束（本轮）：这里把**按需层**的两条判据算出来交给 `ToolSet` ——
      `semantics_declared`（契约里真有语义声明吗）/ `has_deliverable`（本块产出交付物吗）。
      两条都**从现成的数据现算**，拿不到就是 False（fail-closed）。
    """
    # ★ 判据 = 【契约声明了来源】。与 `axis` 无关 —— 理由见本文件上方那段注记。
    #   一个块的素材只有两个来处：声明的来源、或上游产物。白名单按轴名放行时，
    #   新维度的块两个都拿不到，而它【照样会被派活】。
    sources = BoundSources(authorized=authorized, fetched=fetched) if authorized else None
    return ToolSet(
        subtask=subtask.id,
        semantics=BoundSemantics(ontology=ontology),
        task=BoundTask(
            subtask=subtask,
            contract_document=contract,
            upstream_parts=upstream,
            allowed_inputs=subtask.inputs,
            statement=statement,
        ),
        check=checks,
        sources=sources,
        workspace=workspace,
        traces=traces,
        # ★★ 按需层两条（都从现成数据现算，拿不到就是 False）：
        semantics_declared=declares_semantics(contract),
        has_deliverable=subtask.output in contract.deliverable_names,
        allowed_inputs=subtask.inputs,
        inherited_sources=inherited_sources,
    )


def declares_semantics(contract: TaskContract) -> bool:
    """★ 契约里**真有**语义声明吗 —— 从契约现算（不硬编码、不按题写死）。

    判据只有一条：**任一交付物字段声明了类型以外的东西**（`domain` / `granularity` /
    `source` —— 见 `DeliverableField.declared`，那是"一个字段声明"的唯一定义）。
    裸类型契约（`{text: str}`）⇒ 没声明 ⇒ **不给** `semantics.*`：
    那种题上 `semantics.lookup` / `map` / `check` 没有可查的东西，只是占位噪声。
    """
    return any(
        declaration.declared
        for deliverable in contract.deliverables
        for declaration in deliverable.schema.values()
    )


def pinned_versions(ontology: Ontology, concepts: tuple[str, ...]) -> dict[str, str]:
    """The definition versions this block must pin, for ``Candidate.definition_versions``."""
    pinned: dict[str, str] = {}
    for concept in concepts:
        version = pinned_version(concept, ontology)
        if version is not None:
            pinned[concept] = version
    return pinned
