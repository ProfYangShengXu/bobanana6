"""CI 落在 core 层：**三层门 + 快照 + 回归 diff**（补充规格 §二 / §三）。

**为什么能这么轻**：`core/` 自己声明它是"纯 stdlib、确定性、不做 I/O 的判定内核"
（`core/__init__.py`），而依赖方向由 `tests/test_arch_guard.py` 强制。所以"普通项目的 CI"
（拉代码 → 装依赖 → 起环境 → 跑测试）在这里塌缩成一句话：

    造一份【快照】 → 喂给 core → 拿报告

同一个快照喂进去，结果必须逐字相同 —— 这是本设计能成立的全部依据（报告带 digest）。

**这个模块只做调度，不做判定**（规格 §四③）。判据全部来自 `core/checkers` 的注册表与
`core/` 里那五个整跑函数；本模块里**没有一条硬编码的判据**，它只回答"哪个 kind 在哪一层跑"
与"失败落在哪个 scope"。判据的可证性靠一条守卫：`LAYER_KINDS` 必须与 `CHECKS` 的键集**完全相等**
（见 `tests/test_arch_guard.py`）—— 加了一个 checker 却没给它分层，守卫立刻红，
而不是静默漏判。

**三层与分档**（规格 §二）：

```
第 1 层 块级    core/checkers 的形状类   → 拒收（打回给模型，有界重试）   scope=block
第 2 层 装配级  core/checkers 的关系类   → 停这一块，别的继续            scope=block  ★ 绝不升格
第 3 层 交付级  core 的五个整跑判据      → 停全场                        scope=run    ★ 只有这层配
```

★ 升格被**结构性地**挡住：只要第 2 层停过任何一块，第 3 层就**不跑**、并具名记下原因。
否则"少了一块 ⇒ `cut_complete` 不过 ⇒ 停全场"会把一次局部的停块变成停全场 —— 那正是
规格禁止的"升格"，而且它会在最需要局部恢复的时候把整跑判死。

**报告口径**（§二，本项目既有教训）：

```
每条 violation 必须带【谁 / 期望什么 / 实际什么】：
  who      = "<块 id>/<约束 id>"（第 1、2 层）或 "run/<判据名>"（第 3 层）
  expected = 约束声明的 spec【原文】 或 判据名（期望来自声明，不是猜的）
  observed = 检查器逐字给的事实 / 判据给的 detail
```

⚠️ 已知边界（如实标注，见 `变更清单`）：本层判的是**结构**。`core` 判不了"这个数算对了吗"
（那要重算、要数据、要 I/O）—— 重算对账属**数据层**（`acceptance/`），两层报告**合成一份但代码不串**
（串了就破坏"箭头只朝 core"）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from ..core.assemble import check_assembly
from ..core.checkers import (  # noqa: F401  (check_kind: 调度面)
    CHECKS,
    JUDGMENT_ORDER,
    CheckContext,
    check_kind,
)
from ..core.consist import check_consistency
from ..core.kernel import admit
from ..core.model import (
    Artifact,
    Candidate,
    Column,
    Constraint,
    ConstraintBundle,
    PlanCosts,
    Provenance,
    Snapshot,
    SnapshotBody,
    SplitPlan,
    Subtask,
    TaskContract,
    Verdict,
)
from ..core.ontology import validate_ontology
from ..core.provenance import validate_chain
from ..core.split import check_split

__all__ = [
    "CI_TIERS",
    "INLINE_ARTIFACT_BYTES",
    "KIND_LAYER",
    "LAYERS",
    "LAYER_ASSEMBLY",
    "LAYER_BLOCK",
    "LAYER_DELIVERY",
    "LAYER_KINDS",
    "REPORT_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "BlockCapture",
    "CIReport",
    "SnapshotSource",
    "Tier",
    "Violation",
    "build_snapshot",
    "diff_reports",
    "plan_from_document",
    "run_ci",
]

SNAPSHOT_SCHEMA = "bobanana6.ci.snapshot/1"
REPORT_SCHEMA = "bobanana6.ci.report/1"

#: 超过这个体量的产物**不写进快照正文**，只放【摘要 + 指针】（规格 §三②）——
#: 与 `services/artifacts.py` 的 git 侧同一条思路、同一个数量级。
INLINE_ARTIFACT_BYTES = 64 * 1024

LAYER_BLOCK = "block"
LAYER_ASSEMBLY = "assembly"
LAYER_DELIVERY = "delivery"
LAYERS: tuple[str, ...] = (LAYER_BLOCK, LAYER_ASSEMBLY, LAYER_DELIVERY)

#: 哪个 kind 在哪一层 —— 这是**调度表，不是判据表**（规格 §四③）。
#: `custom` / `invariant` 归块级：它们最局部，scope 最小的那一层是安全默认。
LAYER_KINDS: Mapping[str, tuple[str, ...]] = {
    LAYER_BLOCK: (
        "presence",
        "schema",
        "range",
        "provenance",
        "frozen_files",
        "definition_pin",
        "concept_ref",
        "freshness",
        "invariant",
        "custom",
    ),
    LAYER_ASSEMBLY: ("relation", "corroboration", "coverage", "code_valid", "unit_consistency"),
}

KIND_LAYER: Mapping[str, str] = {
    kind: layer for layer, kinds in LAYER_KINDS.items() for kind in kinds
}

#: 第 3 层不是"按 kind 分派的 checker"，是 `core/` 里五个**整跑判据**（规格 §二）。
#: 顺序 = 报告的阅读顺序：整体语义 → 切分合法性 → 装配 → 溯源链。
DELIVERY_CHECKS: tuple[str, ...] = (
    "check_split",
    "check_consistency",
    "check_assembly",
    "validate_chain",
    "validate_ontology",
)

#: 每一层失败的落点。**scope 由层决定，不由"严重程度"决定** —— 于是"升格"不可能靠改一处判据做到。
SCOPE_BY_LAYER: Mapping[str, str] = {
    LAYER_BLOCK: "block",
    LAYER_ASSEMBLY: "block",
    LAYER_DELIVERY: "run",
}

#: 每个块的终态。
BLOCK_OK = "ok"
BLOCK_REFUSED = "refused"  # 第 1 层：拒收，打回给模型（有界重试）
BLOCK_STOPPED = "stopped"  # 第 2 层：停这一块，别的继续

RUN_CONTINUE = "continue"
RUN_STOPPED = "stopped"


@dataclass(frozen=True)
class Tier:
    """一档 CI：跑什么、什么时候选它、为什么（规格 §0.5② 分档 + §三④ 变异）。"""

    name: str
    when: str
    command: tuple[str, ...]
    why: str


#: ★ CI 分档（规格 §0.5②：一次全量 ~40 分钟、130 次调用，不能每次都跑全量）。
#: ★★ `mutation` 那一档是**必须挂的**（规格 §三④）：17 个 checker 任一个写错，
#: CI 就会给出**假绿** —— 所以"检查器自己也要被检查"。这是"CI 的 CI"。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PYTHON = ".venv/bin/python"
CI_TIERS: tuple[Tier, ...] = (
    Tier(
        name="smoke",
        when="只改了 brief / 报告 / 文档",
        command=(PYTHON, "-m", "pytest", "-m", "acceptance_smoke", "-q"),
        why="秒级；先确认没有把端到端入口弄坏",
    ),
    Tier(
        name="block",
        when="改了一个 checker 的判据（局部影响）",
        command=(PYTHON, "-m", "pytest", "-q", "tests/test_ci_gates.py", "tests/test_acceptance.py"),
        why="分层门 + 机制测试；判定语义改了就立刻跑，别排到系列末尾",
    ),
    Tier(
        name="full",
        when="改了 core / 装配 / 依赖方向",
        command=(PYTHON, "-m", "pytest", "-q"),
        why="全量 970+ 条；这是防回归的锚",
    ),
    Tier(
        name="mutation",
        when="改了任何 checker（含新增 kind）",
        command=(PYTHON, "tools/reverse_validation.py"),
        why="★ CI 的 CI：故意改坏每个判据，证明有测试会红 —— 抓 checker 自己写错导致的假绿",
    ),
)


class UnusableSnapshot(RuntimeError):
    """快照缺了某一层**必需**的输入（例如契约解析不出来）—— 具名拒。

    ★ 不做成"静默跳过那一层"：一个跑不起来的交付级门看起来和"过了"一模一样，
    那正是这份规格要消灭的假绿。
    """


class UnknownKind(LookupError):
    """一个已注册的 checker kind 没有被分层 —— 调度表与注册表漂了。"""


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Violation:
    """一条失败，按【谁 / 期望什么 / 实际什么】渲染（规格 §二）。"""

    layer: str
    who: str
    expected: str
    observed: str
    scope: str

    def key(self) -> str:
        """跨两次快照的稳定身份：**同一个规则 + 同一个实际观察**才算同一条。"""
        return f"{self.layer}|{self.who}|{self.expected}|{self.observed}"

    def as_document(self) -> dict[str, str]:
        return {
            "layer": self.layer,
            "who": self.who,
            "expected": self.expected,
            "observed": self.observed,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class CIReport:
    """一次 CI 跑的读数：三层的失败、每个块的终态、整跑的落点。"""

    task_id: str
    schema: str
    snapshot_digest: str
    violations: tuple[Violation, ...] = ()
    blocks: Mapping[str, str] = None  # type: ignore[assignment]
    run_scope: str = RUN_CONTINUE
    layers_run: tuple[str, ...] = ()
    #: (层, 为什么没跑) —— 跳过的层必须**具名**，不许静默省略。
    skipped: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks", dict(self.blocks or {}))

    @property
    def ok(self) -> bool:
        return not self.violations and self.run_scope == RUN_CONTINUE

    @property
    def stopped_blocks(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, state in self.blocks.items() if state == BLOCK_STOPPED))

    def of_layer(self, layer: str) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.layer == layer)

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "snapshot_digest": self.snapshot_digest,
            "layers_run": list(self.layers_run),
            "skipped": [{"layer": layer, "why": why} for layer, why in self.skipped],
            "blocks": dict(sorted(self.blocks.items())),
            "run_scope": self.run_scope,
            "violations": [violation.as_document() for violation in self.violations],
            "notes": list(self.notes),
        }

    def digest(self) -> str:
        return _digest(self.as_document())

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> CIReport:
        return cls(
            task_id=str(document["task_id"]),
            schema=str(document.get("schema", REPORT_SCHEMA)),
            snapshot_digest=str(document.get("snapshot_digest", "")),
            violations=tuple(
                Violation(
                    layer=str(entry["layer"]),
                    who=str(entry["who"]),
                    expected=str(entry["expected"]),
                    observed=str(entry["observed"]),
                    scope=str(entry["scope"]),
                )
                for entry in document.get("violations", ())
            ),
            blocks={str(k): str(v) for k, v in (document.get("blocks") or {}).items()},
            run_scope=str(document.get("run_scope", RUN_CONTINUE)),
            layers_run=tuple(str(x) for x in document.get("layers_run", ())),
            skipped=tuple(
                (str(entry["layer"]), str(entry["why"])) for entry in document.get("skipped", ())
            ),
            notes=tuple(str(x) for x in document.get("notes", ())),
        )


def diff_reports(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """★ 回归专用（规格 §三③）：core 判的是**一次快照**的静态一致，「这版比上版好还是坏」
    要**两次快照**的判定报告逐条对照。

    身份用 `Violation.key()`（层 + 谁 + 期望 + 实际）—— 同一条规则、同样的实际观察才算"还是那条"。
    """
    before = CIReport.from_document(previous)
    after = CIReport.from_document(current)
    old = {violation.key(): violation for violation in before.violations}
    new = {violation.key(): violation for violation in after.violations}
    return {
        "schema": "bobanana6.ci.diff/1",
        "from": before.snapshot_digest,
        "to": after.snapshot_digest,
        # ★ 两边都有的：不报（否则每次都被老问题淹没，"哪条是新出现的"就看不见了）
        "new": [new[key].as_document() for key in sorted(new) if key not in old],
        "fixed": [old[key].as_document() for key in sorted(old) if key not in new],
        "unchanged_count": len([key for key in new if key in old]),
        "blocks": {"from": dict(before.blocks), "to": dict(after.blocks)},
        "run_scope": {"from": before.run_scope, "to": after.run_scope},
    }


# --------------------------------------------------------------------------- #
# 快照：摘要 + 指针
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlockCapture:
    """一个块的痕迹，喂给 `build_snapshot` 的原始输入（调用方从跑测里取）。"""

    subtask_id: str
    output: str
    verdict: Verdict
    artifacts: tuple[Artifact, ...] = ()
    #: 产物名 -> 指针（`<commit>:<path>` 这类）。有指针的产物才可能只存摘要。
    artifact_refs: Mapping[str, str] = None  # type: ignore[assignment]
    #: ★ 这个块的引用点。按块分开记，**不许摊平**：摊平之后每个块都会拿到别人的引用，
    #: `validate_chain` 与 `coverage` 就会拿错对象去判（那是判据之外的错，更难查）。
    provenance: tuple[Provenance, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_refs", dict(self.artifact_refs or {}))


def build_snapshot(
    *,
    task_id: str,
    plan: SplitPlan,
    blocks: Sequence[BlockCapture],
    contract_pointer: Mapping[str, str] | None = None,
    ontology_pointer: Mapping[str, str] | None = None,
    snapshots: Mapping[str, Mapping[str, Any]] | None = None,
    snapshot_bodies: Mapping[str, SnapshotBody] | None = None,
    frozen_files: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """★ 快照只放【摘要 + 指针】（规格 §三②）：一次 13f 的产物有 212KB，
    全文装进快照等于把仓库换成一份转储。

    * 小产物（序列化后 <= `INLINE_ARTIFACT_BYTES`）**内联** —— 它是"判定所需的最小输入集"，
      少了它 CI 就得去够外部状态；**这是刻意的**，不是漏了指针化。
    * 大产物只写 `{name, sha256, bytes, lines, columns, ref}`；`ref` 由调用方给（git commit + 路径）。
    * `totals` 里同时记 `pointer_only_bytes` 与 `full_text_bytes`，
      于是"到底省了多少"是可报的数，不用信一句话。
    """
    entries: list[dict[str, Any]] = []
    pointer_only_bytes = 0
    full_text_bytes = 0
    for block in blocks:
        artifacts: list[dict[str, Any]] = []
        for artifact in block.artifacts:
            body = _artifact_document(artifact)
            rendered = _json_bytes(body)
            full_text_bytes += rendered
            if rendered <= INLINE_ARTIFACT_BYTES:
                artifacts.append({"name": artifact.name, "inline": body})
                continue
            pointer_only_bytes += rendered
            artifacts.append(
                {
                    "name": artifact.name,
                    "pointer": {
                        "ref": str(block.artifact_refs.get(artifact.name, "")),
                        "sha256": hashlib.sha256(
                            json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                        ).hexdigest(),
                        "bytes": rendered,
                        "lines": len(artifact.rows),
                        "columns": [
                            {"name": column.name, "dtype": column.dtype, "unit": column.unit}
                            for column in artifact.columns
                        ],
                    },
                }
            )
        entries.append(
            {
                "id": block.subtask_id,
                "output": block.output,
                "verdict": {
                    "ok": block.verdict.ok,
                    "failed": list(block.verdict.failed),
                    "evidence": dict(block.verdict.evidence),
                    "cause": block.verdict.cause,
                },
                "artifacts": artifacts,
            }
        )
    document: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "task_id": task_id,
        "plan": _plan_document(plan),
        "contract_file": dict(contract_pointer or {}),
        "ontology_file": dict(ontology_pointer or {}),
        "blocks": entries,
        "provenance": [
            {**_provenance_document(point), "block": block.subtask_id}
            for block in blocks
            for point in block.provenance
        ],
        "snapshots": {ref: dict(entry) for ref, entry in (snapshots or {}).items()},
        "snapshot_bodies": sorted((snapshot_bodies or {}).keys()),
        "frozen_files": {
            ref: {"frozen": pair[0], "current": pair[1]}
            for ref, pair in sorted((frozen_files or {}).items())
        },
        "totals": {
            "blocks": len(entries),
            "artifacts": sum(len(entry["artifacts"]) for entry in entries),
            "pointer_only_bytes": pointer_only_bytes,
            "full_text_bytes": full_text_bytes,
        },
    }
    return document


def snapshot_digest(document: Mapping[str, Any]) -> str:
    return _digest(document)


def plan_from_document(document: Mapping[str, Any]) -> SplitPlan:
    """把快照里的计划搬回 core 的 `SplitPlan`（**数据搬运，不是判定**）。"""
    costs = document.get("costs")
    return SplitPlan(
        task_id=str(document["task_id"]),
        version=int(document.get("version", 1)),
        round=int(document.get("round", 1)),
        subtasks=tuple(
            Subtask(
                id=str(entry["id"]),
                goal=str(entry.get("goal", "")),
                inputs=tuple(str(x) for x in entry.get("inputs", ())),
                output=str(entry["output"]),
                contract_ref=str(entry.get("contract_ref", "")),
                axis=str(entry.get("axis", "source")),
                mode=str(entry.get("mode", "read")),
                partition=entry.get("partition"),
                expects=tuple((str(a), str(b)) for a, b in entry.get("expects", ())),
                output_fields=tuple(str(x) for x in entry.get("output_fields", ())),
            )
            for entry in document["subtasks"]
        ),
        shape=str(document.get("shape", "star")),
        interfaces={str(k): str(v) for k, v in (document.get("interfaces") or {}).items()},
        recon_evidence=str(document.get("recon_evidence", "")),
        costs=None if costs is None else PlanCosts(merge=float(costs["merge"]), execute=float(costs["execute"])),
        refusal=str(document.get("refusal", "")),
    )


# --------------------------------------------------------------------------- #
# 跑三层门
# --------------------------------------------------------------------------- #


class SnapshotSource(Protocol):
    """把快照里的**指针**换成 core 要的对象。

    这个端口是"数据层"的边界（规格 §三①）：契约/本体/产物正文都是外围状态，
    `core/` 不许读，所以由外部实现注入。实现里可以做 I/O —— 本模块不做。
    """

    def contract(self) -> TaskContract: ...

    def ontology(self) -> Any: ...  # Ontology | None

    def bundle(self, plan: SplitPlan) -> ConstraintBundle:
        """**整份**约束包（第 3 层用：装配 / 切分 / 一致性看整体）。"""
        ...

    def block_bundle(self, block_id: str, plan: SplitPlan) -> ConstraintBundle:
        """**这一个块**的切片（第 1、2 层用）。

        ★★ 实现**必须复用** `assemble_impl/runtime.py::block_bundle`，不许在这里自己写
        "哪些约束判这个块" —— 那条判据（含字段级规则向本块产出重指向）已经有一份实现了，
        再写一份就是规格 §四③ 明令禁止的"判定逻辑的第二个副本"，
        而且两份会漂：改了 `block_bundle`，CI 报告必须跟着变。
        """
        ...

    def artifact(self, pointer: Mapping[str, Any]) -> Artifact: ...

    def snapshot(self, entry: Mapping[str, Any]) -> Snapshot: ...

    def snapshot_body(self, entry: Mapping[str, Any]) -> SnapshotBody | None: ...


def run_ci(document: Mapping[str, Any], source: SnapshotSource) -> CIReport:
    """跑三层门，返回读数。**判据一条都不在这里** —— 全部经 `core` 的注册表与判据函数。"""
    _require_layered()

    plan = plan_from_document(document["plan"])
    ontology = source.ontology()
    # ★ 每个块的**切片**由数据层经 `runtime.block_bundle` 给出（见端口注释）——
    #   本模块只决定"哪一层的 kind 什么时候跑"，不决定"哪些约束判这个块"。
    slices = {block_id: source.block_bundle(block_id, plan) for block_id in _block_ids(document)}
    for block_id, slice_ in slices.items():
        if slice_ is None:
            raise UnusableSnapshot(
                f"the snapshot carries no constraint bundle for block {block_id!r}; "
                f"refusing rather than judging it against nothing"
            )
    snapshots = {str(ref): source.snapshot(entry) for ref, entry in (document.get("snapshots") or {}).items()}
    bodies = {
        ref: body
        for ref, entry in (document.get("snapshots") or {}).items()
        if (body := source.snapshot_body(entry)) is not None
    }
    context = CheckContext(
        ontology=ontology,
        snapshots=snapshots,
        snapshot_bodies=bodies,
        frozen_files={
            str(ref): (str(pair["frozen"]), str(pair["current"]))
            for ref, pair in (document.get("frozen_files") or {}).items()
        },
    )

    captured = {str(entry["id"]): entry for entry in document["blocks"]}
    candidates = {
        block_id: Candidate(
            subtask_id=block_id,
            source_id=block_id,
            artifacts=tuple(
                source.artifact(entry["pointer"]) for entry in captured[block_id]["artifacts"] if "pointer" in entry
            )
            + tuple(
                _artifact_from_document(entry["inline"])
                for entry in captured[block_id]["artifacts"]
                if "inline" in entry
            ),
            provenance=tuple(
                Provenance(
                    value_ref=str(point["value_ref"]),
                    snapshot_ref=str(point["snapshot_ref"]),
                    locator=str(point.get("locator", "")),
                    source_row=point.get("source_row"),
                )
                for point in _points_for(document, block_id)
            ),
        )
        for block_id in captured
    }

    violations: list[Violation] = []
    blocks: dict[str, str] = {block_id: BLOCK_OK for block_id in captured}
    layers_run: list[str] = []
    skipped: list[tuple[str, str]] = []

    # -- 第 1 层 · 块级 ---------------------------------------------------- #
    layers_run.append(LAYER_BLOCK)
    for block_id, candidate in candidates.items():
        slice_ = _layer_bundle(slices[block_id], LAYER_BLOCK)
        verdict = admit(slice_, candidate, context)
        if verdict.ok:
            continue
        blocks[block_id] = BLOCK_REFUSED
        violations.extend(_violations_from_verdict(LAYER_BLOCK, block_id, verdict, slices[block_id]))

    # -- 第 2 层 · 装配级 -------------------------------------------------- #
    layers_run.append(LAYER_ASSEMBLY)
    for block_id, candidate in candidates.items():
        if blocks[block_id] == BLOCK_REFUSED:
            continue  # 还没被收下的块不参与装配级判断（它要回去重做）
        slice_ = _layer_bundle(slices[block_id], LAYER_ASSEMBLY)
        verdict = admit(slice_, candidate, context)
        if verdict.ok:
            continue
        blocks[block_id] = BLOCK_STOPPED
        violations.extend(_violations_from_verdict(LAYER_ASSEMBLY, block_id, verdict, slices[block_id]))

    # -- 第 3 层 · 交付级 -------------------------------------------------- #
    # ★ 升格门：第 2 层停过块 ⇒ 第 3 层【不跑】。否则"少一块 ⇒ cut_complete 不过 ⇒ 停全场"
    #   会把一次局部停块变成整跑判死 —— 那正是在最需要局部恢复时把整跑判死。
    stopped = [block_id for block_id, state in blocks.items() if state == BLOCK_STOPPED]
    if stopped:
        reason = (
            f"第 2 层停了 {len(stopped)} 块（{', '.join(sorted(stopped))}）—— 交付级门不跑，"
            "免得把局部停块升格成停全场（规格 §二：只有第 3 层配停全场）"
        )
        skipped.append((LAYER_DELIVERY, reason))
    else:
        layers_run.append(LAYER_DELIVERY)
        # ★ 契约**只在这一刻**才去要：第 2 层停过块时交付级门不跑，
        #   于是它连契约都不该读（少一次环境接触，也少一个"跑不了"的理由）。
        contract = source.contract()
        if contract is None:
            raise UnusableSnapshot("the snapshot resolves no task contract; the delivery layer cannot run")
        violations.extend(
            _delivery_violations(
                document,
                plan,
                contract,
                source.bundle(plan),
                ontology,
                candidates,
                snapshots,
                bodies,
            )
        )

    if any(v.scope == "run" for v in violations):
        run_scope = RUN_STOPPED
    else:
        run_scope = RUN_CONTINUE

    return CIReport(
        task_id=str(document["task_id"]),
        schema=REPORT_SCHEMA,
        snapshot_digest=snapshot_digest(document),
        violations=tuple(violations),
        blocks=blocks,
        run_scope=run_scope,
        layers_run=tuple(layers_run),
        skipped=tuple(skipped),
    )


# --------------------------------------------------------------------------- #
# 三层的实现（**只调 core**）
# --------------------------------------------------------------------------- #


def _layer_bundle(bundle: ConstraintBundle, layer: str) -> ConstraintBundle:
    """把 bundle 收成"只有这一层的 kind" —— 判据一个字没动，改的只是**跑哪些**。"""
    kinds = set(LAYER_KINDS[layer])
    return replace(bundle, hard=tuple(c for c in bundle.hard if c.kind in kinds), soft=())


def _violations_from_verdict(
    layer: str, block_id: str, verdict: Verdict, bundle: ConstraintBundle
) -> list[Violation]:
    by_id = {constraint.id: constraint for constraint in bundle.hard}
    rendered: list[Violation] = []
    for constraint_id in verdict.failed:
        constraint = by_id.get(constraint_id)
        rendered.append(
            Violation(
                layer=layer,
                who=f"{block_id}/{constraint_id}",
                # ★ 期望 = 约束**声明的** spec 原文；不是从事实里反推的，也不是复述判据
                expected=_expected(constraint),
                observed=str(verdict.evidence.get(constraint_id, "")),
                scope=SCOPE_BY_LAYER[layer],
            )
        )
    return rendered


def _delivery_violations(
    document: Mapping[str, Any],
    plan: SplitPlan,
    contract: TaskContract,
    bundle: ConstraintBundle,
    ontology: Any,
    candidates: Mapping[str, Candidate],
    snapshots: Mapping[str, Snapshot],
    bodies: Mapping[str, SnapshotBody],
) -> list[Violation]:
    """★ 第 3 层：**只调 core 的五个整跑判据**，一个都不重写。"""
    pieces: dict[str, Artifact] = {}
    for entry in document["blocks"]:
        for artifact in candidates[str(entry["id"])].artifacts:
            pieces[artifact.name] = artifact
    points = tuple(
        Provenance(
            value_ref=str(point["value_ref"]),
            snapshot_ref=str(point["snapshot_ref"]),
            locator=str(point.get("locator", "")),
            source_row=point.get("source_row"),
        )
        for point in document.get("provenance") or ()
    )

    found: list[Violation] = []
    if ontology is not None:
        for violation in validate_ontology(ontology).violations:
            found.append(
                Violation(
                    LAYER_DELIVERY,
                    f"run/validate_ontology:{violation.code}",
                    "ontology.validate_ontology",
                    f"{violation.detail} (ids={list(violation.ids)})",
                    "run",
                )
            )
    for violation in check_split(plan, contract, bundle, ontology).violations:
        found.append(
            Violation(
                LAYER_DELIVERY,
                f"run/check_split:{violation.check}",
                "split.check_split",
                violation.detail,
                "run",
            )
        )
    for violation in check_consistency(plan, contract, bundle, ontology).violations:
        found.append(
            Violation(
                LAYER_DELIVERY,
                f"run/check_consistency:{violation.check}",
                "consist.check_consistency",
                violation.detail,
                "run",
            )
        )
    for violation in check_assembly(
        plan,
        contract,
        bundle,
        pieces,
        CheckContext(ontology=ontology, snapshots=snapshots),
        provenance=points,
        snapshots=snapshots,
    ).violations:
        found.append(
            Violation(
                LAYER_DELIVERY,
                f"run/check_assembly:{violation.check}",
                "assemble.check_assembly",
                f"{violation.detail} (blocks={list(violation.block_ids)})",
                "run",
            )
        )
    for record in validate_chain(points, snapshots, bodies).breaks:
        found.append(
            Violation(
                LAYER_DELIVERY,
                f"run/validate_chain:{record.value_ref}",
                "provenance.validate_chain",
                f"missing snapshot {record.missing_snapshot!r}"
                + (f"; {record.detail}" if record.detail else ""),
                "run",
            )
        )
    return found


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #


def _block_ids(document: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(entry["id"]) for entry in document["blocks"])


def _require_layered() -> None:
    """★ 注册表与调度表必须**完全一致**：多一个 kind 没分层 ⇒ 具名拒，不静默漏判。"""
    missing = sorted(set(CHECKS) - set(KIND_LAYER))
    extra = sorted(set(KIND_LAYER) - set(CHECKS))
    if missing or extra:
        raise UnknownKind(
            f"checker kinds without a layer: {missing}; layers naming unknown kinds: {extra}"
        )


def _expected(constraint: Constraint | None) -> str:
    if constraint is None:
        return "(constraint not in bundle)"
    return json.dumps(
        {"id": constraint.id, "kind": constraint.kind, "target": constraint.target, "spec": dict(constraint.spec)},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _artifact_document(artifact: Artifact) -> dict[str, Any]:
    return {
        "name": artifact.name,
        "columns": [
            {"name": c.name, "dtype": c.dtype, "nullable": c.nullable, "unit": c.unit} for c in artifact.columns
        ],
        "rows": [dict(row) for row in artifact.rows],
        "text": artifact.text,
    }


def _artifact_from_document(document: Mapping[str, Any]) -> Artifact:
    return Artifact(
        name=str(document["name"]),
        columns=tuple(
            Column(
                name=str(entry["name"]),
                dtype=str(entry["dtype"]),
                nullable=bool(entry.get("nullable", True)),
                unit=str(entry.get("unit", "")),
            )
            for entry in document.get("columns", ())
        ),
        rows=tuple(dict(row) for row in document.get("rows", ())),
        text=document.get("text"),
    )


def _provenance_document(point: Provenance) -> dict[str, Any]:
    return {
        "value_ref": point.value_ref,
        "snapshot_ref": point.snapshot_ref,
        "locator": point.locator,
        "source_row": point.source_row,
    }


def _points_for(document: Mapping[str, Any], block_id: str) -> list[Mapping[str, Any]]:
    """★ 按块取点，不摊平 —— 见 `BlockCapture.provenance` 的注释。"""
    return [
        point for point in document.get("provenance") or () if str(point.get("block")) == block_id
    ]


def _plan_document(plan: SplitPlan) -> dict[str, Any]:
    return {
        "task_id": plan.task_id,
        "version": plan.version,
        "round": plan.round,
        "shape": plan.shape,
        "interfaces": dict(plan.interfaces),
        "recon_evidence": plan.recon_evidence,
        "refusal": plan.refusal,
        "costs": None if plan.costs is None else {"merge": plan.costs.merge, "execute": plan.costs.execute},
        "subtasks": [
            {
                "id": subtask.id,
                "goal": subtask.goal,
                "inputs": list(subtask.inputs),
                "output": subtask.output,
                "contract_ref": subtask.contract_ref,
                "axis": subtask.axis,
                "mode": subtask.mode,
                "partition": subtask.partition,
                "expects": [list(pair) for pair in subtask.expects],
                "output_fields": list(subtask.output_fields),
            }
            for subtask in plan.subtasks
        ],
    }


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
