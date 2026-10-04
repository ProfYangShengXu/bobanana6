"""Ports: the boundary between the stable kernel and everything environment
specific.

Every protocol here is *defined* on the stable side and *implemented* outside
it. Nothing in this file imports an implementation, and no implementation
module is named.

Port inventory note: the spec's repo-structure comment lists six ports
(including a ``CandidateSource`` that is never given a signature) while its
port section defines the eight protocols found here. This unit implements the
eight with signatures; ``CandidateSource`` is deliberately not invented. See
the delivery report -- the two documents disagree and must be synced.

Vocabulary note: the environment guard forbids model/network vocabulary inside
``core/``. Two protocol members mandated by the spec itself -- ``LLMClient``
and the ``max_tokens`` / ``approx_tokens`` parameters that belong to it --
contain those substrings. The guard exempts exactly those three identifiers
and nothing else, so any *other* leak still fails.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .model import (
    AssemblyOutcome,
    Budget,
    BundleDelta,
    Candidate,
    ConstraintBundle,
    InputSurvey,
    ModelResponse,
    Ontology,
    Recon,
    Snapshot,
    SnapshotBody,
    SourceRef,
    SplitPlan,
    Subtask,
    TaskContract,
)

__all__ = [
    "AgentIO",
    "AgentRuntimePort",
    "ArbiterPort",
    "CheckTools",
    "ConstraintSource",
    "DecomposerPort",
    "OntologyPort",
    "SemanticsTools",
    "SnapshotReader",
    "SourcePort",
    "SourceTools",
    "TaskTools",
    "ToolSet",
    "ToolSpec",
]


@runtime_checkable
class OntologyPort(Protocol):
    """Whoever supplies the task's shared semantics."""

    def obtain(self, contract: TaskContract, survey: InputSurvey) -> Ontology: ...


@runtime_checkable
class DecomposerPort(Protocol):
    """Whoever cuts the task into subtasks."""

    def split(self, contract: TaskContract, ontology: Ontology, recon: Recon) -> SplitPlan: ...

    def refine(
        self,
        contract: TaskContract,
        ontology: Ontology,
        plan: SplitPlan,
        outcome: AssemblyOutcome,
    ) -> SplitPlan: ...


@runtime_checkable
class ConstraintSource(Protocol):
    """Whoever compiles the constraint bundle for a plan."""

    def compile(self, contract: TaskContract, plan: SplitPlan, ontology: Ontology) -> ConstraintBundle: ...


@runtime_checkable
class AgentRuntimePort(Protocol):
    """Whoever drives one worker through one subtask."""

    def run(
        self,
        subtask: Subtask,
        bundle: ConstraintBundle,
        ontology: Ontology,
        tools: ToolSet,
        budget: Budget,
    ) -> Candidate: ...


@runtime_checkable
class LLMClient(Protocol):
    """One model call.

    Named by the spec even though the name trips a naive vocabulary scan; the
    kernel only ever sees this interface, never an implementation.
    """

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        **kw: Any,
    ) -> ModelResponse: ...


@runtime_checkable
class AgentIO(Protocol):
    """Host capabilities the worker loop needs, kept deliberately narrow."""

    def persist(self, messages: list[dict[str, Any]]) -> None: ...

    def compress(self, messages: list[dict[str, Any]], trigger: str, approx_tokens: int) -> list[dict[str, Any]]: ...

    def context_length(self) -> int: ...

    def note_activity(self, kind: str, detail: str = "") -> None: ...

    @property
    def max_tokens(self) -> int: ...


@runtime_checkable
class ArbiterPort(Protocol):
    """Whoever adds constraints, records conflicts, or rules on a definition."""

    def resolve(
        self,
        bundle: ConstraintBundle,
        ontology: Ontology,
        items: tuple[Any, ...],
    ) -> BundleDelta: ...


@runtime_checkable
class SourcePort(Protocol):
    """Whoever fetches raw material and freezes it into a snapshot."""

    def fetch(self, ref: SourceRef) -> Snapshot: ...


@runtime_checkable
class SnapshotReader(Protocol):
    """Whoever turns a frozen carrier into the rows core may judge (A11).

    Defined on the stable side and implemented outside: the content-level
    provenance check is judgment and belongs in ``core/``, but opening the
    carrier is not something ``core/`` may do (``AGENTS.md §1.1``). The reader
    hands the body over as data.
    """

    def body(self, snapshot: Snapshot) -> SnapshotBody: ...


# --------------------------------------------------------------------------- #
# Tool surface
# --------------------------------------------------------------------------- #


class SemanticsTools(Protocol):
    def lookup(self, term: str) -> Mapping[str, Any]: ...

    def map(self, field_name: str) -> str: ...

    def check(self, usage: Any) -> tuple[bool, str]: ...


class TaskTools(Protocol):
    def mysubtask(self) -> Mapping[str, Any]: ...

    def upstream(self, name: str) -> Mapping[str, Any]: ...

    def contract(self) -> TaskContract: ...


class CheckTools(Protocol):
    def local(self) -> Any: ...

    def provenance(self) -> Any: ...

    def assemble_dryrun(self) -> Any: ...


class SourceTools(Protocol):
    def snapshot(self, ref: str) -> Snapshot: ...


@dataclass(frozen=True)
class ToolSpec:
    name: str
    params: tuple[str, ...]
    returns: str
    bound_to: str | None = None


_BASE_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("semantics.lookup", ("term",), "concept+unit+definition+aliases"),
    ToolSpec("semantics.map", ("field",), "concept_id | unmapped"),
    ToolSpec("semantics.check", ("usage",), "ok/reason"),
    ToolSpec("task.mysubtask", (), "goal/inputs/output/axis/mode", "subtask"),
    ToolSpec("task.upstream", ("name",), "schema+path", "inputs"),
    #: ★ ①(b) 兜底：一次把【全部】声明输入取回来，"忘了取"的代价降到一次调用。
    ToolSpec("task.upstream_all", (), "every declared input, by name", "inputs"),
    ToolSpec("task.contract", (), "interface contract"),
    ToolSpec("check.local", (), "Verdict for this block", "subtask"),
    ToolSpec("check.provenance", (), "provenance report", "subtask"),
    ToolSpec("check.assemble_dryrun", (), "assembly verdict", "subtask"),
    ToolSpec("sources.snapshot", ("ref",), "Snapshot", "authorized sources"),
)


@dataclass(frozen=True)
class ToolSet:
    """The only channel that connects the semantic, task and check layers.

    Tools are *bound* to a subtask: the set a worker holds can only see that
    block. ``upstream`` is restricted to the inputs the subtask declared, and
    the restriction is enforced here rather than requested in a message.

    ``allowed_inputs`` is an inference: the spec binds tools to a subtask id but
    does not say where the declared input names are carried, and the permission
    rule has to be owned by the stable side to be auditable.
    """

    subtask: str
    semantics: SemanticsTools
    task: TaskTools
    check: CheckTools
    sources: SourceTools | None = None
    allowed_inputs: tuple[str, ...] = ()
    #: ★ A11: the snapshots this block did not read itself but inherited from the
    #: parts it consumes. A merge block may cite these and nothing else.
    inherited_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.subtask == "":
            raise ValueError("ToolSet.subtask must not be empty")

    def allows_upstream(self, name: str) -> bool:
        return name in self.allowed_inputs

    def require_upstream(self, name: str) -> None:
        if not self.allows_upstream(name):
            raise PermissionError(f"input {name!r} is not declared by subtask {self.subtask!r}")

    def specs(self) -> tuple[ToolSpec, ...]:
        """The tool schema, independent of any specific runtime."""
        return _BASE_TOOL_SPECS if self.sources is not None else tuple(
            spec for spec in _BASE_TOOL_SPECS if spec.name != "sources.snapshot"
        )
