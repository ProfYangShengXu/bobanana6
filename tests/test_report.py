"""report/: the four reports, and the ``external:report`` fields they read.

Each test names the field it is the reader for. If the field is dropped from the
renderer, the test goes red -- which is the only thing that makes the
``external:report`` annotation mean anything (`实现规格 §5.0.2`).
"""

from __future__ import annotations

import json
from pathlib import Path

from fakes import (
    artifact,
    bundle,
    candidate,
    constraint,
    contract,
    mini_ontology,
    plan,
    snapshot,
    subtask,
)

from bobanana6.core.model import (
    AssemblyOutcome,
    BlindSpot,
    Classification,
    Concept,
    ConvergeDecision,
    Convergence,
    Definition,
    DefinitionMismatch,
    Ontology,
    Provenance,
    Recon,
    Relation,
    TaskContract,
    Unit,
    Verdict,
)
from bobanana6.report import (
    build_reports,
    render_conflicts,
    render_ontology_map,
    render_sources,
    render_summary,
)

OUTCOME = AssemblyOutcome(passed=False, failed_blocks=("T-01",), coverage={"blocks": "0/1"})


def _summary(**kwargs: object) -> dict:
    base: dict = {
        "contract": contract(("out.csv",)),
        "plan": plan(subtasks=(subtask("T-01"),)),
        "recon": Recon(),
        "convergence": Convergence(decision=ConvergeDecision.DELIVER),
        "outcome": OUTCOME,
        "verdicts": {},
        "candidates": {},
        "snapshots": {},
    }
    base.update(kwargs)
    return json.loads(render_summary(**base))  # type: ignore[arg-type]


def test_sources_report_layers_by_source_kind() -> None:
    """Reader for ``Snapshot.source_kind`` and ``Provenance.locator``."""
    text = render_sources(
        [snapshot("s1"), snapshot("s2")],
        [Provenance(value_ref="out.csv:row=0", snapshot_ref="s1", locator="https://example.invalid/1")],
    )
    assert "## src-1" in text
    assert "`https://example.invalid/1`" in text
    assert {line for line in text.splitlines() if line.startswith("## ")} == {"## src-1", "## 定位串"}


def test_sources_report_lists_explicit_fetch_failures() -> None:
    text = render_sources([], [], failures={"src-9": "fetch of 'http://x' failed"})
    assert "取回失败" in text
    assert "src-9" in text


def test_conflicts_report_prints_every_rationale() -> None:
    """Reader for ``Constraint.rationale``."""
    text = render_conflicts(
        bundle(hard=(constraint("C-1", "presence", "T-01", {"min_rows": 1}, rationale="the panel must not be empty"),)),
        Classification(),
    )
    assert "the panel must not be empty" in text
    assert "`C-1`" in text


def test_conflicts_report_separates_the_three_divergence_classes() -> None:
    classification = Classification(
        blind_spots=(BlindSpot(path="out.csv:amount", left=1, right=2),),
        definition_mismatches=(DefinitionMismatch(concept_id="pd", versions=("v1", "v2"), parties=("a", "b")),),
    )
    text = render_conflicts(
        bundle(),
        classification,
        convergence=Convergence(decision=ConvergeDecision.REFINE_SPLIT, note="add a constraint"),
    )
    assert "盲区 BlindSpot: 1" in text
    assert "口径不一致 DefinitionMismatch: 1" in text
    assert "add a constraint" in text


def test_conflicts_report_renders_attribution_and_actions() -> None:
    text = render_conflicts(
        bundle(),
        Classification(),
        attribution={
            "failure": "assembly failed",
            "blame": "tool",
            "confidence": "medium",
            "experiment": "not_run",
            "actions": ["switch_source", "repair_tool"],
            "evidence": ["tool_result(T-01)"],
        },
    )
    assert "switch_source" in text and "repair_tool" in text
    assert "tool_result(T-01)" in text


def test_ontology_map_renders_relations_with_kind_and_cardinality() -> None:
    """Reader for ``Relation.kind`` and ``Relation.cardinality``."""
    ontology = Ontology(
        domain="credit",
        version="2026.1",
        concepts={"pd": Concept(id="pd", label="PD", kind="measure", domain="credit")},
        definitions={
            "pd:v1": Definition(
                concept_id="pd", version="v1", text="point in time", formula="x/y", edge_cases=("restructured",)
            )
        },
        units={"pd:ratio": Unit(concept_id="pd", symbol="ratio", scale=1.0, base="ratio")},
        relations=(Relation(src="pd", dst="pd", kind="measured_in", cardinality="N:1"),),
        code_systems={"scale": ("A", "B")},
        calendar={"timezone": "Asia/Shanghai"},
    ).freeze()
    text = render_ontology_map(ontology)
    assert "| `pd` | measured_in | `pd` | N:1 |" in text
    assert "point in time" in text and "x/y" in text and "restructured" in text
    assert "`scale`: A, B" in text
    assert "Asia/Shanghai" in text


def test_summary_has_no_single_overall_score() -> None:
    summary = _summary()
    assert "overall" not in summary
    assert "score" not in summary


def test_summary_reads_notes_recon_evidence_findings_and_coverage() -> None:
    """Readers for ``TaskContract.notes``, ``SplitPlan.recon_evidence``,
    ``Recon.findings`` and ``AssemblyOutcome.coverage``."""
    summary = _summary(
        contract=TaskContract(task_id="t", goal="g", notes="hand written note"),
        plan=plan(subtasks=(subtask("T-01"),), recon_evidence="surveyed 3 sources"),
        recon=Recon(source_ids=("s1",), findings=("s1: file",)),
    )
    assert summary["task"]["notes"] == "hand written note"
    assert summary["plan"]["recon_evidence"] == "surveyed 3 sources"
    assert summary["recon"]["findings"] == ["s1: file"]
    assert summary["assembly"]["coverage"] == {"blocks": "0/1"}


def test_summary_reports_verdict_evidence_and_soft_scores_per_block() -> None:
    """Reader for ``Verdict.evidence`` / ``Verdict.soft_scores``."""
    summary = _summary(
        verdicts={"T-01": Verdict(ok=True, evidence={"C-1": "3 rows"}, soft_scores={"S-1": 0.5})},
        candidates={"T-01": candidate(artifacts=(artifact(),))},
    )
    block = summary["blocks_report"][0]
    assert block["evidence"] == {"C-1": "3 rows"}
    assert block["soft_scores"] == {"S-1": 0.5}
    assert block["artifacts"] == ["out.csv"]


def test_summary_marks_capability_matching_as_not_evaluated_when_undeclared() -> None:
    summary = _summary()
    assert summary["capability_matching"] == {}


def test_build_reports_writes_all_four_files(tmp_path: Path) -> None:
    reports = build_reports(
        contract=contract(("out.csv",)),
        plan=plan(subtasks=(subtask("T-01"),)),
        recon=Recon(),
        ontology=mini_ontology(),
        bundle=bundle(),
        convergence=Convergence(decision=ConvergeDecision.DELIVER),
        outcome=OUTCOME,
        verdicts={},
        candidates={},
        snapshots={},
        provenances=(),
    )
    written = reports.write(tmp_path)
    assert {path.name for path in written} == {"SOURCES.md", "CONFLICTS.md", "ONTOLOGY-MAP.md", "summary.json"}
    assert all(path.read_text(encoding="utf-8") for path in written)
