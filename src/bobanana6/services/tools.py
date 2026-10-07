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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.model import (
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
    WorkspaceTools,
)

__all__ = [
    "BoundChecks",
    "BoundSemantics",
    "BoundSources",
    "BoundTask",
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


@dataclass(frozen=True)
class BoundChecks(CheckTools):
    """``check.*`` tools. The verdict comes from ``services.checks.BlockJudge``."""

    block: str
    verdict: Any = None
    provenance_report: Any = None
    split_report: Any = None
    plan: SplitPlan | None = None

    def local(self) -> Any:
        return self.verdict

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
        allowed_inputs=subtask.inputs,
        inherited_sources=inherited_sources,
    )


def pinned_versions(ontology: Ontology, concepts: tuple[str, ...]) -> dict[str, str]:
    """The definition versions this block must pin, for ``Candidate.definition_versions``."""
    pinned: dict[str, str] = {}
    for concept in concepts:
        version = pinned_version(concept, ontology)
        if version is not None:
            pinned[concept] = version
    return pinned
