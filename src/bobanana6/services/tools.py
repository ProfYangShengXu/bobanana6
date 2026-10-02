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
from ..core.ports import CheckTools, SemanticsTools, SourceTools, TaskTools, ToolSet

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
    contract: TaskContract
    upstream_parts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    allowed_inputs: tuple[str, ...] = ()

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
        return self.contract


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
) -> ToolSet:
    """Bind the tool surface to one block.

    ``sources.snapshot`` is granted when the block's axis says its material comes
    from sources (``axis == "source"``); the declared-input permission is
    ``subtask.inputs`` and nothing else.
    """
    sources = BoundSources(authorized=authorized, fetched=fetched) if subtask.axis == "source" else None
    return ToolSet(
        subtask=subtask.id,
        semantics=BoundSemantics(ontology=ontology),
        task=BoundTask(subtask=subtask, contract=contract, upstream_parts=upstream, allowed_inputs=subtask.inputs),
        check=checks,
        sources=sources,
        allowed_inputs=subtask.inputs,
    )


def pinned_versions(ontology: Ontology, concepts: tuple[str, ...]) -> dict[str, str]:
    """The definition versions this block must pin, for ``Candidate.definition_versions``."""
    pinned: dict[str, str] = {}
    for concept in concepts:
        version = pinned_version(concept, ontology)
        if version is not None:
            pinned[concept] = version
    return pinned
