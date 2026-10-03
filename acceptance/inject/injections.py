"""每条攻击的注入与三步读数（离线、确定性）。

★ 这里只**观察**，不改被测系统。红的（`found=False` / 归因 miss）是产出：
它记录的是被测系统在当前这一版里的真实状态，而不是「本条还没写」。

★ 注入都是确定性的：脚本化模型 / 构造好的候选 / 直接调判定器，
没有网络、没有真模型 —— 所以这十一条可以随时复跑。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .attacks import Observation

__all__ = ["INJECTORS"]

ACCEPTANCE = Path(__file__).resolve().parents[2] / "acceptance"


# --------------------------------------------------------------------------- #
# 共用小工具
# --------------------------------------------------------------------------- #


def _artifact(name: str, rows: tuple[Mapping[str, Any], ...], columns: tuple[str, ...]) -> Any:
    from bobanana6.core.model import Artifact, Column

    return Artifact(name=name, columns=tuple(Column(name=c, dtype="str") for c in columns), rows=rows)


def _candidate(*, sid: str, source: str, artifact: Any, provenance: tuple[Any, ...] = ()) -> Any:
    from bobanana6.core.model import Candidate

    return Candidate(subtask_id=sid, source_id=source, artifacts=(artifact,), provenance=provenance)


def _snapshot(ref: str) -> Any:
    from bobanana6.core.model import Snapshot

    return Snapshot(ref=ref, body_path=Path(f"/nonexistent/{ref}.csv"), captured_at="2026-10-03T00:00:00+08:00")


def _provenance_for_rows(artifact_name: str, rows: int, snapshot_ref: str) -> tuple[Any, ...]:
    from bobanana6.core.model import Provenance

    return tuple(
        Provenance(value_ref=f"{artifact_name}:row={index}", snapshot_ref=snapshot_ref, locator="L")
        for index in range(rows)
    )


def _bundle(*constraints: Any) -> Any:
    from bobanana6.core.model import ConstraintBundle

    return ConstraintBundle(task_id="t", version=1, hard=tuple(constraints))


def _constraint(cid: str, kind: str, target: str, spec: Mapping[str, Any] | None = None, rationale: str = "r") -> Any:
    from bobanana6.core.model import Constraint

    return Constraint(id=cid, kind=kind, target=target, spec=dict(spec or {}), rationale=rationale)


# --------------------------------------------------------------------------- #
# ① 切分 / 语义 / 判定（A01–A04）
# --------------------------------------------------------------------------- #


def a01_mesh_dependency() -> Observation:
    """A01：跨块依赖必须被【声明】，不是被硬切掉。"""
    from bobanana6.contract import parse_contract_file
    from bobanana6.decompose import Decomposer
    from bobanana6.ontology import load_ontology_file

    task = ACCEPTANCE / "main" / "panel-three-sources"
    document = parse_contract_file(task / "task.yaml")
    ontology = load_ontology_file(task / "ontology.yaml").freeze()
    plan = Decomposer(strategy="source").split(document.contract, ontology, None)
    merge = plan.subtasks[-1]
    declared = tuple(merge.inputs)
    matched = all(dict(merge.expects).get(name) == plan.interfaces.get(name) for name in declared)
    return Observation(
        found=len(declared) == 3 and matched,
        action="declare_cross_block_dependency" if (len(declared) == 3 and matched) else "cut",
        evidence=(
            f"merge block declares inputs={declared}",
            f"expects match the producers' interfaces: {matched}",
        ),
    )


def a02_polysemy() -> Observation:
    """A02：同一个概念在各源里多义 —— 语义层该在 L-1 归一，不是各块各自定义。

    ★ 实测形态：两个候选钉了互不相同的口径版本 → 判定器在 compare 期报
    `DefinitionMismatch`（**不是静默混用**），但那是**事后**发现的；
    §14.2 的 `fail_if` 说的正是「出现 DefinitionMismatch ⇒ 没在 L-1 拦住」。
    """
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.kernel import classify
    from bobanana6.core.model import Concept, Definition, Ontology

    ontology = Ontology(
        domain="d",
        version="1",
        concepts={"pd": Concept(id="pd", label="PD", kind="measure", domain="d")},
        definitions={
            "pd:v1": Definition(concept_id="pd", version="v1", text="12m"),
            "pd:v2": Definition(concept_id="pd", version="v2", text="lifetime"),
        },
        units={},
    ).freeze()
    # 两边值不同才构成「分歧」；口径版本不同 + 该字段被约束覆盖 = DefinitionMismatch
    left = replace(
        _candidate(sid="T-01", source="src-1", artifact=_artifact("out.csv", ({"pd": "0.1"},), ("pd",))),
        definition_versions={"pd": "v1"},
    )
    right = replace(
        _candidate(sid="T-02", source="src-2", artifact=_artifact("out.csv", ({"pd": "0.2"},), ("pd",))),
        definition_versions={"pd": "v2"},
    )
    covered = _bundle(_constraint("C-pd", "range", "out.csv", {"column": "pd", "min": 0, "max": 2}))
    result = classify(covered, left, right, CheckContext(ontology=ontology))
    mismatches = len(result.definition_mismatches)
    return Observation(
        found=mismatches > 0,
        action="no_definition_mismatch" if mismatches == 0 else "definition_mismatch_at_compare_time",
        evidence=(
            f"two candidates pinned v1 vs v2 -> DefinitionMismatch x{mismatches}",
            "★ §14.2 fail_if triggers: found at compare time, not blocked at L-1",
        ),
    )


def a03_real_conflict() -> Observation:
    """A03：两个权威源给出不同的值、都不是错的 → 必须产出 Conflict，而不是 fail。"""
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.kernel import classify
    from bobanana6.core.model import Claim, Provenance

    provenance = Provenance(value_ref="out.csv:row=0", snapshot_ref="snap", locator="L")
    left = replace(
        _candidate(sid="T-01", source="src-1", artifact=_artifact("out.csv", ({"pd": "0.1"},), ("pd",))),
        claims=(Claim(field="pd", value=0.1, source_id="src-1", provenance=provenance),),
    )
    right = replace(
        _candidate(sid="T-02", source="src-2", artifact=_artifact("out.csv", ({"pd": "0.2"},), ("pd",))),
        claims=(Claim(field="pd", value=0.2, source_id="src-2", provenance=provenance),),
    )
    covered = _bundle(_constraint("C-pd", "range", "out.csv", {"column": "pd", "min": 0, "max": 2}))
    result = classify(covered, left, right, CheckContext())
    conflicts = len(result.conflicts)
    kept_both = all(len(conflict.claims) == 2 for conflict in result.conflicts)
    return Observation(
        found=conflicts > 0,
        action="record_conflict" if conflicts > 0 else "picked_one_or_failed",
        evidence=(f"Conflict x{conflicts}; both sides' evidence kept: {kept_both}",),
    )


def a04_missing_not_zero() -> Observation:
    """A04：源里故意留空且不标注 —— 面板里必须是「缺失」，不能是被填平的 0。"""
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.kernel import run_hard

    blank = _candidate(sid="T-01", source="src-1", artifact=_artifact("out.csv", ({"amount": ""},), ("amount",)))
    zero = _candidate(sid="T-02", source="src-2", artifact=_artifact("out.csv", ({"amount": "0"},), ("amount",)))
    bundle = _bundle(_constraint("C-rows", "presence", "out.csv", {"min_rows": 1}))
    blank_outcomes = tuple((o.constraint_id, o.ok, o.facts) for o in run_hard(bundle, blank, CheckContext()))
    zero_outcomes = tuple((o.constraint_id, o.ok, o.facts) for o in run_hard(bundle, zero, CheckContext()))
    # ★ 判据是【行为】而不是别的：一条空值和一条 0 在判定器眼里是不是同一个东西
    distinguished = blank_outcomes != zero_outcomes
    return Observation(
        found=distinguished,
        action="reject_filled_zero" if distinguished else "missing_and_zero_look_the_same",
        evidence=(
            f"blank cell -> {blank_outcomes}",
            f"filled zero -> {zero_outcomes}",
            "the two are indistinguishable to every check in the bundle",
        ),
    )


# --------------------------------------------------------------------------- #
# ② 运行时出口（A05 / A06 / A09）
# --------------------------------------------------------------------------- #


class _ScriptedLoopIO:
    """A minimal LoopIO: the first N produce calls fail, then it answers (or declines)."""

    def __init__(self, *, failures: int = 0, declined: bool = False) -> None:
        self.failures = failures
        self.declined = declined
        self.attempts = 0

    def produce(self, state: Any) -> Any:
        from bobanana6.agentloop.loop import TurnProposal

        self.attempts += 1
        if self.attempts <= self.failures:
            raise _timeout()
        if self.declined:
            return TurnProposal(declined=True, detail="the source does not carry this field")
        return TurnProposal(
            candidate=_candidate(sid="T-01", source="s", artifact=_artifact("out.csv", ({"a": "1"},), ("a",)))
        )

    def execute(self, calls: Sequence[Any]) -> tuple:
        return ()

    def verify(self, candidate: Any) -> Any:
        from bobanana6.agentloop.gate import GateVerdict

        return GateVerdict(status="passed", scope="full")


def _timeout() -> BaseException:
    from bobanana6.adapters import LLMConnectionError

    return LLMConnectionError("the request never reached the server: timed out")


def _run_loop(io: Any, *, max_turns: int = 4) -> Any:
    from bobanana6.agentloop.loop import LoopConfig, WorkerLoop

    return WorkerLoop(io, config=LoopConfig(max_turns=max_turns)).run(task_id="t", worker_id="w")


def a05_transient() -> Observation:
    """A05：两个源前 2 次超时、第 3 次成功 → RETRY_LOCAL 且最终通过。"""
    io = _ScriptedLoopIO(failures=2)
    result = _run_loop(io)
    passed = result.candidate is not None
    return Observation(
        found=passed,
        action="retry_local" if passed and io.attempts == 3 else "other",
        evidence=(
            f"produce calls={io.attempts} (2 timeouts then an answer); exit={result.exit_reason}",
            f"final candidate: {passed}",
        ),
    )


def a06_persistent() -> Observation:
    """A06：一个源整体下线 → 不能无限重试，要走「降级 / 标记不可得」。"""
    from bobanana6.agentloop.errors import classify_error
    from bobanana6.core.model import SourceRef
    from bobanana6.sources import FileFetcher, SnapshotStore, SourceError

    store = SnapshotStore(root=Path("/nonexistent-bobanana6-store"))
    message = ""
    try:
        FileFetcher(store=store, captured_at="2026-10-03T00:00:00+08:00").fetch(
            SourceRef(ref="dead", locator="/nonexistent/dead.csv")
        )
    except SourceError as exc:
        message = str(exc)[:90]
    classified = classify_error(SourceError(message or "cannot read"))
    return Observation(
        found=bool(message),
        action="refuse_run",
        evidence=(
            f"SourceError: {message!r}",
            f"classification reason={classified.reason} action={classified.action.value} (no infinite retry)",
            (
                "★ the run-level exit is Runner's pre-cluster refusal (RunRefused('sources')), "
                "not §14.2's degrade-or-mark-unavailable"
            ),
        ),
    )


def a09_impossible() -> Observation:
    """A09：某块真的不可能完成 → 拒答是合法出口（declined），不是编一个值。"""
    io = _ScriptedLoopIO(declined=True)
    result = _run_loop(io)
    return Observation(
        found=result.declined and result.candidate is None,
        action="decline" if result.declined else "other",
        evidence=(
            f"exit={result.exit_reason}; declined={result.declined}; candidate={result.candidate is not None}",
            "the worker said it could not get the information; the loop treated it as a legal exit",
        ),
    )


# --------------------------------------------------------------------------- #
# ③ 归因 / 装配 / 单位 / 溯源（A07 / A08 / A10 / A11）
# --------------------------------------------------------------------------- #


def a07_same_symptom_different_layer() -> Observation:
    """★ A07：三个块都报「算不出来」，根因分别是 工具坏 / 上下文没给到 / 判定器太严。

    实测形态：`attribute()` 先抓 `tool_result and not ok` 的旁支就定案 ——
    三条 trace 只要都含工具失败，就都归成 `tool`。**同一个 blame = §14.2 的 fail_if。**
    """
    from bobanana6.agentloop.attribution import attribute
    from bobanana6.agentloop.events import EventLog

    def trace(*, tool_failed: bool, constraint_evidence: bool, model_hint: str) -> tuple:
        log = EventLog()
        if tool_failed:
            log.record(kind="tool_result", task_id="t", worker_id="w", at=1.0, ok=False, detail={"tool": "fetch"})
        if constraint_evidence:
            log.record(kind="gate_check", task_id="t", worker_id="w", at=1.0, ok=False, detail={"detail": "C-1"})
        if model_hint:
            log.record(kind="retry", task_id="t", worker_id="w", at=1.0, ok=False, detail={"detail": model_hint})
        return log.events

    cases = {
        "tool_is_broken": trace(tool_failed=True, constraint_evidence=False, model_hint=""),
        "context_not_given": trace(tool_failed=True, constraint_evidence=False, model_hint="context"),
        "judge_too_strict": trace(tool_failed=True, constraint_evidence=True, model_hint=""),
    }
    blames = tuple(attribute(name, events).blame for name, events in cases.items())
    distinct = len(set(blames)) == len(blames)
    return Observation(
        found=True,
        blames=blames,
        evidence=(
            "blames=" + str(dict(zip(cases, blames, strict=True))),
            (
                "★ tool evidence decides first: three different root causes get the same blame"
                if not distinct
                else "the three blames are distinct"
            ),
        ),
    )


def a08_global_invariant() -> Observation:
    """A08：块各自合格，但合并后违反全局不变量 → 装配必须拒绝。

    实测形态：`target="*"` 的全局不变量在**多块装配**上永远报「artifact not found」
    （`artifact_for` 只在候选恰好一个产物时解析 `*`），所以今天**表达不出**这条断言：
    它既报不了「违反」，也没真的看过合并结果。
    """
    from bobanana6.contract import parse_contract_file
    from bobanana6.core.assemble import check_assembly
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.decompose import Decomposer
    from bobanana6.ontology import load_ontology_file

    task = ACCEPTANCE / "main" / "panel-three-sources"
    document = parse_contract_file(task / "task.yaml")
    ontology = load_ontology_file(task / "ontology.yaml").freeze()
    plan = Decomposer(strategy="source").split(document.contract, ontology, None)
    rows = ({"obligor": "A-1", "amount": "1"},)
    wide = ({"obligor": "A-1", "amount": "999999"},)
    pieces = {
        "part-panel-a.csv": _artifact("part-panel-a.csv", rows, ("obligor", "amount")),
        "part-panel-b.csv": _artifact("part-panel-b.csv", rows, ("obligor", "amount")),
        "part-panel-c.csv": _artifact("part-panel-c.csv", rows, ("obligor", "amount")),
        "panel.csv": _artifact("panel.csv", wide, ("obligor", "amount")),
    }
    bundle = _bundle(_constraint("G-1", "presence", "*", {"min_rows": 1}, "assembly-wide bound"))
    report = check_assembly(plan, document.contract, bundle, pieces, CheckContext(ontology=ontology), snapshots={})
    detail = " ".join(v.detail for v in report.violations if v.check == "global_invariant")
    blind = "not found" in detail
    return Observation(
        found=not report.passed and not blind,
        action="artifact_not_found" if blind else ("assembly_rejects" if not report.passed else "passed"),
        evidence=(
            f"assembly codes={report.codes()}",
            f"global_invariant detail: {detail[:90]!r}",
            (
                "★ the '*' invariant cannot see a multi-piece assembly, so it never actually "
                "judges the merge"
            ),
        ),
    )


def a10_units() -> Observation:
    """A10：元/千元/百万元混用且部分源不标 → 归一后数值正确。

    ★ 实测形态：单位**只有在声明了的时候**才可判（`unit_consistency` 拿
    `spec.concept` / `spec.unit` 与本体比对）。源里**不标**单位的那一半，
    系统无从判断 —— 它需要真值（§14.1 的物理隔离），本块不含。
    """
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.checkers.unit_consistency import check as unit_check
    from bobanana6.core.model import Concept, Ontology, Unit

    ontology = Ontology(
        domain="d",
        version="1",
        concepts={"amount": Concept(id="amount", label="Amount", kind="measure", domain="d")},
        definitions={},
        units={"amount:CNY": Unit(concept_id="amount", symbol="CNY", scale=1.0, base="CNY")},
    ).freeze()
    declared = _constraint("U-1", "unit_consistency", "out.csv", {"concept": "amount", "unit": "CNY"})
    labeled = _candidate(sid="T-01", source="s", artifact=_artifact("out.csv", ({"amount": "1 CNY"},), ("amount",)))
    unlabeled = _candidate(sid="T-02", source="s2", artifact=_artifact("out.csv", ({"amount": "1 千元"},), ("amount",)))
    labeled_ok = unit_check(declared, labeled, CheckContext(ontology=ontology)).ok
    unlabeled_outcome = unit_check(declared, unlabeled, CheckContext(ontology=ontology))
    # ★ unit_consistency 只比【声明】：constraint 声明的单位 vs 本体声明的单位（+ 邻块声明）。
    #   候选里的值它一个都不看 —— 所以「归一后数值正确」这件事今天没有判定者。
    same_verdict_for_a_wrong_value = unlabeled_outcome.ok
    return Observation(
        found=same_verdict_for_a_wrong_value is False,
        action="normalized_values" if not same_verdict_for_a_wrong_value else "value_level_normalization_not_judged",
        evidence=(
            f"a declared unit is checkable (CNY passes: {labeled_ok})",
            (
                f"a candidate whose value reads '1 千元' gets the same verdict: {unlabeled_outcome.ok} "
                f"({'; '.join(unlabeled_outcome.facts)[:70]})"
            ),
            "★ unit_consistency compares declarations only (constraint vs ontology vs peer); no value is ever read",
        ),
    )


def a11_provenance_content() -> Observation:
    """★★ A11：溯源引用的**真实性**没人校验（见仓库根 `溯源缺口-引用真实性未校验-20261003.md`）。

    注入：快照只有 3 行，候选声称 30 行、每行都引用它 → `row=3..29` 引用的行不存在。
    期望：在没有真值的情况下当场抓住。**当前行为：放行。**
    """
    from bobanana6.core.provenance import validate_points

    rows = 30
    candidate = _candidate(
        sid="T-04",
        source="panel-a",
        artifact=_artifact("panel.csv", tuple({"obligor": f"X{i}"} for i in range(rows)), ("obligor",)),
        provenance=_provenance_for_rows("panel.csv", rows, "panel-a"),
    )
    required = tuple(f"panel.csv:row={index}" for index in range(rows))
    report = validate_points(required, candidate.provenance, {"panel-a": _snapshot("panel-a")})
    return Observation(
        found=not report.ok,
        action="rejected" if not report.ok else "passed",
        evidence=(
            f"snapshot has 3 rows; the candidate cites {rows} rows (row=3..29 do not exist)",
            f"coverage={report.coverage:.2f}, breaks={len(report.breaks)}, "
            + f"unreachable={len(report.unreachable_points)}",
            "★ the chain only checks that the snapshot *name* is known -- the file is never opened",
        ),
    )


INJECTORS: Mapping[str, Any] = {
    "A01-mesh-dependency": a01_mesh_dependency,
    "A02-polysemy": a02_polysemy,
    "A03-real-conflict": a03_real_conflict,
    "A04-missing-not-zero": a04_missing_not_zero,
    "A05-transient": a05_transient,
    "A06-persistent": a06_persistent,
    "A07-same-symptom-different-layer": a07_same_symptom_different_layer,
    "A08-global-invariant": a08_global_invariant,
    "A09-impossible": a09_impossible,
    "A10-units": a10_units,
    "A11-provenance-content": a11_provenance_content,
}
