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
from ..core.model import Artifact, Column, Snapshot, TaskContract, canonical_row
from ..core.ports import ToolSet
from ..sources.form import SourceForm, decode_body, form_for
from .command_guard import inline_script_problem
from .textops import (
    DEFAULT_LIMIT,
    TARGETS,
    TextArgumentError,
    grep_names,
    line_window,
    search_lines,
)
from .workspace import WorkspaceError

__all__ = [
    "TOOL_NAMES",
    "RegisteredTool",
    "ToolRegistry",
    "artifact_payload",
    "build_registry",
    "deliverables_payload",
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
    # ★ `#39`: 这一项原来漏了 —— 它是**注册表真的会服务**的工具（`build_registry` 里挂着
    #   `task.upstream_all`，`core/ports.py::_BASE_TOOL_SPECS` 也声明了它），而 `TOOL_NAMES`
    #   被 `runner.py:210` 当成"部署能服务什么"的清单喂给 `consist._tool_missing` ⇒ 一条
    #   `spec.requires: [task.upstream_all]` 的约束会被误报成"能力不可用"。补成真的。
    "task.upstream_all",
    "task.contract",
    "check.local",
    "check.provenance",
    "check.assemble_dryrun",
    "sources.snapshot",
    # ★ `#47`: 通用读写/执行原语（Hermes 的 read_file / search_files / write_file /
    #   patch / terminal / execute_code 的**行为契约**）。名字走本仓的命名空间约定，
    #   参数域换成本仓的授权模型（见 `core/ports.py::_BASE_TOOL_SPECS`）。
    "sources.read",
    "sources.grep",
    # ★ 读回本块【有权写】的文件（首要就是它自己的交付物）—— 与写共用 `may_write`。
    "files.read",
    "files.write",
    "files.patch",
    "shell.run",
    # ★ 过程检索（ARTEX 机制 ②）：先 search 再 read —— 两段式是**预算**，不是风格。
    "traces.search",
    "traces.read",
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

        ★ `call_id` is made **unique within the response**. The protocol's key space
        has to be one-to-one: an endpoint requires the assistant row to be followed by
        *exactly one* ``tool`` message per declared id, so a model that repeats an id
        would make two calls answerable by one row (and two answers indistinguishable
        from a duplicate). A repeated id gets a deterministic suffix -- the call
        itself (name/arguments) is untouched, which is the part the model must fix.
        """
        calls: list[ToolCall] = []
        used: set[str] = set()
        for index, raw in enumerate(raw_calls):
            base = str(raw.get("id") or f"call-{index + 1}")
            call_id = base
            suffix = 2
            while call_id in used:
                call_id = f"{base}-{suffix}"
                suffix += 1
            used.add(call_id)
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


#: ★ 单次 ``sources.snapshot`` 回给模型的字符上限。
#:
#: 实测（13f，A 臂，2026-10-06）：``INFOTABLE.tsv`` 只有 261 KB，但一次
#: snapshot 吐出 **298,587 字符 ≈ 8 万 token** —— 模型装不下也算不动，
#: 于是它退回交一段"关于这个产物的描述"当答案。同一次运行里它**从未**
#: 用过 ``sources.read`` / ``sources.grep``。
#:
#: ⇒ 结论：**分页必须是默认，不能是选项**。Hermes 的 ``read_file`` 默认只读
#: 一页，所以大文件天然被切；这里默认整份，于是最大的那份来源一次爆上下文。
#: 超限时不整份回 —— 留头部 + 一张回程票（``_snapshot_preview``）。
SNAPSHOT_MAX_CHARS = 20_000


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

    ★ 超限时**不整份回**：留头部 + 一张回程票（见 ``SNAPSHOT_MAX_CHARS``）。
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
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) > SNAPSHOT_MAX_CHARS:
        encoded = json.dumps(_snapshot_preview(ref, payload, rows, len(encoded)), ensure_ascii=False)
    return encoded


def _snapshot_preview(
    ref: str,
    payload: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    original_chars: int,
) -> dict[str, Any]:
    """★ 超限回执：留下头部，并**明确指路**到分页/检索两个工具。

    这一条是本项目的病根族在工具面上的一个实例：**分页必须是默认，不能是选项**。
    Hermes 的 ``read_file`` 默认只读一页，所以大文件天然被切开；这里的
    ``sources.snapshot`` 默认整份，于是最大的那份来源一次灌进 8 万 token。

    回执里三件事缺一不可：① 说清被截了（``truncated``/``total_rows``）；
    ② 给出**不要据此作答**的指令（否则模型会拿前 20 行编完整答案）；
    ③ 给出下一步该调哪个工具、带什么参数（回程票）。

    ★ 单文本行（非 csv/json 的来源，如 ``.tsv`` 落进"整份当一行"那一支）要**截文本**，
    不能整行丢掉 —— 丢掉的话模型连样本都看不到，只剩一句"太大了"。
    """
    kept: list[Mapping[str, Any]] = []
    budget = SNAPSHOT_MAX_CHARS // 2
    used = 0
    for row in rows:
        piece = len(json.dumps(row, ensure_ascii=False))
        if used + piece > budget:
            # 单个文本行（``{"text": <整份>}``）：截它的文本，别把行丢了。
            cells = {key: value for key, value in row.items() if isinstance(value, str)}
            if len(row) == 1 and len(cells) == 1 and not kept:
                (only_key, only_text), = cells.items()
                room = max(0, budget - used - len(only_key) - 16)
                kept.append({only_key: only_text[:room]})
            break
        kept.append(row)
        used += piece
    return {
        "ref": ref,
        "truncated": True,
        "columns": payload.get("columns", []),
        "rows": kept,
        "shown_rows": len(kept),
        "total_rows": len(rows),
        "original_chars": original_chars,
        "note": (
            f"★ 这份来源整份有 {original_chars} 字符，装不进一次调用，"
            f"这里只回了前 {len(kept)}/{len(rows)} 行。"
            "**不要拿这个残片作答** —— 它是样本，不是数据。"
            "取你要的那部分：`sources.read(ref, offset=起始行, limit=行数)` 按行取，"
            "或 `sources.grep(pattern, ref)` 按正则找你需要的片段。"
            "两者都带分页，整份读不是唯一选项。"
        ),
    }



def deliverables_payload(contract: TaskContract) -> list[Any]:
    """How ``task.contract`` announces the deliverables, one entry each.

    A deliverable that declares nothing beyond its field types is announced by
    name alone -- the historical payload, byte for byte. A deliverable whose
    fields carry a ``domain`` / ``granularity`` / ``source`` is announced with
    them, so the block can see where each value is supposed to come from. The
    "is this a declaration?" decision lives on ``DeliverableSpec`` (single
    source); this function only chooses the shape.
    """
    return [
        {"name": deliverable.name, "fields": dict(deliverable.schema_payload)}
        if deliverable.has_field_declarations
        else deliverable.name
        for deliverable in contract.deliverables
    ]


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
                "deliverables": deliverables_payload(value),
                "sources": [s.ref for s in value.sources],
                # ★ 一次实验：模型把上面的 `sources` 当成【资料本身】来读，
                #   读完名单就以为读完了书。于是它一次都不调 `sources.*`，
                #   直接交空表（13f 实测：五轮里 `sources.*` 调用 0 次，
                #   而它的推理原文写着 "understanding my subtask and the
                #   sources available"）。
                #   这一句把"名字 ≠ 内容、读是下一步动作"钉在**它产生误会的
                #   那一刻**（拿到名单时），而不是写进系统提示 —— 系统提示里
                #   的同类话实测三轮无效（§5.3.5①：改输入形态，不教模型遵守）。
                #   措辞给【工具名】而不给"你应该"：可执行的动作，不是态度。
                "how_to_read": (
                    "The `sources` above are names, not their contents. "
                    "`sources.snapshot` / `sources.read` / `sources.grep` take one of "
                    "those names and return the actual text. Nothing else hands you "
                    "data: read first, then produce."
                ),
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

    # -- ★ `#47`: the general read/write/execute primitives ------------------ #
    #
    # ``sources.read`` / ``sources.grep`` keep Hermes' *behaviour contract*
    # (line numbers, pagination, regex, context lines) and change only **where
    # the path may point**: an authorized source ref instead of a host path.
    # The permission check is the same one ``sources.snapshot`` uses, so the
    # three read tools cannot drift apart.

    def _locator(ref: str) -> str:
        """The carrier's locator for the payload's hint -- never a permission rule.

        Guarded with ``getattr`` so a partial stand-in toolset (a test double that
        implements only the read path) degrades to "no hint" instead of breaking
        every read with an ``AttributeError``.
        """
        locator = getattr(getattr(toolset, "sources", None), "locator", None)
        return str(locator(ref)) if callable(locator) else ""

    def _authorized_text(ref: str) -> tuple[str, str]:
        if toolset.sources is None:
            raise PermissionError(f"subtask {toolset.subtask!r} is not authorized to read sources")
        resolved = toolset.sources.resolve(ref)
        bound = toolset.sources.snapshot(resolved)
        body = body_reader(bound)
        return resolved, decode_body(body, form_for(forms or {}, bound.ref)).text

    def source_read(args: Mapping[str, Any]) -> str:
        resolved, text = _authorized_text(str(args.get("ref", "")))
        window = line_window(
            text,
            offset=args.get("offset", 0),
            limit=args.get("limit", DEFAULT_LIMIT),
        )
        return payload({"ref": resolved, "locator": _locator(resolved), **dict(window)})

    def source_grep(args: Mapping[str, Any]) -> str:
        pattern = args.get("pattern", "")
        if not isinstance(pattern, str):
            raise TextArgumentError(f"pattern must be a string, got {type(pattern).__name__}")
        target = str(args.get("target") or "content")
        if target not in TARGETS:
            raise TextArgumentError(f"target must be one of {list(TARGETS)!r}, got {target!r}")
        if target == "files":
            if toolset.sources is None:
                raise PermissionError(f"subtask {toolset.subtask!r} is not authorized to read sources")
            names = list(toolset.sources.refs())
            names.extend(name for name in map(_locator, names) if name and name not in names)
            return payload(dict(grep_names(names, pattern)))
        resolved, text = _authorized_text(str(args.get("ref", "")))
        found = search_lines(
            text,
            pattern,
            offset=args.get("offset", 0),
            limit=args.get("limit", DEFAULT_LIMIT),
            context=args.get("context", 0),
            output_mode=str(args.get("output_mode") or "content"),
        )
        return payload({"ref": resolved, "locator": _locator(resolved), **dict(found)})

    def _workspace() -> Any:
        workspace = getattr(toolset, "workspace", None)
        if workspace is None:
            raise PermissionError(f"subtask {toolset.subtask!r} has no workspace; file tools are not granted")
        return workspace

    def files_read(args: Mapping[str, Any]) -> str:
        """Read back a file this block may write (a numbered line window).

        ★ The permission predicate is the same ``may_write`` the write tools use
        (``BoundWorkspace.read``), so "may write ⟺ may read" cannot drift.
        ★ A successful ``files.read`` does **not** count as "read the sources":
        ``produce`` flips ``read_sources`` only on ``sources.*``.
        """
        return payload(
            dict(
                _workspace().read(
                    str(args.get("path", "")),  # blame: fail-closed — 缺 path 即空串 ⇒ 具名拒，不猜一个默认文件
                    offset=args.get("offset", 0),  # blame: fail-closed — 缺 offset 即从头读
                    limit=args.get("limit", DEFAULT_LIMIT),  # blame: fail-closed — 缺 limit 即一页，不整份灌上下文
                )
            )
        )

    def files_write(args: Mapping[str, Any]) -> str:
        return payload(
            dict(
                _workspace().write(
                    str(args.get("path", "")),
                    str(args.get("content", "")),
                    append=str(args.get("append", "")).strip().lower() in ("true", "1", "yes"),
                )
            )
        )

    def files_patch(args: Mapping[str, Any]) -> str:
        """定点修改：新形状是 `path + edits[]`（一次多条、整体原子）。

        ★ 旧形状（`path + old_string + new_string`，可选 `replace_all`）**仍然兼容**，
          但它已经不再广告给模型 —— 一个工具只留一种形状，模型才不用在两种写法里选。
        ★★ 这里**不许**把缺失的参数默认成空串：`args.get("new_string", "")` 会把
          "没给 new_string" 变成"把这段删掉"，还回一个 ok=True（只读审计探针实测：
          `{path, old_string:"world"}` 把 `'hello world\n'` 静默改成 `'hello \n'`）。
          壳侧不校验 required，所以这道检查必须在执行层自己做。
        """
        workspace = _workspace()
        path = str(args.get("path", ""))
        edits = args.get("edits")
        if edits not in (None, "", []):
            return payload(dict(workspace.patch_edits(path, edits)))
        if "old_string" not in args or "new_string" not in args:
            raise WorkspaceError(
                "files.patch needs `edits` = [{oldText, newText}, …]; "
                "the older `old_string`/`new_string` shape is still accepted but both keys must be given "
                "(a missing key would silently delete text)"
            )
        return payload(
            dict(
                workspace.patch(
                    path,
                    str(args.get("old_string", "")),
                    str(args.get("new_string", "")),
                    replace_all=str(args.get("replace_all", "")).strip().lower() in ("true", "1", "yes"),
                )
            )
        )

    def shell_run(args: Mapping[str, Any]) -> str:
        command = str(args.get("command", ""))
        # ★ 内联脚本当场拒（`command_guard.py` 的理由）：脚本不在磁盘上就永远
        #   "没得改"，`files.patch` 一次都不会被调到。拒的是**这一次调用**
        #   （`ToolRegistry.execute` 把它折成一条 ok=False 的 ToolResult），
        #   块不会被判死；拒收信息带 violated / expected / got 并指出下一步。
        problem = inline_script_problem(command)
        if problem:
            raise WorkspaceError(problem)
        return payload(
            dict(
                _workspace().run(
                    command,
                    background=str(args.get("background", "")).strip().lower() in ("true", "1", "yes"),
                    timeout=args.get("timeout") or None,
                    workdir=str(args.get("workdir", "")),
                )
            )
        )

    def _traces() -> Any:
        """★ 与 `_workspace()` 同一条规则：**不广告就不该被调用**（拿不到的能力不硬撑）。"""
        traces = getattr(toolset, "traces", None)
        if traces is None:
            raise PermissionError(
                f"subtask {toolset.subtask!r} has no traces; upstream process retrieval is not granted"
            )
        return traces

    def traces_search(args: Mapping[str, Any]) -> str:
        query = str(args.get("query", ""))  # blame: fail-closed — 缺 query 即空串 ⇒ `search` 具名拒，不会凭空受理
        return payload(dict(_traces().search(query)))

    def traces_read(args: Mapping[str, Any]) -> str:
        block_id = str(args.get("block_id", ""))  # blame: fail-closed — 缺 block_id 即空串 ⇒ 不在可见闭包 ⇒ 具名拒
        seq = str(args.get("seq", ""))  # blame: fail-closed — 缺 seq 即空串 ⇒ 具名拒，不静默取整块
        return payload(dict(_traces().read(block_id, seq)))

    #: name -> (handler, effect, target arguments).
    #: ★ The **effect** is declared here, once, by the tool itself -- a write is
    #: a barrier for call-level concurrency (``agentloop/tools.plan_tool_segments``),
    #: and a tool must not be able to lie about it (``services/registry.py`` header).
    catalog: Mapping[str, tuple[Handler, str, tuple[str, ...]]] = {
        "semantics.lookup": (lookup, "read", ()),
        "semantics.map": (map_field, "read", ()),
        "semantics.check": (check_usage, "read", ()),
        "task.mysubtask": (mysubtask, "read", ()),
        "task.upstream": (upstream, "read", ("name",)),
        "task.upstream_all": (upstream_all, "read", ()),
        "task.contract": (contract, "read", ()),
        "check.local": (check_local, "read", ()),
        "check.provenance": (check_provenance, "read", ()),
        "check.assemble_dryrun": (assemble_dryrun, "read", ()),
        "sources.snapshot": (snapshot, "read", ("ref",)),
        "sources.read": (source_read, "read", ("ref",)),
        "sources.grep": (source_grep, "read", ("ref",)),
        # ★ effect 必须是 `read`：读不是写屏障（`plan_tool_segments` 靠它分段）。
        "files.read": (files_read, "read", ("path",)),
        "files.write": (files_write, "write", ("path",)),
        "files.patch": (files_patch, "write", ("path",)),
        "shell.run": (shell_run, "write", ()),
        # ★ 只读：检索别人的过程不改任何东西，也不该被当成写屏障。
        "traces.search": (traces_search, "read", ()),
        "traces.read": (traces_read, "read", ()),
    }
    tools: list[RegisteredTool] = []
    for spec in toolset.servable_specs():
        entry = catalog.get(spec.name)
        if entry is None:
            raise KeyError(f"tool {spec.name!r} is advertised but has no implementation")
        handler, effect, targets = entry
        tools.append(
            RegisteredTool(
                name=spec.name,
                parameters=tuple(spec.params),
                handler=handler,
                returns=spec.returns,
                bound_to=spec.bound_to,
                effect=effect,
                target_args=tuple(targets),
            )
        )
    return ToolRegistry(tools=tuple(tools))
