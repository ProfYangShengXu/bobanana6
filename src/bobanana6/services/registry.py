"""The tool registry: what a tool call *is*, filled by the caller's side.

``agentloop.tools.ToolCall`` carries ``effect`` and ``targets``, and until now
those were self-reported by whoever built the call -- so a write could be
mistaken for a read and ``plan_tool_segments`` would stop treating writes as a
barrier. The registry owns them instead: a tool declares its effect once, and
the target names are the argument values the tool says it touches.

This module also owns the **payload shape** the tools answer with. The worker
loop never sees core value objects: it sees JSON text, and
``assemble_impl/produce.py`` is the only place that turns that text back into a
``Candidate``.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..agentloop.tools import (
    TOOL_EFFECTS,
    ToolCall,
    ToolCallError,
    ToolResult,
    invalid_arguments_result,
    parse_tool_arguments,
)
from ..core.model import Artifact, Column, Snapshot, canonical_row
from ..core.ports import ToolSet
from ..sources.form import SourceForm, decode_body, form_for

__all__ = [
    "TOOL_NAMES",
    "RegisteredTool",
    "ToolRegistry",
    "artifact_payload",
    "build_registry",
    "snapshot_payload",
]

#: How a call's arguments reach a handler.
Handler = Callable[[Mapping[str, Any]], str]


#: Every tool name this deployment can serve. ``consist._tool_missing`` compares
#: a constraint's ``spec.requires`` against this inventory, so it must be the
#: real list, not a guess.
TOOL_NAMES: tuple[str, ...] = (
    "semantics.lookup",
    "semantics.map",
    "semantics.check",
    "task.mysubtask",
    "task.upstream",
    "task.contract",
    "check.local",
    "check.provenance",
    "check.assemble_dryrun",
    "sources.snapshot",
)


@dataclass(frozen=True)
class RegisteredTool:
    """One tool: its schema, its effect, its target rule and its implementation."""

    name: str
    parameters: tuple[str, ...]
    handler: Handler
    returns: str = ""
    bound_to: str | None = None
    effect: str = "read"
    #: Argument names whose values name the resources this call touches. Used
    #: only for overlap admission, never to decide the outcome.
    target_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("RegisteredTool.name must not be empty")
        if self.effect not in TOOL_EFFECTS:
            raise ValueError(f"unknown tool effect {self.effect!r}; expected one of {TOOL_EFFECTS}")


@dataclass
class ToolRegistry:
    """Dispatches calls and fills the fields the caller must not fill itself."""

    tools: tuple[RegisteredTool, ...] = ()
    handled: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate tool names: {sorted(names)}")

    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def effect(self, name: str) -> str:
        tool = self.get(name)
        return "read" if tool is None else tool.effect

    def get(self, name: str) -> RegisteredTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def targets(self, name: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        tool = self.get(name)
        if tool is None:
            return ()
        values = [str(arguments[key]) for key in tool.target_args if arguments.get(key) not in (None, "")]
        return tuple(values) or (name,)

    def calls(self, raw_calls: Sequence[Mapping[str, Any]]) -> tuple[ToolCall, ...]:
        """Translate model-shaped calls into ``ToolCall`` values.

        An unparseable argument list is kept as a call with empty arguments, so
        it still gets a result later: a dangling tool call is not allowed to
        exist (AGENTS §7①).
        """
        calls: list[ToolCall] = []
        for index, raw in enumerate(raw_calls):
            call_id = str(raw.get("id") or f"call-{index + 1}")
            function = raw.get("function")
            if not isinstance(function, Mapping):
                calls.append(ToolCall(call_id=call_id, name="<malformed>", arguments={}, raw_arguments=""))
                continue
            name = str(function.get("name") or "<malformed>")
            raw_arguments = function.get("arguments")
            text = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments or {})
            try:
                arguments = dict(parse_tool_arguments(text))
            except ToolCallError:
                arguments = {}
            calls.append(
                ToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    raw_arguments=text,
                    effect=self.effect(name),
                    targets=self.targets(name, arguments),
                )
            )
        return tuple(calls)

    def execute(self, calls: Sequence[ToolCall]) -> tuple[ToolResult, ...]:
        results: list[ToolResult] = []
        for call in calls:
            tool = self.get(call.name)
            if tool is None:
                results.append(
                    ToolResult(
                        call_id=call.call_id,
                        ok=False,
                        content=f"unknown tool {call.name!r}; available: {list(self.names())!r}",
                        error_type="unknown_tool",
                        # ★ the caller named a tool it does not have (AGENTS §7⑫)
                        cause="model",
                    )
                )
                continue
            try:
                # ★ The fallback must hand the parser the *same* type the parser
                #   accepts. `call.raw_arguments or call.arguments` looked safe but
                #   fed a Mapping into a `json.loads`-only function, so a call built
                #   with valid `arguments` and no raw text was refused as "not valid
                #   JSON" -- a wrong message pointing at the wrong party. The fork's
                #   strict parser contract is untouched (it still refuses to repair
                #   model-emitted junk); only our own fallback now serializes what
                #   it already parsed.
                arguments = dict(
                    parse_tool_arguments(
                        call.raw_arguments or json.dumps(dict(call.arguments), ensure_ascii=False)
                    )
                )
            except ToolCallError as exc:
                results.append(invalid_arguments_result(call.call_id, str(exc)))
                continue
            try:
                content = tool.handler(arguments)
            except Exception as exc:  # noqa: BLE001 - a broken tool is visible, never a silent success
                results.append(
                    ToolResult(
                        call_id=call.call_id,
                        ok=False,
                        content=f"{type(exc).__name__}: {exc}",
                        error_type=type(exc).__name__,
                        # ★ §7⑫: read the layer off the exception when it declares
                        #   one (``SourceError.blame = "tool"``). An unlabelled
                        #   third-party exception declares nothing, and attribution
                        #   falls back to its own order rather than guessing here.
                        cause=str(getattr(exc, "blame", "") or ""),
                    )
                )
                continue
            self.handled.append(call.name)
            results.append(ToolResult(call_id=call.call_id, ok=True, content=content))
        return tuple(results)


# --------------------------------------------------------------------------- #
# Payloads: the only shapes a worker ever sees
# --------------------------------------------------------------------------- #


def artifact_payload(artifact: Artifact) -> dict[str, Any]:
    return {
        "name": artifact.name,
        # ★ A10 (found by a real-model run): the unit a producer established for
        #   its column has to travel with the part, otherwise the consumer cannot
        #   see it and the "established or not" state dies at the hand-off.
        "columns": [
            {"name": c.name, "dtype": c.dtype, "nullable": c.nullable, "unit": c.unit} for c in artifact.columns
        ],
        # ★ ②: the payload the consumer sees carries the canonical spelling too --
        #   an absent cell is an absent key, not a null it must learn to translate.
        "rows": [canonical_row(row) for row in artifact.rows],
        "text": artifact.text,
    }


def _rows_from_csv(text: str) -> tuple[list[Column], list[dict[str, Any]]]:
    reader = csv.DictReader(io.StringIO(text))
    names = list(reader.fieldnames or [])
    rows = [dict(row) for row in reader]
    return [Column(name=name, dtype="str") for name in names], rows


def _rows_from_json(text: str) -> tuple[list[Column], list[dict[str, Any]]]:
    document = json.loads(text)
    if isinstance(document, Mapping):
        document = [document]
    if not isinstance(document, list):
        raise TypeError("a JSON snapshot must be an object or a list of objects")
    rows = [dict(row) for row in document if isinstance(row, Mapping)]
    names = sorted({key for row in rows for key in row})
    return [Column(name=name, dtype="str") for name in names], rows


def snapshot_payload(
    ref: str,
    snapshot: Snapshot,
    body: bytes,
    form: SourceForm | None = None,
) -> str:
    """The JSON a ``sources.snapshot`` call answers with.

    The payload is artifact-shaped on purpose: it is what the worker is expected
    to hand back as its answer, and what ``produce.py`` parses into an
    ``Artifact`` with a ``Provenance`` back to the frozen carrier.

    ★ `#32`: this is the **派活前** delivery point -- the bytes are normalized to
    the form the task declared (`encoding` / `eol`) *here*, before any block sees
    them, and what was normalized (or assumed) rides along in ``source_form``.
    Undeclared and clean stays byte-identical to the old payload (no extra key).
    """
    decoded = decode_body(body, form)
    text = decoded.text
    suffix = snapshot.body_path.suffix.lower()
    if suffix == ".csv":
        columns, rows = _rows_from_csv(text)
    elif suffix == ".json":
        columns, rows = _rows_from_json(text)
    else:
        columns, rows = [Column(name="text", dtype="str")], [{"text": text}]
    payload: dict[str, Any] = {
        "ref": ref,
        "source_kind": snapshot.source_kind,
        "captured_at": snapshot.captured_at,
        "body_path": str(snapshot.body_path),
        "columns": [{"name": c.name, "dtype": c.dtype, "nullable": c.nullable} for c in columns],
        "rows": rows,
    }
    if decoded.note:
        payload["source_form"] = dict(decoded.note)
    return json.dumps(payload, ensure_ascii=False)


def build_registry(
    toolset: ToolSet,
    body_reader: Callable[[Snapshot], bytes],
    forms: Mapping[str, SourceForm] | None = None,
) -> ToolRegistry:
    """Register every tool the bound ``ToolSet`` advertises.

    The registry is built *from* the toolset, so a tool cannot exist in the
    advertised schema without an implementation, and an implementation cannot
    be reached without being advertised. ``task.upstream`` goes through the
    stable-side permission rule (``ToolSet.require_upstream``) before it reads
    anything.
    """

    def payload(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    def lookup(args: Mapping[str, Any]) -> str:
        return payload(toolset.semantics.lookup(str(args.get("term", ""))))

    def map_field(args: Mapping[str, Any]) -> str:
        name = str(args.get("field", ""))
        return payload({"field": name, "concept": toolset.semantics.map(name)})

    def check_usage(args: Mapping[str, Any]) -> str:
        ok, reason = toolset.semantics.check(args.get("usage", args))
        return payload({"ok": ok, "reason": reason})

    def mysubtask(args: Mapping[str, Any]) -> str:
        return payload(toolset.task.mysubtask())

    def upstream(args: Mapping[str, Any]) -> str:
        name = str(args.get("name", ""))
        toolset.require_upstream(name)
        return payload(toolset.task.upstream(name))

    def upstream_all(args: Mapping[str, Any]) -> str:
        # ★ ①(b): 名字来自【本块声明的输入】，不是调用方给的列表 ——
        #   授权仍然由稳定侧说了算（require_upstream 一件一件过）。
        names = [str(name) for name in getattr(toolset, "allowed_inputs", ()) or ()]
        for name in names:
            toolset.require_upstream(name)
        return payload({name: dict(toolset.task.upstream(name)) for name in names})

    def contract(args: Mapping[str, Any]) -> str:
        value = toolset.task.contract()
        return payload(
            {
                "task_id": value.task_id,
                "goal": value.goal,
                "deliverables": [d.name for d in value.deliverables],
                "sources": [s.ref for s in value.sources],
            }
        )

    def check_local(args: Mapping[str, Any]) -> str:
        return payload(toolset.check.local())

    def check_provenance(args: Mapping[str, Any]) -> str:
        return payload(toolset.check.provenance())

    def assemble_dryrun(args: Mapping[str, Any]) -> str:
        return payload(toolset.check.assemble_dryrun())

    def snapshot(args: Mapping[str, Any]) -> str:
        if toolset.sources is None:
            raise PermissionError(f"subtask {toolset.subtask!r} is not authorized to read sources")
        bound = toolset.sources.snapshot(str(args.get("ref", "")))
        return snapshot_payload(bound.ref, bound, body_reader(bound), form_for(forms or {}, bound.ref))

    catalog: Mapping[str, tuple[Handler, tuple[str, ...], tuple[str, ...]]] = {
        "semantics.lookup": (lookup, (), ()),
        "semantics.map": (map_field, (), ()),
        "semantics.check": (check_usage, (), ()),
        "task.mysubtask": (mysubtask, (), ()),
        "task.upstream": (upstream, (), ("name",)),
        "task.upstream_all": (upstream_all, (), ()),
        "task.contract": (contract, (), ()),
        "check.local": (check_local, (), ()),
        "check.provenance": (check_provenance, (), ()),
        "check.assemble_dryrun": (assemble_dryrun, (), ()),
        "sources.snapshot": (snapshot, (), ("ref",)),
    }
    tools: list[RegisteredTool] = []
    for spec in toolset.specs():
        handler, _, targets = catalog.get(spec.name, (None, (), ()))
        if handler is None:
            raise KeyError(f"tool {spec.name!r} is advertised but has no implementation")
        tools.append(
            RegisteredTool(
                name=spec.name,
                parameters=tuple(spec.params),
                handler=handler,
                returns=spec.returns,
                bound_to=spec.bound_to,
                effect="read",  # ★ no tool in this surface mutates state; a write tool must declare it
                target_args=tuple(targets),
            )
        )
    return ToolRegistry(tools=tuple(tools))
