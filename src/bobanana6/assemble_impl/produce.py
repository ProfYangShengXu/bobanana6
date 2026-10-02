"""Turn model output into core values -- the only place that does.

The worker loop never sees an ``Artifact`` or a ``Candidate`` from outside: it
sees JSON text and tool calls. This module is the boundary that parses that text
**strictly**. A malformed answer must fail visibly (the loop classifies it and
retries) rather than degrade into a half-built candidate, because a candidate
that quietly loses its provenance would be admitted by every check that trusts
it.

Only the parsing boundary lives here; no constraint is judged here.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..agentloop.events import EventLog
from ..agentloop.gate import GateVerdict
from ..agentloop.loop import LoopIO, LoopState, TurnProposal
from ..agentloop.tools import ToolCall, ToolResult
from ..core.model import (
    Artifact,
    Candidate,
    Claim,
    Column,
    Provenance,
    Subtask,
    content_digest,
)
from ..core.ports import LLMClient, ToolSet
from ..services.checks import BlockJudge
from ..services.registry import ToolRegistry

__all__ = ["CandidateFormatError", "LoopIOImpl", "parse_candidate", "tool_schemas"]


def _declined(text: str) -> bool:
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(document, Mapping) and bool(document.get("declined"))


def _decline_reason(text: str) -> str:
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError):  # pragma: no cover - guarded by _declined
        return ""
    if isinstance(document, Mapping):
        return str(document.get("reason") or "")
    return ""


def _name_of(calls: Sequence[ToolCall], call_id: str) -> str:
    for call in calls:
        if call.call_id == call_id:
            return call.name
    return ""


class CandidateFormatError(ValueError):
    """The model's answer could not be read as a candidate."""


def tool_schemas(toolset: ToolSet) -> list[dict[str, Any]]:
    """The advertised tool surface, in the shape a model expects."""
    schemas: list[dict[str, Any]] = []
    for spec in toolset.specs():
        properties = {name: {"type": "string"} for name in spec.params}
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.returns or spec.name,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(spec.params),
                        "additionalProperties": False,
                    },
                },
            }
        )
    return schemas


def _columns(raw: Any, rows: Sequence[Mapping[str, Any]]) -> tuple[Column, ...]:
    if raw is None:
        names = sorted({key for row in rows for key in row})
        return tuple(Column(name=name, dtype="str") for name in names)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise CandidateFormatError("columns must be a list")
    columns: list[Column] = []
    for item in raw:
        if isinstance(item, str):
            columns.append(Column(name=item, dtype="str"))
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            columns.append(
                Column(
                    name=str(item["name"]),
                    dtype=str(item.get("dtype") or "str"),
                    nullable=bool(item.get("nullable", True)),
                )
            )
        else:
            raise CandidateFormatError(f"column {item!r} is neither a name nor a {{name, dtype}} object")
    return tuple(columns)


def _claims(raw: Any, *, snapshot_ref: str, locator: str) -> tuple[Claim, ...]:
    if not raw:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise CandidateFormatError("claims must be a list")
    claims: list[Claim] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise CandidateFormatError(f"claim {item!r} must be an object")
        name = item.get("field")
        source = item.get("source_id")
        if not isinstance(name, str) or not name or not isinstance(source, str) or not source:
            raise CandidateFormatError(f"claim {item!r} needs a non-empty 'field' and 'source_id'")
        claims.append(
            Claim(
                field=name,
                value=item.get("value"),
                source_id=source,
                provenance=Provenance(value_ref=f"{name}:{source}", snapshot_ref=snapshot_ref, locator=locator),
            )
        )
    return tuple(claims)


def parse_candidate(
    text: str,
    *,
    subtask: Subtask,
    source_id: str,
) -> Candidate:
    """Read one answer into a ``Candidate``, or fail loudly.

    Row-level provenance is synthesized from the snapshot the answer cites:
    ``required_refs`` asks for ``<artifact>:row=<n>`` per row, so citing one
    snapshot per row is exactly enough for the chain to be walkable.
    """
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CandidateFormatError(f"the answer is not JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise CandidateFormatError("the answer must be a JSON object")
    rows = document.get("rows", [])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise CandidateFormatError("'rows' must be a list")
    clean_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise CandidateFormatError(f"row {row!r} is not an object")
        clean_rows.append(dict(row))

    snapshot_ref = document.get("ref")
    if not isinstance(snapshot_ref, str) or not snapshot_ref:
        snapshot_ref = source_id
    locator = str(document.get("body_path") or "")
    artifact_text = document.get("text")
    artifact = Artifact(
        name=subtask.output,
        columns=_columns(document.get("columns"), clean_rows),
        rows=tuple(clean_rows),
        text=artifact_text if isinstance(artifact_text, str) else None,
    )
    provenance = [
        Provenance(value_ref=f"{subtask.output}:row={index}", snapshot_ref=snapshot_ref, locator=locator)
        for index in range(len(clean_rows))
    ]
    if isinstance(artifact_text, str) and artifact_text:
        provenance.extend(
            Provenance(value_ref=f"{subtask.output}:line={index}", snapshot_ref=snapshot_ref, locator=locator)
            for index, line in enumerate(artifact_text.splitlines())
            if line
        )
    versions = document.get("definition_versions")
    return Candidate(
        subtask_id=subtask.id,
        # ★ The source that asserted this, when the answer names one. Two blocks
        #   reading the same source are not independent corroboration, so this
        #   field has to be the source and not the block.
        source_id=str(document.get("source_id") or document.get("ref") or source_id),
        hint=str(document.get("hint") or content_digest(text)),
        files={},
        provenance=tuple(provenance),
        artifacts=(artifact,),
        claims=_claims(document.get("claims"), snapshot_ref=snapshot_ref, locator=locator),
        definition_versions=dict(versions) if isinstance(versions, Mapping) else {},
    )


@dataclass
class LoopIOImpl(LoopIO):
    """The loop's three collaborators: produce, execute, verify.

    ``produce`` is where the model is called, ``execute`` is the registry, and
    ``verify`` is the block judge -- three different layers, so a failure in one
    cannot be mistaken for a failure in another.
    """

    client: LLMClient
    toolset: ToolSet
    registry: ToolRegistry
    judge: BlockJudge
    subtask: Subtask
    source_id: str = "runtime"
    max_tokens: int = 512
    #: The event stream the tool round writes to. ``execute`` is the single point
    #: every tool result passes through, so it is where they are recorded.
    log: EventLog | None = None
    task_id: str = ""
    epoch: Callable[[], float] = time.monotonic
    calls: int = 0
    failed_tools: set[str] = field(default_factory=set)

    def prompt(self, state: LoopState) -> list[dict[str, Any]]:
        """Stable instructions first, this block next, the transcript last.

        Order matters: the first two parts never change within a worker, so the
        prefix stays cacheable, and everything dynamic is already at the tail of
        ``state.messages`` (``AGENTS.md §6``: no cache-hostile prompt assembly).
        """
        block = self.toolset.task.mysubtask()
        authorized: list[str] = []
        sources = getattr(self.toolset, "sources", None)
        if sources is not None and hasattr(sources, "refs"):
            authorized = list(sources.refs())
        head = [
            {
                "role": "system",
                "content": (
                    "You are one worker in a split plan. Answer with a single JSON object "
                    'shaped {"ref": <source id>, "columns": [...], "rows": [...]}. '
                    "Use the tools you were given; you may only read what they expose."
                ),
            },
            {"role": "user", "content": json.dumps({"subtask": dict(block), "authorized_sources": authorized}, ensure_ascii=False)},
        ]
        return [*head, *(dict(message) for message in state.messages)]

    def produce(self, state: LoopState) -> TurnProposal:
        self.calls += 1
        response = self.client.complete(self.prompt(state), tool_schemas(self.toolset), self.max_tokens)
        calls = self.registry.calls(response.tool_calls)
        if calls:
            return TurnProposal(tool_calls=calls, text=response.text)
        if not response.text.strip():
            return TurnProposal(text="", detail="the model returned neither text nor a tool call")
        if _declined(response.text):
            # ★ The third legal exit: the worker says it cannot get this
            #   information. It is not a failure and the loop must not retry it.
            return TurnProposal(declined=True, detail=_decline_reason(response.text))
        candidate = parse_candidate(response.text, subtask=self.subtask, source_id=self.source_id)
        return TurnProposal(candidate=candidate, text=response.text)

    def execute(self, calls: tuple[ToolCall, ...]) -> tuple[ToolResult, ...]:
        results = self.registry.execute(calls)
        now = self.epoch()
        for result in results:
            name = _name_of(calls, result.call_id)
            # ★ "recovered" means this tool failed earlier in this block and has
            #   now succeeded -- counting a retried win as a first-try win is
            #   exactly the pollution tool_metrics splits out.
            recovered = result.ok and name in self.failed_tools
            if self.log is not None:
                self.log.record(
                    kind="tool_result",
                    task_id=self.task_id or self.subtask.id,
                    worker_id=self.subtask.id,
                    at=now,
                    ok=result.ok,
                    detail={
                        "args_ok": result.error_type != "invalid_tool_arguments",
                        "recovered": recovered,
                        "tool": name,
                    },
                )
            if name and not result.ok:
                self.failed_tools.add(name)
        return results

    def verify(self, candidate: Candidate) -> GateVerdict:
        return self.judge.verify(candidate)
