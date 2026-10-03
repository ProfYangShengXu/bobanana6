"""Turn model output into core values -- the only place that does.

The worker loop never sees an ``Artifact`` or a ``Candidate`` from outside: it
sees JSON text and tool calls. This module is the boundary that parses that text
**strictly**. A malformed answer must fail visibly (the loop classifies it and
retries) rather than degrade into a half-built candidate, because a candidate
that quietly loses its provenance would be admitted by every check that trusts
it.

Only the parsing boundary lives here; no constraint is judged here.

Structured output is built in three layers here (lec9:237 -- *never assume the
first answer is right*):

  * **before** -- :func:`candidate_response_format` asks the service for the shape;
  * **during** -- the schema itself (``CANDIDATE_SCHEMA``) is the tightening;
  * **after** -- a rejected answer raises :class:`CandidateFormatError` carrying
    *how to fix it*, which the loop feeds back to the model.

The three are complementary, not alternatives: layer 3 is the only one that can
say anything at all when a service ignores layers 1-2.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

from ..agentloop.events import EventLog
from ..agentloop.gate import GateVerdict
from ..agentloop.loop import LoopIO, LoopState, TurnProposal
from ..agentloop.tools import ToolCall, ToolResult
from ..core.model import (
    Artifact,
    Candidate,
    Claim,
    Column,
    ModelResponse,
    Provenance,
    Subtask,
    content_digest,
)
from ..core.ports import LLMClient, ToolSet
from ..services.checks import BlockJudge
from ..services.registry import ToolRegistry

__all__ = [
    "CANDIDATE_SCHEMA",
    "EXPECTED_SHAPE",
    "CandidateFormatError",
    "LoopIOImpl",
    "ResponseFormatSupport",
    "candidate_response_format",
    "parse_candidate",
    "rejects_response_format",
    "tool_schemas",
]


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


#: The answer contract, in one place: the schema sent to the service and the text
#: fed back on a rejected answer are two renderings of *this*.
EXPECTED_SHAPE = (
    '{"ref": "<source id>", "columns": [{"name": "<column>", "dtype": "str"}], '
    '"rows": [{"<column>": <value>}]}'
)

#: ``json_schema`` form. Deliberately **not** ``strict: true``: strict mode requires
#: every property to be required, which would force the model to invent ``hint`` /
#: ``text`` / ``claims`` on every answer. The tightening that matters here is the
#: shape of ``columns`` and ``rows``.
CANDIDATE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["columns", "rows"],
    "properties": {
        "ref": {"type": "string"},
        "source_id": {"type": "string"},
        "hint": {"type": "string"},
        "text": {"type": "string"},
        "definition_versions": {"type": "object", "additionalProperties": {"type": "string"}},
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "dtype": {"type": "string"},
                    "nullable": {"type": "boolean"},
                },
            },
        },
        "rows": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "source_id"],
                "properties": {"field": {"type": "string"}, "value": {}, "source_id": {"type": "string"}},
            },
        },
    },
}


def candidate_response_format() -> Mapping[str, Any]:
    """The ``response_format`` payload for one answer (the *before* layer)."""
    return {"type": "json_schema", "json_schema": {"name": "candidate", "schema": dict(CANDIDATE_SCHEMA)}}


#: A schema/structured-output request the service does not understand.
_SCHEMA_MARKERS = ("response_format", "json_schema", "json schema", "structured output")
_REJECTION_MARKERS = ("not supported", "unsupported", "unrecognized", "unknown", "unexpected", "invalid")


def rejects_response_format(error: BaseException) -> bool:
    """Whether a failure says "I do not understand structured output".

    Only client-shaped statuses count: a 500 is not a capability answer, and a
    timeout says nothing about the parameter. Read off the exception rather than
    from an adapter type so this module keeps talking to the port only.
    """
    status = getattr(error, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool) or status not in (400, 404, 415, 422):
        return False
    text = str(error).lower()
    return any(marker in text for marker in _SCHEMA_MARKERS) and any(marker in text for marker in _REJECTION_MARKERS)


@dataclass
class ResponseFormatSupport:
    """What this service has accepted so far, learned once per run.

    Capability negotiation must not become a hard failure, and it must not become a
    per-call probe either: the first refusal turns the layer off for the rest of the
    run, so a service that ignores ``response_format`` costs exactly one extra call.
    """

    enabled: bool = True
    refused: bool = False


class CandidateFormatError(ValueError):
    """The model's answer could not be read as a candidate.

    Carries two *different* representations on purpose:

      * the message -- prose for logs, reports and ``state.errors``;
      * :meth:`feedback` -- the structured fix the model is told, rendered by
        ``recovery.reformat_message`` (code + problem + what was sent + the shape).
    """

    #: ★ Declared: retrying the identical request cannot fix a malformed answer.
    #: Without this the message was matched against a word list, and
    #: "the answer is not JSON" matched nothing -> ``unknown`` -> ``retry_local``,
    #: which burned the budget re-sending the same request.
    reason = "format_error"

    def __init__(
        self,
        problem: str,
        *,
        code: str = "invalid_answer",
        got: str = "",
        expected: str = EXPECTED_SHAPE,
    ) -> None:
        super().__init__(problem)
        self._feedback: dict[str, Any] = {"code": code, "problem": problem, "got": got, "expected": expected}

    def feedback(self) -> Mapping[str, Any]:
        """How to fix it -- read by ``agentloop.recovery.declared_feedback``."""
        return dict(self._feedback)


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


#: ``reject(code, problem)`` raises with the fix attached.
Reject = Callable[[str, str], "NoReturn"]


def _columns(raw: Any, rows: Sequence[Mapping[str, Any]], reject: Reject) -> tuple[Column, ...]:
    if raw is None:
        names = sorted({key for row in rows for key in row})
        return tuple(Column(name=name, dtype="str") for name in names)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        reject("columns_not_a_list", "'columns' is present but it is not an array")
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
            reject("column_not_an_object", f"column entry {item!r} is neither a name nor a {{name, dtype}} object")
    return tuple(columns)


def _claims(raw: Any, *, snapshot_ref: str, locator: str, reject: Reject) -> tuple[Claim, ...]:
    if not raw:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        reject("claims_not_a_list", "'claims' is present but it is not an array")
    claims: list[Claim] = []
    for item in raw:
        if not isinstance(item, Mapping):
            reject("claim_not_an_object", f"claim entry {item!r} is not an object")
        name = item.get("field")
        source = item.get("source_id")
        if not isinstance(name, str) or not name or not isinstance(source, str) or not source:
            reject("claim_missing_keys", f"claim entry {item!r} needs a non-empty 'field' and 'source_id'")
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
    # ★ One place builds the fix, and every rejection carries it: what the model
    #   sent, which rule it broke, and the shape that would be accepted.
    def reject(code: str, problem: str) -> NoReturn:
        raise CandidateFormatError(problem, code=code, got=text)

    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        head = text.lstrip()
        where = "the answer is empty" if not head else f"the first character is {head[0]!r}, not '{{'"
        reject("not_json", f"{where} (JSON parse failed: {exc})")
    if not isinstance(document, Mapping):
        reject("not_an_object", f"the answer is a JSON {type(document).__name__}, not an object")
    rows = document.get("rows", [])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        reject("rows_not_a_list", "'rows' is missing or is not an array of objects")
    clean_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            reject("row_not_an_object", f"a row is {type(row).__name__}, not an object of column: value")
        clean_rows.append(dict(row))

    snapshot_ref = document.get("ref")
    if not isinstance(snapshot_ref, str) or not snapshot_ref:
        snapshot_ref = source_id
    locator = str(document.get("body_path") or "")
    artifact_text = document.get("text")
    artifact = Artifact(
        name=subtask.output,
        columns=_columns(document.get("columns"), clean_rows, reject),
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
        claims=_claims(document.get("claims"), snapshot_ref=snapshot_ref, locator=locator, reject=reject),
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
    #: Structured-output support, shared with the rest of the run so a refusal is
    #: learned once instead of once per block.
    response_format: ResponseFormatSupport = field(default_factory=ResponseFormatSupport)
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
        response = self._complete(state)
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

    def wants_structured_output(self) -> bool:
        """Whether this call should ask for the answer shape.

        ★ Not on the very first call of a block: until the model has had one turn
        it may legitimately need to call a tool, and a strict answer schema is
        exactly what stops it. From the second turn on, either it has data (and
        should answer) or its first answer was unusable (and structure is what it
        needs) -- both want the schema.
        """
        return self.response_format.enabled and self.calls > 1

    def _complete(self, state: LoopState) -> ModelResponse:
        """One model call, with capability negotiation around the answer schema.

        ★ A service that does not understand ``response_format`` must not turn into
        a hard failure: it is asked once, refused once, and then the layer is off for
        the rest of the run. That is negotiation, not a retry policy -- the call is
        only repeated when the failure *is* the capability answer.
        """
        prompt = self.prompt(state)
        schemas = tool_schemas(self.toolset)
        if not self.wants_structured_output():
            return self.client.complete(prompt, schemas, self.max_tokens)
        try:
            return self.client.complete(
                prompt, schemas, self.max_tokens, response_format=candidate_response_format()
            )
        except Exception as exc:
            if not rejects_response_format(exc):
                raise
            self.response_format.enabled = False
            self.response_format.refused = True
            return self.client.complete(prompt, schemas, self.max_tokens)

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
