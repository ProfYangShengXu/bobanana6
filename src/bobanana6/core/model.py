"""Data structures for the judgment kernel.

Everything here is a frozen value object: no logic beyond construction-time
validation, no I/O, no environment access. All ordering that can leak into a
decision is explicit (tuples, or sorted iteration over mappings) so that the
same input always yields the same output regardless of process hash seed.

Field reader rule (spec §5.0)
-----------------------------
Every dataclass field carries exactly one reader marker as a trailing comment:

  ``core:<module>``    a decision in that core module reads the field
  ``external:<module>``a declared boundary consumer reads it; core decides nothing
  ``passthrough:<why>`` no decision reads it today; the reason is mandatory

Construction-time validation and the content hash are deliberately **not**
counted as readers: if they were, every field would trivially "have a reader"
and the rule would be vacuous. Fields whose only use is validation or hashing
are therefore marked ``passthrough``, and the ones with no reader anywhere are
listed in the delivery report rather than given a convincing-looking marker.

``tests/test_arch_guard.py::test_every_field_declares_a_reader`` enforces this.

Inference notes (the spec names these concepts but does not pin every field;
they are marked so a later doc sync can confirm them):
  * ``Limits`` / ``WorkerLimits`` / ``ResourceUse`` / ``Budget`` carry the
    capacity ceilings that the spec references but never defines.
  * ``DeliverableSpec`` / ``PlanCosts`` / ``Subtask.expects`` /
    ``Subtask.output_fields`` supply the data the split and assemble checks
    need but the spec's field lists omit.
  * ``Artifact`` is the in-memory view of a produced file. The kernel never
    reads paths: a loader outside ``core/`` fills this in, which is what keeps
    the kernel deterministic and environment free.
  * ``Claim`` is the (source, value, provenance) carrier that ``Conflict``
    refers to but never declares a producer for.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from json import dumps
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any

__all__ = [
    "KNOWN_KINDS",
    "MAX_ROUNDS",
    "MISSING",
    "MISSING_CELLS",
    "UNIT_SYMBOLS",
    "Artifact",
    "BlindSpot",
    "Budget",
    "BundleDelta",
    "Candidate",
    "Claim",
    "Classification",
    "Column",
    "Concept",
    "Conflict",
    "Constraint",
    "ConstraintBundle",
    "ConvergeDecision",
    "Convergence",
    "Definition",
    "DefinitionMismatch",
    "DeliverableField",
    "DeliverableSpec",
    "DisagreementTypes",
    "FieldSource",
    "InputSurvey",
    "Limits",
    "ModelResponse",
    "Ontology",
    "PlanCosts",
    "Provenance",
    "Recon",
    "Relation",
    "ResourceUse",
    "Snapshot",
    "SnapshotBody",
    "SourceRef",
    "SplitPlan",
    "Subtask",
    "TaskContract",
    "Unit",
    "Verdict",
    "canonical_row",
    "is_missing",
]

#: The constraint kinds this kernel can dispatch (the spec's set plus the kinds
#: this repo added; the count is intentionally not repeated here -- a guard
#: keeps this set equal to the checker registry).
KNOWN_KINDS: frozenset[str] = frozenset(
    {
        "schema",
        "presence",
        "range",
        "invariant",
        "relation",
        "provenance",
        "corroboration",
        "freshness",
        "coverage",
        "concept_ref",
        "unit_consistency",
        "definition_pin",
        "code_valid",
        #: ★ 2026-10-17: the artifact must **behave** -- run one declared command
        #: and require one marker in its stdout. Static kinds cannot tell
        #: "complete-looking" from "actually runs".
        "execution",
        "custom",
        #: ★ `#33`: 冻结的载体在装配期还是不是原文（代码类任务的"测试文件被改"就靠它）。
        "frozen_files",
    }
)

#: Iterative splitting is bounded; the default is a hard limit, not a hint.
MAX_ROUNDS = 3

#: The single source of truth for cost/output ruler symbols (spec §5.0.1 ④,
#: criterion ③: use a controlled table, never invent a symbol ad hoc). ``core``
#: never interprets what a symbol means -- it only refuses to compare numbers
#: declared under different rulers, and refuses a ruler outside this set.
#:
#: Only genuinely ambiguous quantities get a symbol. ``chars`` / ``seconds`` /
#: ``calls`` were removed: nothing would ever declare them because the field
#: name already states the unit (§5.0.1 ①). The model-output ruler and the
#: body-size ruler stay distinct because the *same* output can be counted either
#: way, and mixing them is the mistake recorded in ``AGENTS.md §7⑤``.
#:
#: NOTE: this set also drives the environment guard's vocabulary exemption, so
#: it is security-relevant -- adding a symbol widens what the guard lets pass.
UNIT_SYMBOLS: frozenset[str] = frozenset(
    {
        "tokens",  # model-output ruler
        "bytes",  # body-size ruler only; never mixed with the model-output ruler
    }
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_non_empty(value: str, message: str) -> None:
    _require(isinstance(value, str) and value != "", message)


def canonical_json(value: Any) -> str:
    """Render a value as order-stable JSON.

    Only used for content hashing and evidence strings. ``sort_keys=True`` plus
    the absence of float formatting surprises keeps the digest identical across
    runs and platforms.
    """
    return dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def content_digest(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Missing values
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Missing:
    """The one "this cell holds no value" marker (A04).

    A missing cell is not a zero and not an empty string: a zero is a value. The
    kernel cannot tell a filled-in zero from a real one *after the fact*, so the
    loader maps an absent cell here and the judgment reads absence directly
    instead of guessing from the number.
    """

    def __str__(self) -> str:
        return "MISSING"

    def __repr__(self) -> str:
        return "MISSING"


#: The missing-value marker. Compare with ``is`` (``is_missing``).
MISSING = _Missing()

#: Raw cell shapes the loader maps to :data:`MISSING`: no JSON value, or an
#: empty cell. ``0`` is deliberately **not** in this set -- a filled zero is a
#: value and must stay distinguishable from an absent one.
MISSING_CELLS: tuple[Any, ...] = (None, "")


def is_missing(value: Any) -> bool:
    """Whether this cell carries no value, *whatever shape it arrived in*.

    ★ ②（`三条调优点-介绍与修法`）：本文档的纪律是「一个东西的表示在**一个地方**
    定义」。这里就是那个地方 —— 哨兵 / ``None`` / 空白串都算「没有值」，
    每一个读者（判定器、比较、序列化）都问这一个函数，不再各自记住写法。
    ``0`` **不是**缺：填进去的 0 是一个值，必须与「没有值」分得开（A04）。
    """
    if value is MISSING or value is None:
        return True
    if not isinstance(value, str):
        return False
    # ★ 哨兵【被序列化之后】的写法也是「缺」：`json.dumps(..., default=str)` 会把它
    #   写成字符串 "MISSING"，而消费方（部件载荷 / 归档）看到的就是这个字符串。
    #   不认它的话，②的归一就漏掉一条真在流动的写法。
    return not value.strip() or value.strip().upper() == "MISSING"


def canonical_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The canonical spelling of "no value": **the key is absent**.

    ★ 这是解析入口用的归一：进来时把每一种「缺」的写法都折成同一种，
    后面（序列化、比较、判定）就只有一个形态要处理。
    """
    return {str(key): value for key, value in row.items() if not is_missing(value)}


# --------------------------------------------------------------------------- #
# Ontology
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Concept:
    id: str  # core:ontology —— 概念表键、映射、层级图都读它
    label: str  # core:ontology —— 别名消歧与 map_field 的标签匹配读它
    kind: str  # external:report (ONTOLOGY-MAP.md) —— 概念类型，报告渲染；core 只做构造期枚举校验
    domain: str  # external:report (ONTOLOGY-MAP.md) —— 领域标签，报告渲染
    parent: str | None = None  # core:ontology —— 层级/环检测读它
    aliases: tuple[str, ...] = ()  # core:ontology —— map_field 与别名冲突检测读它

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "Concept.id must not be empty")
        _require_non_empty(self.label, "Concept.label must not be empty")
        _require(self.kind in {"entity", "attribute", "event", "measure"}, f"unknown concept kind: {self.kind}")


@dataclass(frozen=True)
class Definition:
    concept_id: str  # core:ontology —— definition_orphan 与 pinned_version 读它
    version: str  # core:ontology —— definition_pin 与口径归一读它
    text: str  # external:report (ONTOLOGY-MAP.md) —— 人读口径文本
    formula: str | None = None  # external:report (ONTOLOGY-MAP.md) —— 可选形式化
    edge_cases: tuple[str, ...] = ()  # external:report (ONTOLOGY-MAP.md) —— 边界说明

    def __post_init__(self) -> None:
        _require_non_empty(self.concept_id, "Definition.concept_id must not be empty")
        _require_non_empty(self.version, "Definition.version must not be empty")


@dataclass(frozen=True)
class Unit:
    concept_id: str  # core:ontology —— resolve_unit 读它
    symbol: str  # core:ontology —— unit_consistency 与 convert_value 读它
    scale: float  # core:ontology —— convert_value 的缩放读它
    base: str  # core:ontology —— convert_value 的同基准校验读它

    def __post_init__(self) -> None:
        _require_non_empty(self.concept_id, "Unit.concept_id must not be empty")
        _require_non_empty(self.symbol, "Unit.symbol must not be empty")
        _require(
            isinstance(self.scale, (int, float)) and isfinite(float(self.scale)) and self.scale != 0.0,
            "Unit.scale must be a finite non-zero number",
        )


@dataclass(frozen=True)
class Relation:
    src: str  # core:ontology —— 端点存在性校验读它
    dst: str  # core:ontology —— 端点存在性校验读它
    kind: str  # external:report (ONTOLOGY-MAP.md) —— 关系类型；core 只做构造期枚举校验
    cardinality: str  # external:report (ONTOLOGY-MAP.md) —— 基数声明

    def __post_init__(self) -> None:
        _require_non_empty(self.src, "Relation.src must not be empty")
        _require_non_empty(self.dst, "Relation.dst must not be empty")
        _require(
            self.kind in {"belongs_to", "subtype_of", "measured_in", "derived_from"},
            f"unknown relation kind: {self.kind}",
        )


@dataclass(frozen=True)
class Ontology:
    domain: str  # external:report (ONTOLOGY-MAP.md) —— 领域标签；core 只做构造期非空校验
    version: str  # external:report (ONTOLOGY-MAP.md) —— 领域版本；口径版本走 Definition.version
    concepts: Mapping[str, Concept]  # core:ontology —— 校验/映射/层级读它
    definitions: Mapping[str, Definition]  # core:ontology —— 口径版本判定读它
    units: Mapping[str, Unit]  # core:ontology —— 单位一致性读它
    #: ★ A02 后半: 本体【显式声明】哪个口径版本是规范口径（concept -> version）。
    #: 缺省时退回"字典序最小"—— 但那条兜底必须在 evidence 里被报出来（不静默）。
    pins: Mapping[str, str] = field(default_factory=dict)  # core:ontology —— pinned_version 读它
    relations: tuple[Relation, ...] = ()  # core:ontology —— 端点校验读它
    code_systems: Mapping[str, tuple[str, ...]] = field(  # core:checkers —— code_valid 读它
        default_factory=dict
    )
    calendar: Mapping[str, Any] = field(  # external:decompose —— 时间窗切分用；core 不读、不校验
        default_factory=dict
    )

    def __post_init__(self) -> None:
        _require_non_empty(self.domain, "Ontology.domain must not be empty")
        _require_non_empty(self.version, "Ontology.version must not be empty")
        _require(self.version != "*", "Ontology.version must be a concrete version")
        for key, concept in self.concepts.items():
            _require(key == concept.id, f"concepts key {key!r} != Concept.id {concept.id!r}")

    # -- immutability ------------------------------------------------------ #

    @property
    def is_frozen(self) -> bool:
        """True once the container mappings can no longer be mutated."""
        return isinstance(self.concepts, MappingProxyType)

    def freeze(self) -> Ontology:
        """Return an immutable copy.

        ``frozen=True`` stops attribute rebinding but not mutation of the nested
        dicts, so the "shared semantics frozen before the first worker starts"
        requirement needs an explicit deep freeze. ``calendar`` is frozen like
        everything else but never inspected: it is carried through untouched for
        the boundary module that owns trading-day semantics.
        """
        return Ontology(
            domain=self.domain,
            version=self.version,
            concepts=_deep_freeze_mapping(self.concepts),
            definitions=_deep_freeze_mapping(self.definitions),
            pins=_deep_freeze_mapping(self.pins),
            units=_deep_freeze_mapping(self.units),
            relations=self.relations,
            code_systems=_deep_freeze_mapping(self.code_systems),
            calendar=_deep_freeze_mapping(self.calendar),
        )

    def content_hash(self) -> str:
        """Deterministic digest of the whole ontology -- the freeze witness."""
        return content_digest(
            {
                "domain": self.domain,
                "version": self.version,
                "concepts": {
                    key: {
                        "id": concept.id,
                        "label": concept.label,
                        "kind": concept.kind,
                        "domain": concept.domain,
                        "parent": concept.parent,
                        "aliases": list(concept.aliases),
                    }
                    for key, concept in self.concepts.items()
                },
                "definitions": {
                    key: {
                        "concept_id": definition.concept_id,
                        "version": definition.version,
                        "text": definition.text,
                        "formula": definition.formula,
                        "edge_cases": list(definition.edge_cases),
                    }
                    for key, definition in self.definitions.items()
                },
                "units": {
                    key: {
                        "concept_id": unit.concept_id,
                        "symbol": unit.symbol,
                        "scale": unit.scale,
                        "base": unit.base,
                    }
                    for key, unit in self.units.items()
                },
                "relations": [
                    {"src": r.src, "dst": r.dst, "kind": r.kind, "cardinality": r.cardinality}
                    for r in self.relations
                ],
                "code_systems": {k: list(v) for k, v in self.code_systems.items()},
                # Carried through so a change to trading-day data also breaks the
                # freeze witness; core never interprets the content.
                "calendar": dict(self.calendar),
            }
        )


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _deep_freeze_mapping(value: Mapping[Any, Any]) -> Mapping[Any, Any]:
    frozen = _deep_freeze(value)
    assert isinstance(frozen, MappingProxyType)
    return frozen


# --------------------------------------------------------------------------- #
# Contract / split
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FieldSource:
    """Where one declared output field comes from: a declared source + a locator.

    ``ref`` names one of the contract's ``sources``; ``locator`` says where inside
    that carrier the value lives (a column name, a JSON pointer, ...). Empty
    ``locator`` = the declaration names the carrier but not the spot -- the field
    is still *declared*, it just does not say which cell.
    """

    ref: str  # external:contract —— 字段值取自哪个已声明的来源
    locator: str = ""  # external:contract —— 该来源内的定位

    def __post_init__(self) -> None:
        _require_non_empty(self.ref, "FieldSource.ref must not be empty")


@dataclass(frozen=True)
class DeliverableField:
    """One declared output field: what value it holds, from where, at what grain.

    The bare spelling (``{text: str}``) is a legal declaration too -- it becomes a
    field whose only declared thing is its type (the Frictionless precedent that a
    missing type defaults to string, applied here to domain/granularity/source as
    well: missing means *not declared*, never a guessed default). Keeping the bare
    form parseable is what lets every existing contract keep working.
    """

    name: str  # external:contract —— schema 映射的键，也是列名
    type: str = ""  # external:contract —— 值的基本形态（str / int / float / bool）
    domain: str = ""  # external:contract —— 取值域：能给哪些值
    granularity: str = ""  # external:contract —— 粒度：一个值代表多大一块
    source: FieldSource | None = None  # external:contract —— 值的出处；None = 没声明

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "DeliverableField.name must not be empty")

    @property
    def declared(self) -> bool:
        """Whether this field says anything beyond its type.

        The one place that decides what "a field declaration" means: a bare type
        (``{text: str}``) is not a declaration, so a contract written before this
        existed keeps producing exactly the payload it produced before.
        """
        return bool(self.domain or self.granularity or self.source is not None)

    def as_payload(self) -> Any:
        """The declaration as a JSON-shaped value.

        A field that declares nothing beyond its type keeps the historical payload
        (the bare type string), so reports and ontology drafts are byte-identical
        for every contract written before this field existed. A field that declares
        more carries it -- nothing is dropped from the report.
        """
        if not self.declared:
            return self.type
        payload: dict[str, Any] = {"type": self.type}
        if self.domain:
            payload["domain"] = self.domain
        if self.granularity:
            payload["granularity"] = self.granularity
        if self.source is not None:
            payload["source"] = {"ref": self.source.ref, "locator": self.source.locator}
        return payload


@dataclass(frozen=True)
class DeliverableSpec:
    name: str  # core:split —— deliverable_names 供 no_orphan / cut_complete 读
    #: ★ 字段名 -> 字段声明。契约的 schema 从「透传一个字符串」升为「解析出
    #: `DeliverableField`」：类型之外还能声明 domain / granularity / source。
    #: 裸类型（`{text: str}`）解析成只声明了 type 的字段，向后兼容。
    schema: Mapping[str, DeliverableField] = field(  # external:contract —— 产物字段契约
        default_factory=dict
    )
    required: bool = True  # core:assemble —— cut_complete 只强制 required 的交付物必须产出  # blame: fail-closed — 省略即"这份产物必须有"，方向是保守的（多要不少要）
    #: ★ 层次切分：这份产物【依赖哪些别的产物】—— 由契约的提供方写明（`adapters/`
    #: 不猜、`decompose/` 不解析散文）。空元组 = 没有声明链，此时切分照旧。
    #: core:split —— 分层、同层检查与「接口数纪律」都读它。
    depends_on: tuple[str, ...] = ()  # core:split —— layered 计划据此接线
    #: ★★ 契约声明的**期望体量**（字符）：这份交付物本身至少要写出多少内容。
    #: ``0`` = 契约没声明 —— 壳不下发任何产出指令，行为与改动前逐字相同。
    #: ★ 读者只有一个：写块的产出指令（``decompose/strategies.py``）。它**不是判定** ——
    #:   产物短了不拒收，这里只加数据、不加规则。
    min_chars: int = 0  # external:decompose —— 写块的产出体量目标。# blame: fail-closed — 0 = 未声明 ⇒ 不下发任何产出指令，行为与改动前逐字相同

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "DeliverableSpec.name must not be empty")
        for upstream in self.depends_on:
            _require_non_empty(upstream, "DeliverableSpec.depends_on entries must not be empty")
        _require(
            type(self.min_chars) is int and self.min_chars >= 0,
            "DeliverableSpec.min_chars must be an integer >= 0",
        )

    @property
    def schema_payload(self) -> Mapping[str, Any]:
        """``schema`` as JSON-shaped values, for the report and the ontology draft."""
        return {name: spec.as_payload() for name, spec in self.schema.items()}

    @property
    def has_field_declarations(self) -> bool:
        """Whether any of this deliverable's fields declares more than its type.

        False for a bare spec (``{text: str}``, or no schema at all): such a
        deliverable is announced by name alone, exactly as before.
        """
        return any(spec.declared for spec in self.schema.values())


#: ★ `#32`: 载体行尾的**声明词表**（数据，不是判定）。`""` = 没声明；`preserve` = 原样。
#: 词表放稳定侧，`contract/`（声明解析）与 `sources/`（规整动作）都读它，避免各写一份。
EOL_MODES: tuple[str, ...] = ("", "preserve", "lf", "crlf")


@dataclass(frozen=True)
class SourceRef:
    ref: str  # external:sources —— SourcePort.fetch 的入参
    locator: str = ""  # external:sources —— 取回定位，core 不读
    #: ★ `#32`: 载体的**形式**，由任务声明；`core` 不读这两项（与 `locator` 同类）。
    #: 空串 = 没声明 —— 壳按 utf-8 猜，并把"这是假设"写进投递载荷，不静默替换。
    encoding: str = ""  # external:sources —— 声明的字符编码（如 gbk / utf-8-sig）
    eol: str = ""  # external:sources —— "" / preserve 原样 · lf · crlf
    #: ★ `#33`: 这份来源属于哪个**模块** —— 切分的切口就落在模块边界上（`split_by_module`）。
    #: 空串 = 不属于任何模块（例如冻结的测试文件：只做载体，不参与写区）。
    module: str = ""  # external:decompose —— 模块轴读它
    #: ★★ 这份来源是**已知位置**还是**一个还没定位的问题**。
    #:
    #:     "" / "file"     → 现状：`locator` 是确定的位置，`SourcePort.fetch` 取回来
    #:     "research"      → `locator` 留空，`question` 是**要找什么**，由 `ResearchPort` 先找再冻
    #:
    #: ★ 缺省是空串（= 现状），这是 **fail-closed** 的默认值：
    #:   既有契约一个字不改，行为逐字不变。
    kind: str = ""  # external:runner —— `Runner._research` 按它分流：research 走 ResearchPort，其余走 SourcePort。# blame: fail-closed — 缺省即"已知位置"，不会把普通来源误当研究问题去搜
    #: 研究问题（人话一句）—— 只有 `kind="research"` 时有意义。`locator` 留空。
    question: str = ""  # external:runner/研究器 —— 原样搬进 `ResearchQuestion.question`。# blame: fail-closed — 空 = 非研究来源，不会被拿去搜
    #: ★★ 种子地址：**起点，不是范围**。给了种子照样该自己往外扩。
    seeds: tuple[str, ...] = ()  # external:research —— 研究器把它作为搜索起点。# blame: whitelist — 空种子合法（问题可以只给方向，让实作自己找入口）

    def __post_init__(self) -> None:
        _require_non_empty(self.ref, "SourceRef.ref must not be empty")


@dataclass(frozen=True)
class TaskContract:
    """The task as data. Parsing a task file is a later unit's job."""

    task_id: str  # external:cli —— 装配与报告用；core 判定不读
    goal: str  # external:decompose —— 切分策略读它
    deliverables: tuple[DeliverableSpec, ...] = ()  # core:split —— deliverable_names 供切分/装配判定读
    sources: tuple[SourceRef, ...] = ()  # external:sources —— 取网实现读它
    notes: str = ""  # external:report (summary.json) —— 契约备注；报告模块未落地，当前无实现读者
    #: ★ ③: 这些来源【共享同一份状态】（多份局部视图 / 同一份账本）。
    #: 拆开就是 fake parallelism —— 但这不是壳替任务猜的：是任务【声明】的。
    #: 壳的判据是"声明了就别按来源扇出"，不是"数来源个数"。
    sources_share_state: bool = False  # core:split —— 切分策略据此回落单块，split 检查据此拦多块计划

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "TaskContract.task_id must not be empty")

    @property
    def deliverable_names(self) -> frozenset[str]:
        return frozenset(d.name for d in self.deliverables)

    @property
    def required_deliverable_names(self) -> frozenset[str]:
        """Only these must be produced; optional deliverables may be absent.

        Read by ``assemble.check_assembly`` when judging cut completeness, which
        is what makes ``DeliverableSpec.required`` load-bearing instead of a
        decorative marker.
        """
        return frozenset(d.name for d in self.deliverables if d.required)

    @property
    def declared_field_sources(self) -> Mapping[str, Mapping[str, FieldSource]]:
        """``{deliverable: {field: declared source}}`` for the fields that declare one.

        This is how a field declaration's ``source`` reaches the judgment context
        (``CheckContext.declared_field_sources``). Fields that declare no source
        are omitted rather than defaulted -- "not declared" is not a location.
        """
        return {
            deliverable.name: {
                name: spec.source for name, spec in deliverable.schema.items() if spec.source is not None
            }
            for deliverable in self.deliverables
        }


@dataclass(frozen=True)
class Subtask:
    id: str  # core:split —— 各检查按 id 定位与报错
    goal: str  # external:agentloop —— worker 只看得见自己这块的目标
    inputs: tuple[str, ...]  # core:split —— 星形/接口/环判定读它
    output: str  # core:split —— 接口完整性与产出定位读它
    contract_ref: str  # external:agentloop —— runtime 取契约用；core 不读
    axis: str  # core:split —— axis_uniform 读它
    mode: str  # core:split —— write_serial 读它
    partition: str | None = None  # core:split —— 同级 write 分区互斥判定读它
    # Inference: the schema the consumer expects for each named input, so
    # `iface_match` has something to compare against the producer declaration.
    expects: tuple[tuple[str, str], ...] = ()  # core:split —— iface_match 读它
    # Inference: which ontology-mapped fields this block produces, so
    # `concept_mapped` is checkable.
    output_fields: tuple[str, ...] = ()  # core:split —— concept_mapped 读它
    #: ★★ 本块要读的来源窗口：`(ref, 起始行, 结束行)`，行号从 0 起、右开。空 = 不按窗口读
    #: （回落的一份源一块：读哪份由 `goal` 说）。一个块可以携带多个窗口 —— 那正是
    #: 「多份小源合成一块」的形态。
    #: ★ 这是一条**声明**，不是一个注释：读者是 `services/tools.py::BoundTask.mysubtask`，
    #:   它把 `reads` 原样放进 brief 交到模型手上（模型据此调带 offset/limit 的读工具）。
    reads: tuple[tuple[str, int, int], ...] = ()  # external:services —— mysubtask 把窗口交进 brief。# blame: fail-closed — 缺省即"没有窗口声明"，不凭空指一个范围
    #: ★★ 读块的**产出指令**（壳写死的模板，不是契约字段）：把材料压成「可引用的要点 + 行号」、
    #: 一条一行、≤ N 字符。N 由写层预算倒推（`BlockBudget.read_output_cap`），
    #: 所以读块同时知道【读多少】与【写多少】—— 不知道产出上限的读块会把写层再撑爆一次。
    #: ★ 读者是 `assemble_impl/produce.py`（块提示的装配点）：它把这段放进 system 的块后缀
    #:   （**不进 brief** —— 压缩会先丢最老的 user 行，brief 正是那条）。
    #: 空 = 壳没给产出指令（非读块 / 没声明两级预算）⇒ 一个字都不加。
    read_instruction: str = ""  # external:assemble_impl —— 块提示装配点把它放进 system。# blame: fail-closed — 缺省即"没有产出指令"，不带任何隐含预算
    #: ★★ 写块的**产出指令** —— 与 `read_instruction` 对称的另一半，但**不复用它**：
    #: 那个字段的名字与语义都是读块的（把材料压成"一行一条、≤N 字符"的可引用要点），
    #: 混用会让下一个读代码的人以为写块也受读层预算管。
    #: 写块收到的是：产出的是交付物【本身】、声明的体量目标、直接写文件（`files.write` +
    #: `append` 续写）、脚本只用于要算的东西、每条说法带坐标、每段回读尾巴。
    #: 空 = 壳没给产出指令（契约未声明 `min_chars` / 不是写块）⇒ 一个字都不加。
    write_instruction: str = ""  # external:assemble_impl —— 块提示装配点把它放进 system（块后缀）。# blame: fail-closed — 缺省即"没有产出指令"

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "Subtask.id must not be empty")
        _require_non_empty(self.output, "Subtask.output must not be empty")
        _require(self.mode in {"read", "write"}, f"unknown subtask mode: {self.mode}")
        _require(
            self.axis in {"source", "entity", "window", "field", "module", "product"},
            f"unknown subtask axis: {self.axis}",
        )
        for ref, start, end in self.reads:
            _require_non_empty(ref, "Subtask.reads must name the source of a window")
            _require(
                type(start) is int and type(end) is int and 0 <= start < end,
                f"Subtask.reads window must satisfy 0 <= start < end, got {start!r}..{end!r}",
            )


@dataclass(frozen=True)
class PlanCosts:
    """Estimated merge cost and execution cost, in the same abstract unit."""

    merge: float  # core:split —— merge_cheap / merge_cost 读它
    execute: float  # core:split —— 与 merge 作比的执行代价，判定读它

    def __post_init__(self) -> None:
        _require(self.merge >= 0.0, "PlanCosts.merge must be >= 0")
        _require(self.execute >= 0.0, "PlanCosts.execute must be >= 0")


@dataclass(frozen=True)
class SplitPlan:
    task_id: str  # external:cli —— 装配与报告用；core 判定不读
    version: int  # external:decompose —— 方案版本号，供迭代与续跑对齐
    round: int  # core:split —— round_budget 读它
    subtasks: tuple[Subtask, ...]  # core:split —— 全部合法性检查遍历它
    shape: str  # core:split —— star 检查读它
    interfaces: Mapping[str, str] = field(  # core:split —— iface_complete / iface_match 读它
        default_factory=dict
    )
    recon_evidence: str = ""  # external:report (summary.json) —— 「为什么这么切」；报告模块未落地，当前无实现读者
    costs: PlanCosts | None = None  # core:split —— merge_cheap 读它
    #: ★ ③: 为什么这份计划【不扇出】（空串 = 没拒绝）。判据来自契约的声明，
    #: 不是"来源数不够"（那是代理判据，正是本块要换掉的东西）。
    refusal: str = ""  # core:split —— decision 读它（refuse_split 的判据落点）

    @property
    def decision(self) -> str:
        """`refuse_split` / `split` / `single_agent` —— 规格 §14.4 的那三个名字。"""
        if self.refusal:
            return "refuse_split"
        return "split" if len(self.subtasks) > 1 else "single_agent"

    @property
    def fallback(self) -> str:
        """Where a refused plan lands: the single-agent shape."""
        return "single_agent" if len(self.subtasks) <= 1 else "none"

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "SplitPlan.task_id must not be empty")
        _require(self.version >= 1, "SplitPlan.version must be >= 1")
        _require(self.round >= 0, "SplitPlan.round must be >= 0")
        _require(self.shape in {"star", "pipeline", "grid", "layered"}, f"unknown plan shape: {self.shape}")

    @property
    def subtask_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.subtasks)


@dataclass(frozen=True)
class InputSurvey:
    source_refs: tuple[str, ...] = ()  # external:adapters —— 本体起草读它
    byte_size: int = 0  # external:adapters —— 规模估计，起草读它
    shape: str = ""  # external:adapters —— 输入形态，起草读它
    notes: str = ""  # external:adapters —— 起草提示，core 不读


@dataclass(frozen=True)
class Recon:
    evidence: str = ""  # external:decompose —— 切分策略读侦察结论
    source_ids: tuple[str, ...] = ()  # external:decompose —— 侦察覆盖的源，切分读它
    findings: tuple[str, ...] = ()  # external:decompose —— 切分输入；decompose 模块未落地，当前无实现读者
    #: ★★ 每一份已冻结来源的**逐行字符数**（不含换行）。这是"按大小开并行"唯一的尺寸事实：
    #: 切分器据此把超预算的源切成按行对齐的窗口、把远小于预算的源合成一块读块。
    #: ★ 为什么放在 recon 上：`DecomposerPort.split(contract, ontology, recon)` 是计划期
    #:   唯一携带运行期数据的通道 —— 装配点构造 `Decomposer` 时还不知道取回了哪几份、多大。
    #: ★ 空 = **没有尺寸事实** ⇒ 切分回落「一份源一块」（不凭空造一个尺寸）。
    #: 名字里没有"content"：这是度量（每行多长），不是材料本身，recon 不携带正文。
    source_lines: Mapping[str, tuple[int, ...]] = field(  # blame: fail-closed — 缺省即"没有尺寸事实"⇒不拆不合，回落到改动前的形状
        default_factory=dict  # external:decompose —— 按大小切窗口 / 合成读块时读它
    )

    # 说明：Recon 整体只作为 DecomposerPort.split 的入参出现，core 不解释它。


# --------------------------------------------------------------------------- #
# Constraints
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Constraint:
    id: str  # core:kernel —— 判定顺序、失败清单与证据键读它
    kind: str  # core:checkers —— 分派到哪个 checker 读它
    target: str  # core:checkers —— 定位被判定对象读它
    spec: Mapping[str, Any]  # core:checkers —— 各 kind 的参数都在这里，checker 读它
    rationale: str  # external:report (CONFLICTS.md) —— 回答「这条约束为什么存在」；报告模块未落地，当前无实现读者

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "Constraint.id must not be empty")
        _require_non_empty(self.kind, "Constraint.kind must not be empty")
        _require_non_empty(self.rationale, "Constraint.rationale must not be empty")
        _require(isinstance(self.spec, Mapping), "Constraint.spec must be a mapping")


@dataclass(frozen=True)
class Limits:
    """Capacity ceilings and thresholds, each in the unit its name states.

    There is deliberately no ``cost_unit``: ``requires_calls`` is counted in
    calls, so the field name *is* the unit (spec §5.0.1 ①). The one genuinely
    ambiguous ruler -- ``max_output`` -- lives on the value it measures, i.e.
    ``Budget.max_output_unit``, not on a global limits object.
    """

    max_rounds: int = MAX_ROUNDS  # core:split —— round_budget 读它
    merge_cost_ratio: float = 0.5  # core:split —— merge_cheap 的阈值，判定读它
    tol: float = 1e-9  # core:checkers —— range/relation 的容差默认值
    provenance_min_coverage: float = 0.95  # core:checkers —— provenance 覆盖下限
    corroboration_min_sources: int = 2  # core:checkers —— corroboration 来源数下限

    def __post_init__(self) -> None:
        _require(self.max_rounds >= 0, "Limits.max_rounds must be >= 0")
        _require(self.merge_cost_ratio > 0.0, "Limits.merge_cost_ratio must be > 0")
        _require(self.tol >= 0.0, "Limits.tol must be >= 0")
        _require(0.0 <= self.provenance_min_coverage <= 1.0, "provenance_min_coverage must be in [0, 1]")
        _require(self.corroboration_min_sources >= 1, "corroboration_min_sources must be >= 1")


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class ConstraintBundle:
    task_id: str  # external:cli —— 装配与报告用；core 判定不读
    version: int  # external:decompose —— 约束包版本号，供迭代对齐
    hard: tuple[Constraint, ...] = ()  # core:kernel —— admit 的判定集
    soft: tuple[Constraint, ...] = ()  # core:kernel —— 只计分不判定的约束
    blind: tuple[str, ...] = ()  # core:consist —— stale_blind 读它
    unmapped: tuple[tuple[str, str], ...] = ()  # core:split —— concept_mapped 读它
    limits: Limits = DEFAULT_LIMITS  # core:checkers —— 阈值都由它提供

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "ConstraintBundle.task_id must not be empty")
        _require(self.version >= 1, "ConstraintBundle.version must be >= 1")
        ids = [c.id for c in self.hard] + [c.id for c in self.soft]
        _require(len(ids) == len(set(ids)), "constraint ids must be unique within a bundle")

    @property
    def unmapped_fields(self) -> frozenset[str]:
        return frozenset(name for name, _reason in self.unmapped)


# --------------------------------------------------------------------------- #
# Divergence -- three distinct types, deliberately not merged
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlindSpot:
    """Divergence caused by constraint silence -> add a constraint."""

    path: str  # core:kernel —— 去重与决策读它
    left: Any  # core:kernel —— 去重读它
    right: Any  # core:kernel —— 去重读它


@dataclass(frozen=True)
class Conflict:
    """Same definition, different sources, different values -> record both."""

    concept_id: str  # core:kernel —— 去重与分类决策读它
    definition_version: str  # external:arbiter —— 证据束载荷，core 不读
    claims: tuple[tuple[str, Any, Provenance], ...]  # external:arbiter —— 双方证据载荷，core 不读


@dataclass(frozen=True)
class DefinitionMismatch:
    """Different definition versions -> a defect, normalize before comparing."""

    concept_id: str  # core:kernel —— 去重与归一路径决策读它
    versions: tuple[str, ...]  # external:arbiter —— 版本载荷，core 不读
    parties: tuple[str, ...]  # external:arbiter —— 当事方载荷，core 不读


Disagreement = BlindSpot | Conflict | DefinitionMismatch

#: The three divergence classes, kept as a tuple so a guard can assert that
#: they remain three distinct, non-overlapping types.
DisagreementTypes: tuple[type, ...] = (BlindSpot, Conflict, DefinitionMismatch)


# --------------------------------------------------------------------------- #
# Provenance / artifacts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Snapshot:
    ref: str  # core:checkers —— freshness/provenance 按 ref 查表
    body_path: Path  # external:sources —— 固化载体的位置，core 不打开
    captured_at: str  # core:checkers —— freshness 的时间戳比较读它
    source_kind: str = ""  # external:report (SOURCES.md) —— 溯源按来源分层：文件/网络是载体类，研究来源落 T1–T4 信任层
    #: ★ 这条来源是**走哪条路**取回来的（`web-use` 纪律①：降级必须可见）。
    #: 例：`jina` / `direct` / `github` / `rss`。空的 = 没走取网那条路（本地文件）。
    #: 读者：`report/builders.py` 的 snapshots 段（报告要能看出"这条是备选后端给的"）。
    backend: str = ""  # external:report —— SOURCES.md 列出"这条走的是哪条路"。# blame: fail-closed — 空 = 没走取网那条路（本地文件），不假装成网上的
    #: ★★ 实际用的 ≠ 想要的 ⇒ True。降级来的内容**完整度可能打折**，
    #: 下游要能知道这件事（否则它会当成一手材料用）。
    #: 读者：① `report/builders.py` 的 snapshots 段；② 块的 brief（`produce.py`）。
    degraded: bool = False  # external:report/agentloop —— 报告标出来 + brief 告诉块"这条打折了"。# blame: fail-closed — 缺省 False = 当作没降级，只有真降级时才标 True（不虚报）

    def __post_init__(self) -> None:
        _require_non_empty(self.ref, "Snapshot.ref must not be empty")


@dataclass(frozen=True)
class ResearchQuestion:
    """一个**还没被定位到具体位置**的研究问题。

    ★ 与 `SourceRef` 的区别是这条分工的根：
      `SourceRef` 的 `locator` 是**已经确定的**位置 —— 取回来就行；
      这里只知道"要找什么"，位置由研究者（`ResearchPort` 的实作）决定。
      所以两者不能共用 `locator` 字段（那会把"位置"和"问题"挤进同一个名字）。

    ★★ `seeds` 是**起点，不是范围**：给了种子照样该自己往外扩 ——
       从一个页面里挖出更多相关来源，才是"研究"而不是"下载"。
    """

    ref: str  # external:contract —— 产出快照的名字前缀
    question: str  # external:research —— 要找什么，人话一句
    seeds: tuple[str, ...] = ()  # external:research —— 搜索起点。# blame: whitelist — 空种子合法（纯搜索型问题，渠道自己找入口）
    channels: tuple[str, ...] = ()  # external:research —— 限定走哪些后端。# blame: whitelist — 空 = 不限定，由实作按【当前可用】的渠道定

    def __post_init__(self) -> None:
        _require_non_empty(self.ref, "ResearchQuestion.ref must not be empty")
        _require_non_empty(self.question, "ResearchQuestion.question must not be empty")


@dataclass(frozen=True)
class ResearchBudget:
    """一次检索花多少 —— 每一项缺省都是**保守**的那一侧。

    ★ 与 `Budget`（整跑的钱）分开：这是**一次取数**的闸，
      检索本身也可能失控（把整站扒下来），所以它自己要有上限。
    """

    max_sources: int = 8  # external:research —— 抓几份。# blame: fail-closed — 少抓比多抓好；要更多得显式声明
    max_depth: int = 1  # external:research —— 从种子往外扩几层。# blame: fail-closed — 只扩一层，不默认爬
    max_bytes: int = 4_000_000  # external:research —— 落盘上限。# blame: fail-closed — 单次检索约 4MB 封顶

    def __post_init__(self) -> None:
        if self.max_sources <= 0:
            raise ValueError("ResearchBudget.max_sources must be positive")
        if self.max_depth < 0:
            raise ValueError("ResearchBudget.max_depth must not be negative")


@dataclass(frozen=True)
class Provenance:
    value_ref: str  # core:checkers —— 逐点覆盖统计读它
    snapshot_ref: str  # core:checkers —— 可达性判定读它
    locator: str = ""  # external:report —— 人读定位串，core 不判定
    #: Which row of the cited snapshot this point came from (A11). ``None`` means
    #: the producer did not say: the check then falls back to the row named in
    #: ``value_ref``. A merge block names a *different* source row than its own
    #: output row, which is exactly why this has to be sayable.
    source_row: int | None = None  # core:checkers —— 内容级溯源校验读它

    def __post_init__(self) -> None:
        _require_non_empty(self.value_ref, "Provenance.value_ref must not be empty")
        _require_non_empty(self.snapshot_ref, "Provenance.snapshot_ref must not be empty")


@dataclass(frozen=True)
class Column:
    name: str  # core:checkers —— schema/range/relation 按名取列
    dtype: str  # core:checkers —— schema 类型比对读它
    nullable: bool = True  # blame: fail-open — ★该改成 False，但受"不动 core/"约束（§五①）：省略即假定列可空 ⇒ 缺格检查更松。已具名登记待裁决  # core:checkers —— schema 可空性比对读它
    #: ★ A10: the unit the *producer* established for this column, in words
    #: (``"CNY"`` / ``"千元"``). Empty means **not established** -- neither the
    #: values nor the answer declared one, so the column's unit would rest on the
    #: ontology's concept declaration. That is a real state, not a default: a task
    #: may require an explicit unit (``unit_consistency.spec.unit_source``).
    unit: str = ""  # core:checkers —— unit_consistency 读它

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "Column.name must not be empty")
        _require_non_empty(self.dtype, "Column.dtype must not be empty")


@dataclass(frozen=True)
class Artifact:
    """An in-memory view of one produced deliverable.

    Loaders live outside ``core/``; the kernel only reasons over this value, so
    no path is ever opened here.
    """
    name: str  # core:checkers —— 产出定位与坐标报错读它
    columns: tuple[Column, ...] = ()  # core:checkers —— schema/range/relation 读它
    rows: tuple[Mapping[str, Any], ...] = ()  # core:checkers —— 逐值判定遍历它
    text: str | None = None  # core:checkers —— provenance 的文本行点位读它

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "Artifact.name must not be empty")

    def column(self, name: str) -> Column | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class SnapshotBody:
    """The frozen content of one snapshot, as data (A11).

    core never opens ``Snapshot.body_path``; the reader outside parses the carrier
    and hands the rows over in this shape (``AGENTS.md §1.3``). Having the body is
    what lets a citation be checked for *content* instead of only for its name.
    """

    ref: str  # core:provenance —— 内容级校验按 ref 找体
    columns: tuple[Column, ...] = ()  # core:provenance —— 值比对按列名取交集
    rows: tuple[Mapping[str, Any], ...] = ()  # core:provenance —— 引用行是否存在、值是否一致读它

    def __post_init__(self) -> None:
        _require_non_empty(self.ref, "SnapshotBody.ref must not be empty")

    def row(self, index: int) -> Mapping[str, Any] | None:
        if 0 <= index < len(self.rows):
            return self.rows[index]
        return None


@dataclass(frozen=True)
class Claim:
    """One source's asserted value for a field, with its provenance."""

    field: str  # core:checkers —— corroboration 与 classify 按字段归并读它
    value: Any  # core:checkers —— 值一致性判定读它
    source_id: str  # core:checkers —— 独立来源计数读它
    provenance: Provenance  # core:checkers —— 随证据一起产出 Conflict 读它

    def __post_init__(self) -> None:
        _require_non_empty(self.field, "Claim.field must not be empty")
        _require_non_empty(self.source_id, "Claim.source_id must not be empty")


# --------------------------------------------------------------------------- #
# Runtime-facing value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResourceUse:
    calls: int = 0  # external:metrics —— 用量记录读它；core 判定不读
    wall_s: float = 0.0  # external:metrics —— 用量记录读它；core 判定不读
    output: int = 0  # external:metrics —— 用量记录读它；core 判定不读


@dataclass(frozen=True)
class ModelResponse:
    """One model call's result, as data. Returned by the ``LLMClient`` port."""

    text: str = ""  # external:agentloop —— worker 循环读它
    tool_calls: tuple[Mapping[str, Any], ...] = ()  # external:agentloop —— 工具派发读它
    usage: Mapping[str, int] = field(  # external:metrics —— 计量读它；core 不读
        default_factory=dict
    )
    #: ★ 端点**要求原样带回**的 assistant 字段（思考模式的 `reasoning_content` 就是这一族）。
    #: 壳不解释它的内容：适配器负责认出"哪些字段必须带回去"，循环负责把它们存进它建的那条
    #: assistant 行、出站再原样送回。没给就是空映射 —— **不许凭空造一个空值塞进去**。
    carry_back: Mapping[str, Any] = field(  # external:agentloop —— 循环把它放进 assistant 行
        default_factory=dict
    )


@dataclass(frozen=True)
class EscalationRequest:
    """一处求助的**内容** —— 这一块自己做不成，把"做不到 + 原因 + 证据"交出去。

    ★ 端口只描述**交出去什么**；怎么交、等多久、对面是谁，是实作那一侧的事
      （`core/` 不认识它们，也不该认识）。
    ★ 为什么要有这个出口：实测（qwen4b14）三个块连挂三跑，失败读数**逐字相同** ——
      重试与反思都试过之后，壳手里没有任何新东西可换，只能把它判死。
      而"换个脑子看一遍"正是外部能提供的、壳自己产不出来的东西。
    """
    task_id: str  # external:report —— 报告按它归档
    block: str  # external:report —— 求助的是哪一块（块自己能报的就是这个）
    reason: str  # external:report —— 分类出来的失败身份
    attempts: int  # external:report —— 一共失败了几次
    # blame: whitelist — 省略即"没有细节"；求助照样发得出去（细节只是附带的证据）
    detail: str = ""  # external:report —— 分类器给的细节原文


@dataclass(frozen=True)
class EscalationOutcome:
    """拿回来的答复。★ 拿不到是**正常返回**（`answered=False`），不是异常 ——
    交不出去不该把这一块判死（它本来就快做不成了）。"""
    answered: bool  # external:agentloop —— 只有收到了才把答复注入上下文
    # blame: fail-closed — 省略即"没有答复"⇒ 不注入上下文、落回降级（不会凭空当成功）
    advice: str = ""  # external:agentloop —— 答复正文（注入给模型的那一段）
    # blame: whitelist — 省略即"没说明白"，只影响留痕详尽度，不影响判定
    detail: str = ""  # external:report —— 走的是什么路径 / 为什么没拿到


@dataclass(frozen=True)
class Budget:
    max_calls: int  # core:consist —— unreachable 与 requires_calls 比
    max_wall_s: int  # core:consist —— 与 requires_wall_s 比；字段名的 _s 就是单位
    max_output: int  # core:consist —— unreachable 与 requires_output 比
    max_output_unit: str = "tokens"  # core:consist —— ruler_vs_limits 读它；必须 ∈ UNIT_SYMBOLS

    def __post_init__(self) -> None:
        _require(self.max_calls >= 0, "Budget.max_calls must be >= 0")
        _require(self.max_wall_s >= 0, "Budget.max_wall_s must be >= 0")
        _require(self.max_output >= 0, "Budget.max_output must be >= 0")
        _require(
            self.max_output_unit in UNIT_SYMBOLS,
            f"unknown unit symbol {self.max_output_unit!r}; allowed: {sorted(UNIT_SYMBOLS)!r}",
        )


@dataclass(frozen=True)
class Candidate:
    subtask_id: str  # core:kernel —— classify 定位覆盖约束读它
    source_id: str  # core:kernel —— 冲突判定区分来源读它
    hint: str = ""  # core:kernel —— 内容指纹读它
    files: Mapping[str, Path] = field(  # core:kernel —— 内容指纹读它；由边界外填入
        default_factory=dict
    )
    provenance: tuple[Provenance, ...] = ()  # core:checkers —— 溯源判定读它
    cost: ResourceUse = ResourceUse()  # external:metrics —— 用量记录读它；core 判定不读
    # In-memory content of `files`, injected by the loader outside core/.
    artifacts: tuple[Artifact, ...] = ()  # core:checkers —— 全部内容判定都读它
    claims: tuple[Claim, ...] = ()  # core:checkers —— corroboration/classify 读它
    # Inference: which definition version this candidate used per concept, so
    # `classify` can tell a definition mismatch from a real conflict.
    definition_versions: Mapping[str, str] = field(  # core:kernel —— 先查口径读它
        default_factory=dict
    )
    #: ★★ 上游写给**接手的人**的交接说明（`#66`）。它**不进判定**（`core/checkers/`
    #: 一个都不读它），但它要**传下去** —— 下游块靠它复现上游的口径与决定，
    #: 而不是从数据里反推。契约里那一格原来只放行了名字、没人接。
    #: 为什么不让判定器读：一旦有判据读它，就会回到「判据只查形状 ⇒ 学最小可过形态」。
    summary: str = ""  # external:runner —— 交接件（`handoffs`）取它给下游
    #: ★★ 曾经在这里加过一个 `done` 字段（"模型自己声明我干完了"），让"交答案"与
    #: "终结"分开。**实测撤掉了**（13f `ansrow` vs `bigfile`）：写不写 `done` 的两次
    #: 交的都是真内容，它一次没抓到过占位符，却把一次好答案判成 `restart_cap`。
    #: 真要治"填了形状就交卷"，该加的是**内容层判据**，不是完成标记。
    def __post_init__(self) -> None:
        _require_non_empty(self.subtask_id, "Candidate.subtask_id must not be empty")
        _require_non_empty(self.source_id, "Candidate.source_id must not be empty")

    def artifact(self, name: str) -> Artifact | None:
        for artifact in self.artifacts:
            if artifact.name == name:
                return artifact
        return None

    @property
    def artifact_names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.artifacts)


@dataclass(frozen=True)
class Verdict:
    ok: bool  # core:kernel —— converge 的通过分支读它
    failed: tuple[str, ...] = ()  # external:agentloop —— 局部重试按失败约束定位
    evidence: Mapping[str, str] = field(  # external:report —— 判定证据展示，core 不再读
        default_factory=dict
    )
    #: ★ `#44`: 失败的检查器们**一致声明**的归因（空串 = 没声明 / 声明冲突）。
    #: 由 `kernel.admit` 汇总：多个人说不同的话时不挑一个代表（那是猜）。
    cause: str = ""  # external:agentloop —— runner 写进 gate_check 事件，归因层读声明
    soft_scores: Mapping[str, float] = field(  # external:report —— 软分展示，core 不再读
        default_factory=dict
    )


@dataclass(frozen=True)
class Classification:
    blind_spots: tuple[BlindSpot, ...] = ()  # core:kernel —— converge 决策读它
    conflicts: tuple[Conflict, ...] = ()  # core:kernel —— converge 决策读它
    definition_mismatches: tuple[DefinitionMismatch, ...] = ()  # core:kernel —— converge 决策读它

    @property
    def is_empty(self) -> bool:
        return not (self.blind_spots or self.conflicts or self.definition_mismatches)


class ConvergeDecision(StrEnum):
    DELIVER = "deliver"
    ARBITRATE = "arbitrate"
    NORMALIZE_DEFINITION = "normalize_definition"
    RECORD_CONFLICT = "record_conflict"
    REFINE_SPLIT = "refine_split"


@dataclass(frozen=True)
class Convergence:
    decision: ConvergeDecision  # external:cli —— 调用方按它分派；core 只产出不分支
    verdicts: tuple[Verdict, ...] = ()  # external:cli —— 调用方展示/重试用它
    classification: Classification = Classification()  # external:arbiter —— 分歧载荷交给裁决方
    note: str = ""  # external:report —— 人读说明，core 不读


@dataclass(frozen=True)
class BundleDelta:
    added_hard: tuple[Constraint, ...] = ()  # external:decompose —— ArbiterPort 返回契约
    added_blind: tuple[str, ...] = ()  # external:decompose —— 同上
    resolved: tuple[str, ...] = ()  # external:decompose —— 同上
    note: str = ""  # external:report —— 人读说明，core 不读


@dataclass(frozen=True)
class AssemblyOutcome:
    passed: bool  # external:decompose —— refine 依据它决定是否重切
    failed_blocks: tuple[str, ...] = ()  # external:decompose —— 局部重切定位用它
    coverage: Mapping[str, str] = field(  # external:report —— 覆盖声明展示，core 不读
        default_factory=dict
    )
    notes: str = ""  # external:report —— 人读说明，core 不读
