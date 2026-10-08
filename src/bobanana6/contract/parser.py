"""Parse a task contract into ``TaskContract`` + ``ConstraintBundle``.

The bundle is *compiled per plan* (``ConstraintSource``): a contract is written
before anything is cut, so per-block constraints can only exist once the blocks
do. :func:`parse_contract` produces the declarative document; :func:`compile_bundle`
turns it into a bundle for a concrete plan.

Two fields are load-bearing and are refused here rather than later:

  * ``Constraint.rationale`` -- the spec makes it mandatory, and a parser that
    let it through would push the failure into ``core``, far from the file that
    caused it;
  * ``ConstraintBundle.unmapped`` reasons -- an empty reason is the same as not
    declaring at all, which makes "the ontology does not cover this field"
    disappear silently.
"""

from __future__ import annotations

import codecs
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..core.model import (
    DEFAULT_LIMITS,
    EOL_MODES,
    KNOWN_KINDS,
    Constraint,
    ConstraintBundle,
    DeliverableField,
    DeliverableSpec,
    FieldSource,
    Limits,
    SourceRef,
    SplitPlan,
    TaskContract,
)

__all__ = [
    "ContractDocument",
    "ContractParseError",
    "compile_bundle",
    "parse_contract",
    "parse_contract_file",
]

_TOP_LEVEL_KEYS = frozenset(
    {
        "task_id",
        "goal",
        "version",
        "deliverables",
        "sources",
        "notes",
        "constraints",
        "blind",
        "unmapped",
        "limits",
        # ★ 声明「哪份来源是题面」—— 题面全文要内联进每个块的 brief。
        "statement",
        # ★ ③: 声明"这些来源共享同一份状态"（多份局部视图 / 同一份账本）
        "sources_share_state",
    }
)
_CONSTRAINT_KEYS = frozenset({"id", "kind", "target", "spec", "rationale"})

#: ★ `deliverables[].schema` 里一个字段声明的**唯一形状定义**：键集直接从
#: `DeliverableField` / `FieldSource` 这两个 dataclass 推导（与 `_limits` 用
#: `Limits.__dataclass_fields__` 同一做法），字段增删不会在这里留下一份手写副本。
#: 裸类型字符串（`{text: str}`）不走这张表 —— 它是「只声明了 type」的简写。
_FIELD_KEYS = frozenset(DeliverableField.__dataclass_fields__) - {"name"}
_FIELD_SOURCE_KEYS = frozenset(FieldSource.__dataclass_fields__)


class ContractParseError(ValueError):
    """A contract failure that names the file, the JSON path and the rule."""


@dataclass(frozen=True)
class ContractDocument:
    """The declarative half: a task plus constraints that are not yet per-block."""

    contract: TaskContract
    version: int = 1
    #: ★ 任务声明的「哪份来源是**题面**」—— 一个已声明的 source ref；空 = 不声明。
    #: 题面全文会被内联进**每个块**的 brief（`produce.py::_statement_entry`），
    #: 而不是让八个块各自去取、各自理解。
    statement: str = ""
    hard: tuple[Constraint, ...] = ()
    soft: tuple[Constraint, ...] = ()
    blind: tuple[str, ...] = ()
    unmapped: tuple[tuple[str, str], ...] = ()
    limits: Limits = DEFAULT_LIMITS


def _fail(source: str, path: str, message: str, rule: str) -> None:
    raise ContractParseError(f"{source}:{path}: {message} (rule: {rule})")


def _load(text: str, source: str) -> Mapping[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{mark.line + 1}:{mark.column + 1}" if mark is not None else "<unknown>"
        problem = getattr(exc, "problem", None) or str(exc).strip()
        raise ContractParseError(f"{source}:{where}: YAML parse error: {problem}") from exc
    if not isinstance(data, Mapping):
        _fail(source, "$", "the contract must be a mapping", "type")
    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        _fail(source, "$", f"unknown top-level keys {unknown!r}", "additionalProperties")
    return data


def _fields(
    raw: Mapping[Any, Any],
    source: str,
    path: str,
    *,
    declared_sources: frozenset[str],
) -> dict[str, DeliverableField]:
    """`deliverables[].schema` -> ``{field name: declaration}``.

    Two legal spellings, one shape:

      * a bare type name -- ``{text: str}`` -- is the historical form and stays
        legal; it becomes a field that declares only its type. Every contract
        written before this existed keeps parsing (and keeps its payload).
      * a mapping -- ``{CUSIP: {type: str, domain: ..., granularity: ...,
        source: {ref: infotable, locator: CUSIP}}}`` -- declares the extra facts.

    A ``source.ref`` that is not one of the contract's declared sources is a
    contract error here, next to the file, not a silent dangling reference.
    """
    fields: dict[str, DeliverableField] = {}
    for raw_name, raw_value in raw.items():
        if not isinstance(raw_name, str) or not raw_name:
            _fail(source, f"{path}.<name>", "field names must be non-empty strings", "required")
        field_path = f"{path}.{raw_name}"
        if isinstance(raw_value, str):
            fields[raw_name] = DeliverableField(name=raw_name, type=raw_value)
            continue
        if not isinstance(raw_value, Mapping):
            _fail(source, field_path, "a field must be a type name or a declaration mapping", "type")
        unknown = sorted(str(key) for key in set(raw_value) - _FIELD_KEYS)
        if unknown:
            _fail(source, field_path, f"unknown field keys {unknown!r}", "additionalProperties")
        dtype = raw_value.get("type")
        if not isinstance(dtype, str) or not dtype:
            _fail(source, f"{field_path}.type", "type must be a non-empty string", "required")
        fields[raw_name] = DeliverableField(
            name=raw_name,
            type=dtype,
            domain=_optional_text(raw_value.get("domain"), source, f"{field_path}.domain"),
            granularity=_optional_text(raw_value.get("granularity"), source, f"{field_path}.granularity"),
            source=_field_source(raw_value.get("source"), source, field_path, declared_sources=declared_sources),
        )
    return fields


def _optional_text(raw: Any, source: str, path: str) -> str:
    """An optional declaration string; absent or ``None`` = not declared."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        _fail(source, path, "must be a string", "type")
    return raw


def _field_source(
    raw: Any,
    source: str,
    path: str,
    *,
    declared_sources: frozenset[str],
) -> FieldSource | None:
    """`source: {ref, locator}` -- the carrier one field's values come from."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        _fail(source, f"{path}.source", "source must be a mapping", "type")
    unknown = sorted(str(key) for key in set(raw) - _FIELD_SOURCE_KEYS)
    if unknown:
        _fail(source, f"{path}.source", f"unknown source keys {unknown!r}", "additionalProperties")
    ref = raw.get("ref")
    if not isinstance(ref, str) or not ref:
        _fail(source, f"{path}.source.ref", "ref must be a non-empty string", "required")
    if ref not in declared_sources:
        _fail(
            source,
            f"{path}.source.ref",
            f"{ref!r} is not a declared source",
            "source",
        )
    return FieldSource(ref=ref, locator=_optional_text(raw.get("locator"), source, f"{path}.source.locator"))


def _deliverables(
    data: Mapping[str, Any], source: str, *, declared_sources: frozenset[str]
) -> tuple[DeliverableSpec, ...]:
    raw_items = data.get("deliverables")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)) or not raw_items:
        _fail(source, "$.deliverables", "at least one deliverable is required", "required")
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping) or not isinstance(raw.get("name"), str) or not raw["name"]:
            _fail(source, f"$.deliverables[{index}].name", "deliverable name must be a non-empty string", "required")
    #: ★ 层次切分：先收齐名字，再校验依赖 —— 声明了一个不存在的产物是**契约错误**，
    #: 在文件旁边当场报（与 `#32` 的 encoding/eol 同一条纪律），不留到运行期才炸。
    names = {str(raw["name"]) for raw in raw_items}
    deliverables: list[DeliverableSpec] = []
    for index, raw in enumerate(raw_items):
        path = f"$.deliverables[{index}]"
        name = str(raw["name"])
        schema = raw.get("schema", {})  # blame: fail-open — ★该改：缺字段声明 ⇒ 空 schema ⇒ schema checker 无从收紧；这里只是把"没声明"如实记成空
        if not isinstance(schema, Mapping):
            _fail(source, f"{path}.schema", "deliverable schema must be a mapping", "type")
        fields = _fields(schema, source, f"{path}.schema", declared_sources=declared_sources)
        required = raw.get("required", True)  # blame: fail-closed — 缺声明即"必须有"，方向保守
        if not isinstance(required, bool):
            _fail(source, f"{path}.required", "required must be a boolean", "type")
        depends_on = _depends_on(raw.get("depends_on", ()), source, path, name=name, names=names)  # blame: fail-open — ★该改：缺声明 ⇒ 没有依赖链 ⇒ 分层/接口检查都无从谈起，切分器照样扇出
        try:
            deliverables.append(
                DeliverableSpec(name=name, schema=fields, required=required, depends_on=depends_on)
            )
        except ValueError as exc:
            _fail(source, path, str(exc), "deliverable")
    return tuple(deliverables)


def _depends_on(raw: Any, source: str, path: str, *, name: str, names: set[str]) -> tuple[str, ...]:
    """`deliverables[].depends_on` -> a de-duplicated tuple of declared upstream names.

    A missing key and an explicit `[]` are the same thing: no declared chain, so
    the cut falls back to whatever it did before layered splitting existed. A name
    that is not another deliverable of the same contract is refused here rather
    than silently dropped -- a dropped dependency is a missing input, and a
    missing input is what makes a worker invent one.
    """
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        _fail(source, f"{path}.depends_on", "depends_on must be a list of deliverable names", "type")
    ordered: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            _fail(source, f"{path}.depends_on", "depends_on entries must be non-empty strings", "type")
        if entry == name:
            _fail(source, f"{path}.depends_on", f"{entry!r} depends on itself", "selfReference")
        if entry not in names:
            _fail(source, f"{path}.depends_on", f"{entry!r} is not a declared deliverable", "additionalProperties")
        if entry not in ordered:
            ordered.append(entry)
    return tuple(ordered)


def _sources(data: Mapping[str, Any], source: str) -> tuple[SourceRef, ...]:
    raw_items = data.get("sources", ())  # blame: fail-open — ★该改：缺声明 ⇒ 没有来源可授权，读授权检查失去对象
    if raw_items is None:
        raw_items = ()
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        _fail(source, "$.sources", "sources must be a list", "type")
    sources: list[SourceRef] = []
    for index, raw in enumerate(raw_items):
        path = f"$.sources[{index}]"
        if not isinstance(raw, Mapping) or not isinstance(raw.get("ref"), str) or not raw["ref"]:
            _fail(source, f"{path}.ref", "source ref must be a non-empty string", "required")
        encoding = str(raw.get("encoding", "") or "")  # blame: fail-closed — 空串 = 未声明，且这个状态被显式写进投递载荷（不是静默按 utf-8 替换）
        eol = str(raw.get("eol", "") or "")  # blame: fail-closed — 同上：空串 = 未声明，不静默改行尾
        # ★ `#32`: 载体的形式由任务声明；**声明错了当场报**（别留到运行时才炸）。
        if encoding:
            try:
                codecs.lookup(encoding)
            except LookupError:
                _fail(source, f"{path}.encoding", f"unknown encoding {encoding!r}", "source")
        if eol not in EOL_MODES:
            _fail(source, f"{path}.eol", f"unknown eol {eol!r}; expected one of {EOL_MODES!r}", "source")
        try:
            sources.append(
                SourceRef(
                    ref=raw["ref"],
                    locator=str(raw.get("locator", "")),  # blame: fail-closed — 无定位 ⇒ 取网层具名失败，不会拿别的定位顶上
                    encoding=encoding,
                    eol=eol,
                    module=str(raw.get("module", "") or ""),  # blame: fail-closed — 不属于任何模块 ⇒ 不参与写区，方向保守
                )
            )
        except ValueError as exc:
            _fail(source, path, str(exc), "source")
    return tuple(sources)


def _constraints(raw_items: Any, *, source: str, group: str) -> tuple[Constraint, ...]:
    if raw_items is None:
        return ()
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        _fail(source, f"$.constraints.{group}", "constraints must be a list", "type")
    constraints: list[Constraint] = []
    for index, raw in enumerate(raw_items):
        path = f"$.constraints.{group}[{index}]"
        if not isinstance(raw, Mapping):
            _fail(source, path, "a constraint must be a mapping", "type")
        unknown = sorted(set(raw) - _CONSTRAINT_KEYS)
        if unknown:
            _fail(source, path, f"unknown keys {unknown!r}", "additionalProperties")
        for key in ("id", "kind", "target"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                _fail(source, f"{path}.{key}", f"{key} must be a non-empty string", "required")
        # ★ rationale is mandatory: refuse it here, not deep inside core.
        if not isinstance(raw.get("rationale"), str) or not raw["rationale"].strip():
            _fail(source, f"{path}.rationale", "rationale must not be empty", "non_empty")
        if raw["kind"] not in KNOWN_KINDS:
            _fail(source, f"{path}.kind", f"unknown constraint kind {raw['kind']!r}", "enum")
        spec = raw.get("spec", {})  # blame: fail-closed — 空 spec 会被各 checker 具名拒（它们要求 spec.x），方向保守
        if not isinstance(spec, Mapping):
            _fail(source, f"{path}.spec", "spec must be a mapping", "type")
        try:
            constraints.append(
                Constraint(id=raw["id"], kind=raw["kind"], target=raw["target"], spec=dict(spec), rationale=raw["rationale"])
            )
        except ValueError as exc:
            _fail(source, path, str(exc), "constraint")
    return tuple(constraints)


def _unmapped(data: Mapping[str, Any], source: str) -> tuple[tuple[str, str], ...]:
    raw_items = data.get("unmapped", ())  # blame: fail-closed — 缺声明 ⇒ 没有"允许未映射"的例外，方向保守
    if raw_items is None:
        raw_items = ()
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        _fail(source, "$.unmapped", "unmapped must be a list", "type")
    unmapped: list[tuple[str, str]] = []
    for index, raw in enumerate(raw_items):
        path = f"$.unmapped[{index}]"
        if not isinstance(raw, Mapping) or not isinstance(raw.get("field"), str) or not raw["field"]:
            _fail(source, f"{path}.field", "field must be a non-empty string", "required")
        # ★ an empty reason is not a declaration.
        if not isinstance(raw.get("reason"), str) or not raw["reason"].strip():
            _fail(source, f"{path}.reason", "an empty reason is the same as no declaration", "non_empty")
        unmapped.append((raw["field"], raw["reason"]))
    return tuple(unmapped)


def _blind(data: Mapping[str, Any], source: str) -> tuple[str, ...]:
    raw_items = data.get("blind", ())  # blame: fail-closed — 同上：没有预声明的盲区
    if raw_items is None:
        raw_items = ()
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        _fail(source, "$.blind", "blind must be a list of strings", "type")
    if any(not isinstance(item, str) or not item for item in raw_items):
        _fail(source, "$.blind", "blind entries must be non-empty strings", "items")
    return tuple(raw_items)


def _limits(data: Mapping[str, Any], source: str) -> Limits:
    raw = data.get("limits")
    if raw is None:
        return DEFAULT_LIMITS
    if not isinstance(raw, Mapping):
        _fail(source, "$.limits", "limits must be a mapping", "type")
    known = set(Limits.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        _fail(source, "$.limits", f"unknown limit keys {unknown!r}", "additionalProperties")
    try:
        return Limits(**dict(raw))
    except (TypeError, ValueError) as exc:
        _fail(source, "$.limits", str(exc), "limits")


def parse_contract(text: str, *, source: str = "<string>") -> ContractDocument:
    """Parse a contract document. Pure: no I/O beyond what the caller passes in."""
    data = _load(text, source)
    if not isinstance(data.get("task_id"), str) or not data["task_id"]:
        _fail(source, "$.task_id", "task_id must be a non-empty string", "required")
    if not isinstance(data.get("goal"), str) or not data["goal"]:
        _fail(source, "$.goal", "goal must be a non-empty string", "required")
    version = data.get("version", 1)  # blame: fail-closed — 缺版本按 1，不去猜更新的版本
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        _fail(source, "$.version", "version must be an integer >= 1", "minimum")
    constraints = data.get("constraints", {})  # blame: fail-open — ★该改：缺约束声明 ⇒ 只剩 compile_bundle 补的 AUTO presence，字段级规则一条都没有
    if constraints is None:
        constraints = {}
    if not isinstance(constraints, Mapping):
        _fail(source, "$.constraints", "constraints must be a mapping of hard/soft", "type")
    unknown = sorted(set(constraints) - {"hard", "soft"})
    if unknown:
        _fail(source, "$.constraints", f"unknown groups {unknown!r}", "additionalProperties")
    share_state = data.get("sources_share_state", False)  # blame: fail-open — ★★该改：#25 那条代判据的落点。缺声明即"不共享状态"⇒ 切分器照样按来源扇出；正确的告警是"这件事没被声明"
    if not isinstance(share_state, bool):
        _fail(source, "$.sources_share_state", "sources_share_state must be a boolean", "type")
    notes = data.get("notes", "")  # blame: fail-closed — 无备注
    if not isinstance(notes, str):
        _fail(source, "$.notes", "notes must be a string", "type")

    #: ★ 字段声明的 `source.ref` 要在解析 deliverables 时就能对照已声明来源，
    #: 所以 sources 先解析（契约错误仍在文件旁边当场报，只是顺序固定）。
    sources = _sources(data, source)
    # ★ 题面来源：值必须是**已声明**的 source ref；不填就是不声明（退回今天的行为）。
    #   指向不存在的 ref ⇒ 具名拒（说清"你指的哪份来源不存在"），绝不静默忽略。
    statement = data.get("statement", "") or ""  # blame: fail-closed — 未声明题面 ⇒ 不内联（退回旧行为），绝不伪造一份题面
    if not isinstance(statement, str):
        _fail(source, "$.statement", "statement must be a declared source ref", "type")
    if statement and statement not in {item.ref for item in sources}:
        _fail(
            source,
            "$.statement",
            f"{statement!r} is not a declared source; known refs: {sorted(item.ref for item in sources)!r}",
            "statement",
        )
    contract = TaskContract(
        task_id=data["task_id"],
        goal=data["goal"],
        deliverables=_deliverables(data, source, declared_sources=frozenset(item.ref for item in sources)),
        sources=sources,
        notes=notes,
        sources_share_state=share_state,
    )
    return ContractDocument(
        contract=contract,
        version=version,
        statement=statement,
        hard=_constraints(constraints.get("hard"), source=source, group="hard"),
        soft=_constraints(constraints.get("soft"), source=source, group="soft"),
        blind=_blind(data, source),
        unmapped=_unmapped(data, source),
        limits=_limits(data, source),
    )


def parse_contract_file(path: str | Path) -> ContractDocument:
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractParseError(f"{file}: cannot read the contract file: {exc}") from exc
    return parse_contract(text, source=str(file))


def compile_bundle(document: ContractDocument, plan: SplitPlan) -> ConstraintBundle:
    """Instantiate the bundle for one plan (implements what ``ConstraintSource`` promises).

    Declared constraints are carried verbatim. Any block left without a hard
    constraint targeting it gets a synthesized ``presence`` constraint, because
    ``core/split.py::verifiable`` refuses a block that cannot be checked -- and a
    block that cannot be checked cannot be retried locally.
    """
    covered: set[str] = set()
    for constraint in document.hard:
        covered.add(constraint.target)
    hard: list[Constraint] = list(document.hard)
    for subtask in plan.subtasks:
        if subtask.id in covered or subtask.output in covered:
            continue
        hard.append(
            Constraint(
                id=f"AUTO-{subtask.id}",
                kind="presence",
                target=subtask.id,
                spec={"min_rows": 1},
                rationale="every block needs at least one decidable constraint (split.verifiable)",
            )
        )
    return ConstraintBundle(
        task_id=plan.task_id,
        version=max(document.version, plan.version),
        hard=tuple(hard),
        soft=document.soft,
        blind=document.blind,
        unmapped=document.unmapped,
        limits=document.limits,
    )
