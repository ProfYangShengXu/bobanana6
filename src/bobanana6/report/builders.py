"""The four reports. This is what gives the ``external:report`` fields a reader.

``实现规格 §5.0.2`` fixes the rule: a field is kept when it carries *diagnosis*
("why was this refused", "why was it cut this way") **and** the report checklist
names which report reads it. Until this module existed, ``Constraint.rationale``,
``SplitPlan.recon_evidence``, ``Snapshot.source_kind``, ``Recon.findings``,
``TaskContract.notes`` and ``Relation.kind`` / ``Relation.cardinality`` were
annotated but unread -- decorative. Each renderer below says which field it is
the reader for, so the claim can be checked by deleting it and watching a test
go red.

The reports deliberately do not compute a single overall score: block level,
assembly level and attribution are reported side by side (``AGENTS §4.2②``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.model import (
    AssemblyOutcome,
    Candidate,
    Classification,
    ConstraintBundle,
    Convergence,
    Ontology,
    Provenance,
    Recon,
    Snapshot,
    SplitPlan,
    TaskContract,
    Verdict,
)

__all__ = ["ReportBundle", "build_reports", "render_conflicts", "render_ontology_map", "render_sources", "render_summary"]


@dataclass(frozen=True)
class ReportBundle:
    """The four artifacts, as text, plus the names they must be written under."""

    sources_md: str
    conflicts_md: str
    ontology_map_md: str
    summary_json: str

    def files(self) -> Mapping[str, str]:
        return {
            "SOURCES.md": self.sources_md,
            "CONFLICTS.md": self.conflicts_md,
            "ONTOLOGY-MAP.md": self.ontology_map_md,
            "summary.json": self.summary_json,
        }

    def write(self, directory: str | Path) -> tuple[Path, ...]:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for name, text in self.files().items():
            path = target / name
            path.write_text(text, encoding="utf-8")
            written.append(path)
        return tuple(written)


# --------------------------------------------------------------------------- #
# SOURCES.md -- reader for Snapshot.source_kind and Provenance.locator
# --------------------------------------------------------------------------- #


def render_sources(
    snapshots: Iterable[Snapshot],
    provenances: Iterable[Provenance],
    *,
    failures: Mapping[str, str] | None = None,
) -> str:
    ordered = sorted(snapshots, key=lambda s: (s.source_kind, s.ref))
    by_kind: dict[str, list[Snapshot]] = {}
    for snapshot in ordered:
        by_kind.setdefault(snapshot.source_kind or "<unspecified>", []).append(snapshot)

    lines = ["# SOURCES.md", "", "溯源链按来源分层。每一层是同一类载体（文件 / 网络 / 其它）。", ""]
    if any(str(kind).startswith("T") for kind in by_kind):
        lines.append("信任层：T1 官方文档/API · T2 论文/官方博客 · T3 社区 · T4 不知名博客/聚合站（只当线索）。")
        lines.append("")
    for kind in sorted(by_kind):
        lines.append(f"## {kind}")
        lines.append("")
        # ★ reader for Snapshot.backend / Snapshot.degraded —— `web-use` 纪律②：
        #   报告必须能看出"这条走的是哪条路、是不是降级拿到的"。
        lines.append("| ref | captured_at | 走的后端 | 降级 | body |")
        lines.append("|---|---|---|---|---|")
        for snapshot in by_kind[kind]:
            lines.append(
                f"| `{snapshot.ref}` | {snapshot.captured_at} | `{snapshot.backend or '-'}` | "
                f"{'是' if snapshot.degraded else '否'} | `{snapshot.body_path}` |"
            )
        lines.append("")

    locators = sorted({p.locator for p in provenances if p.locator})
    lines.extend(["## 定位串", ""])
    if locators:
        lines.extend(f"- `{locator}`" for locator in locators)
    else:
        lines.append("（本任务没有记录人读定位串）")
    lines.append("")

    if failures:
        lines.append("## 取回失败（显式失败，不是空快照）")
        lines.append("")
        for ref in sorted(failures):
            lines.append(f"- `{ref}`: {failures[ref]}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CONFLICTS.md -- reader for Constraint.rationale, Classification, Convergence
# --------------------------------------------------------------------------- #


def render_conflicts(
    bundle: ConstraintBundle,
    classification: Classification,
    *,
    convergence: Convergence | None = None,
    attribution: Mapping[str, Any] | None = None,
) -> str:
    lines = ["# CONFLICTS.md", "", "约束、冲突与归因。三类分歧分开报，不合并（`AGENTS §6`）。", ""]

    lines.extend(["## 硬约束（含「这条约束为什么存在」）", ""])
    lines.append("| id | kind | target | rationale |")
    lines.append("|---|---|---|---|")
    for constraint in sorted(bundle.hard, key=lambda c: c.id):
        lines.append(f"| `{constraint.id}` | {constraint.kind} | `{constraint.target}` | {constraint.rationale} |")
    lines.append("")

    if bundle.soft:
        lines.append("## 软约束")
        lines.append("")
        for constraint in sorted(bundle.soft, key=lambda c: c.id):
            lines.append(f"- `{constraint.id}` ({constraint.kind} → `{constraint.target}`): {constraint.rationale}")
        lines.append("")

    if bundle.blind:
        lines.append("## 已声明盲区")
        lines.append("")
        lines.extend(f"- {item}" for item in bundle.blind)
        lines.append("")

    if bundle.unmapped:
        lines.append("## 本体未覆盖的字段（带理由）")
        lines.append("")
        for name, reason in bundle.unmapped:
            lines.append(f"- `{name}`: {reason}")
        lines.append("")

    lines.extend(["## 分歧", ""])
    lines.append(f"- 盲区 BlindSpot: {len(classification.blind_spots)}")
    for spot in classification.blind_spots:
        lines.append(f"  - `{spot.path}`: {spot.left!r} vs {spot.right!r}")
    lines.append(f"- 冲突 Conflict: {len(classification.conflicts)}")
    for conflict in classification.conflicts:
        claims = ", ".join(f"{source}={value!r}" for source, value, _ in conflict.claims)
        lines.append(f"  - `{conflict.concept_id}` @{conflict.definition_version}: {claims}")
    lines.append(f"- 口径不一致 DefinitionMismatch: {len(classification.definition_mismatches)}")
    for mismatch in classification.definition_mismatches:
        lines.append(f"  - `{mismatch.concept_id}`: {', '.join(mismatch.versions)} ({', '.join(mismatch.parties)})")
    lines.append("")

    if convergence is not None:
        lines.append("## 收敛")
        lines.append("")
        lines.append(f"- 决策: `{convergence.decision}`")
        if convergence.note:
            lines.append(f"- 说明: {convergence.note}")
        lines.append("")

    if attribution is not None:
        lines.append("## 归因")
        lines.append("")
        lines.append(f"- failure: {attribution.get('failure')}")
        lines.append(f"- blame: `{attribution.get('blame')}`")
        lines.append(f"- confidence: `{attribution.get('confidence')}`")
        lines.append(f"- experiment: `{attribution.get('experiment')}`")
        actions = attribution.get("actions") or ()
        lines.append(f"- actions: {', '.join(f'`{a}`' for a in actions) if actions else '（没有可执行动作）'}")
        evidence = attribution.get("evidence") or ()
        for item in evidence:
            lines.append(f"  - evidence: `{item}`")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# ONTOLOGY-MAP.md -- reader for Concept/Definition/Unit/Relation fields
# --------------------------------------------------------------------------- #


def render_ontology_map(ontology: Ontology) -> str:
    lines = [
        "# ONTOLOGY-MAP.md",
        "",
        f"- domain: `{ontology.domain}`",
        f"- version: `{ontology.version}`",
        f"- frozen: `{ontology.is_frozen}`",
        "",
        "## 概念",
        "",
        "| id | label | kind | domain | parent | unit | definition |",
        "|---|---|---|---|---|---|---|",
    ]
    for concept_id in sorted(ontology.concepts):
        concept = ontology.concepts[concept_id]
        unit = next((u.symbol for u in ontology.units.values() if u.concept_id == concept_id), "")
        definition = next((d.version for d in ontology.definitions.values() if d.concept_id == concept_id), "")
        lines.append(
            f"| `{concept.id}` | {concept.label} | {concept.kind} | {concept.domain} | "
            f"{concept.parent or ''} | {unit} | {definition} |"
        )
    lines.append("")

    lines.extend(["## 口径（Definition）", ""])
    if ontology.definitions:
        for key in sorted(ontology.definitions):
            definition = ontology.definitions[key]
            lines.append(f"### `{definition.concept_id}` @ {definition.version}")
            lines.append("")
            lines.append(definition.text)
            lines.append("")
            if definition.formula:
                lines.append(f"- formula: `{definition.formula}`")
            for case in definition.edge_cases:
                lines.append(f"- edge case: {case}")
            lines.append("")
    else:
        lines.append("（没有声明口径）")
        lines.append("")

    lines.extend(["## 单位", ""])
    if ontology.units:
        lines.append("| key | concept | symbol | scale | base |")
        lines.append("|---|---|---|---|---|")
        for key in sorted(ontology.units):
            unit = ontology.units[key]
            lines.append(f"| `{key}` | `{unit.concept_id}` | {unit.symbol} | {unit.scale} | {unit.base} |")
    else:
        lines.append("（没有声明单位）")
    lines.append("")

    lines.extend(["## 关系（kind / cardinality：本报告的读者）", ""])
    if ontology.relations:
        lines.append("| src | kind | dst | cardinality |")
        lines.append("|---|---|---|---|")
        for relation in ontology.relations:
            lines.append(f"| `{relation.src}` | {relation.kind} | `{relation.dst}` | {relation.cardinality} |")
    else:
        lines.append("（没有声明关系）")
    lines.append("")

    if ontology.code_systems:
        lines.extend(["## 编码体系", ""])
        for name in sorted(ontology.code_systems):
            values = ", ".join(ontology.code_systems[name])
            lines.append(f"- `{name}`: {values}")
        lines.append("")
    if ontology.calendar:
        lines.extend(["## 日历", "", f"```json\n{json.dumps(dict(ontology.calendar), ensure_ascii=False, sort_keys=True)}\n```", ""])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# summary.json -- reader for TaskContract.notes, SplitPlan.recon_evidence,
# Recon.findings, Verdict.evidence, AssemblyOutcome.coverage, metrics
# --------------------------------------------------------------------------- #


def render_summary(
    *,
    contract: TaskContract,
    plan: SplitPlan,
    recon: Recon,
    convergence: Convergence,
    outcome: AssemblyOutcome,
    verdicts: Mapping[str, Verdict],
    candidates: Mapping[str, Candidate],
    snapshots: Mapping[str, Snapshot],
    metrics: Mapping[str, Any] | None = None,
    attribution: Mapping[str, Any] | None = None,
    blocks: Iterable[Mapping[str, Any]] = (),
    capability_matching: Mapping[str, Any] | None = None,
    exit_reason: str = "",
) -> str:
    document: dict[str, Any] = {
        "task": {
            "task_id": contract.task_id,
            "goal": contract.goal,
            # reader for TaskContract.notes
            "notes": contract.notes,
            "deliverables": [
                {"name": d.name, "required": d.required, "schema": dict(d.schema_payload)} for d in contract.deliverables
            ],
        },
        "plan": {
            "shape": plan.shape,
            "round": plan.round,
            "version": plan.version,
            "blocks": list(plan.subtask_ids),
            # reader for SplitPlan.recon_evidence
            "recon_evidence": plan.recon_evidence,
            "costs": None if plan.costs is None else {"merge": plan.costs.merge, "execute": plan.costs.execute},
        },
        "recon": {
            "source_ids": list(recon.source_ids),
            # reader for Recon.findings
            "findings": list(recon.findings),
        },
        "snapshots": [
            {
                "ref": snapshot.ref,
                # reader for Snapshot.source_kind
                "source_kind": snapshot.source_kind,
                # reader for Snapshot.backend / Snapshot.degraded —— 报告要能看出
                # "这条是走哪条路取回来的、是不是降级拿到的"（`web-use` 纪律②）
                "backend": snapshot.backend,
                "degraded": snapshot.degraded,
                "captured_at": snapshot.captured_at,
                "body_path": str(snapshot.body_path),
            }
            for snapshot in sorted(snapshots.values(), key=lambda s: s.ref)
        ],
        "blocks_report": [
            {
                "subtask": block_id,
                "ok": verdicts[block_id].ok if block_id in verdicts else None,
                "failed": list(verdicts[block_id].failed) if block_id in verdicts else [],
                # reader for Verdict.evidence / soft_scores
                "evidence": dict(verdicts[block_id].evidence) if block_id in verdicts else {},
                "soft_scores": dict(verdicts[block_id].soft_scores) if block_id in verdicts else {},
                "artifacts": list(candidates[block_id].artifact_names) if block_id in candidates else [],
                "source_id": candidates[block_id].source_id if block_id in candidates else "",
            }
            for block_id in plan.subtask_ids
        ],
        "assembly": {
            "passed": outcome.passed,
            "failed_blocks": list(outcome.failed_blocks),
            # reader for AssemblyOutcome.coverage
            "coverage": dict(outcome.coverage),
        },
        "convergence": {"decision": str(convergence.decision), "note": convergence.note},
        "capability_blocks": [dict(block) for block in blocks],
        # reader for the §6.8 needs<->provides matching outcome
        "capability_matching": dict(capability_matching or {}),
        "metrics": dict(metrics or {}),
        "exit_reason": exit_reason,
    }
    if attribution is not None:
        document["attribution"] = attribution
    # ★ No "overall" / "score" key: a single number would hide which of the three
    #   axes moved (AGENTS §4.2②).
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False)


def build_reports(
    *,
    contract: TaskContract,
    plan: SplitPlan,
    recon: Recon,
    ontology: Ontology,
    bundle: ConstraintBundle,
    convergence: Convergence,
    outcome: AssemblyOutcome,
    verdicts: Mapping[str, Verdict],
    candidates: Mapping[str, Candidate],
    snapshots: Mapping[str, Snapshot],
    provenances: Iterable[Provenance],
    fetch_failures: Mapping[str, str] | None = None,
    metrics: Mapping[str, Any] | None = None,
    attribution: Mapping[str, Any] | None = None,
    blocks: Iterable[Mapping[str, Any]] = (),
    capability_matching: Mapping[str, Any] | None = None,
    exit_reason: str = "",
) -> ReportBundle:
    return ReportBundle(
        sources_md=render_sources(snapshots.values(), provenances, failures=fetch_failures),
        conflicts_md=render_conflicts(bundle, convergence.classification, convergence=convergence, attribution=attribution),
        ontology_map_md=render_ontology_map(ontology),
        summary_json=render_summary(
            contract=contract,
            plan=plan,
            recon=recon,
            convergence=convergence,
            outcome=outcome,
            verdicts=verdicts,
            candidates=candidates,
            snapshots=snapshots,
            metrics=metrics,
            attribution=attribution,
            blocks=blocks,
            capability_matching=capability_matching,
            exit_reason=exit_reason,
        ),
    )
