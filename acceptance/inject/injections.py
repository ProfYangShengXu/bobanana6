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
    """A04：源里故意留空且不标注 —— 面板里必须是「缺失」，不能是被填平的 0。

    ★ 修后：「缺失」是**一等状态**（`core.model.MISSING`），不再是「某个空串」。
    读取端把空单元格映射成 `MISSING`；列声明了 `nullable: false` 时，`schema`
    逐格判它 —— 空白格报到 `row=0`，而被填平的 `0` 是一个值、照样通过。
    两者因此不再同形（对照读数见 acceptance/inject/README.md）。
    """
    import json

    from bobanana6.assemble_impl.produce import parse_candidate
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.kernel import run_hard
    from bobanana6.core.model import Subtask

    block = Subtask(
        id="T-01", goal="read one source", inputs=(), output="out.csv", contract_ref="t", axis="field", mode="read"
    )

    def candidate_from(cell: Any) -> Any:
        answer = json.dumps(
            {
                "ref": "snap-1",
                "columns": [{"name": "amount", "dtype": "str", "nullable": False}],
                "rows": [{"amount": cell}],
            }
        )
        return parse_candidate(answer, subtask=block, source_id="snap-1", authorized_sources=("snap-1",))

    blank = candidate_from("")  # ★ 空单元格：读取端记成 MISSING，不是 ""
    zero = candidate_from(0)  # ★ 被填平的 0：一个值
    bundle = _bundle(
        _constraint(
            "C-amount",
            "schema",
            "out.csv",
            {"columns": [{"name": "amount", "dtype": "str", "nullable": False}]},
        )
    )
    blank_outcomes = tuple((o.constraint_id, o.ok, o.facts) for o in run_hard(bundle, blank, CheckContext()))
    zero_outcomes = tuple((o.constraint_id, o.ok, o.facts) for o in run_hard(bundle, zero, CheckContext()))
    # ★ 判据是【行为】而不是别的：一条空值和一条 0 在判定器眼里是不是同一个东西
    distinguished = blank_outcomes != zero_outcomes
    return Observation(
        found=distinguished,
        action="reject_missing_not_fill_zero" if distinguished else "missing_and_zero_look_the_same",
        evidence=(
            f"missing cell -> {blank_outcomes}",
            f"filled zero -> {zero_outcomes}",
            (
                "★ a missing cell is a first-class state, so a not-null column can name it"
                if distinguished
                else "the two are still indistinguishable to every check in the bundle"
            ),
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
    """A06：一个源整体下线 → 【不是】无限重试，也不是把可用的结果一起丢掉。

    ★ 修后：`_fetch_sources` 只在**一个源都没取到**时拒跑；部分失败时用剩下的源继续，
    并把缺席的源标出来（`SOURCES.md` + 验收 note）。跑的是【真 pipeline】：把面板题
    复制一份、删掉一个源的文件 —— 不是把判决写死在注入里。
    """
    import shutil
    import tempfile

    from bobanana6.adapters import EchoToolResultClient

    def _run(*, dead: tuple[str, ...]) -> Any:
        from acceptance.harness import run_task

        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "panel-three-sources"
            shutil.copytree(
                ACCEPTANCE / "main" / "panel-three-sources",
                task,
                ignore=shutil.ignore_patterns("_run", "__pycache__", "generate.py"),
            )
            for ref in dead:
                (task / "input" / f"{ref}.csv").unlink(missing_ok=True)
            return run_task(task, client=EchoToolResultClient(), env={})

    partial = _run(dead=("panel-b",))
    total = _run(dead=("panel-a", "panel-b", "panel-c"))
    marked = next((note for note in partial.report.notes if "unavailable" in note), "")
    degraded = partial.refused is None and bool(marked)
    still_refuses = total.refused == "sources"
    return Observation(
        found=degraded,  # 停下来了：没有无限重试，也没有把 run 整个丢掉
        action="degrade_or_mark_unavailable" if degraded and still_refuses else "refuse_run",
        evidence=(
            f"one source down -> refused={partial.refused!r}, blocks={partial.blocks!r}, note={marked!r}",
            f"all sources down -> refused={total.refused!r} (该拒的还是要拒)",
            "★ 降级与拒跑的界线：缺席的是【一个输入】就降级并标记；一个源都没有才拒跑",
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

    ★ 修后：三条 trace **仍然都含「工具失败」这个症状**，但病因改由
    「产生失败的那一方自己声明」（`AGENTS.md §7⑫` 的同一个手法）：
      工具坏        → 工具标了自己的失败是确定性的（`deterministic`）
      上下文没给到  → 分离实验（把上下文交给人都答不出来）
      判定器太严    → 判定器宣告了它拒的那条约束（`constraint_rejected`）
    归因层先读声明，不再按固定优先级把「有工具失败」当死因。
    """
    from bobanana6.agentloop.attribution import Experiment, attribute
    from bobanana6.agentloop.events import EventLog

    def trace(*, deterministic: bool = False, constraint_rejected: bool = False) -> tuple:
        log = EventLog()
        detail: dict[str, Any] = {"tool": "fetch"}
        if deterministic:
            detail["deterministic"] = True
        log.record(kind="tool_result", task_id="t", worker_id="w", at=1.0, ok=False, detail=detail)
        if constraint_rejected:
            log.record(
                kind="gate_check",
                task_id="t",
                worker_id="w",
                at=1.0,
                ok=False,
                detail={"constraint_rejected": True, "detail": "C-1"},
            )
        return log.events

    cases = {
        "tool_is_broken": (trace(deterministic=True), Experiment.NOT_RUN),
        "context_not_given": (trace(), Experiment.HUMAN_CANNOT_ANSWER),
        "judge_too_strict": (trace(constraint_rejected=True), Experiment.NOT_RUN),
    }
    blames = tuple(
        attribute(name, events, experiment=experiment).blame for name, (events, experiment) in cases.items()
    )
    distinct = len(set(blames)) == len(blames)
    return Observation(
        found=True,
        blames=blames,
        evidence=(
            "blames=" + str(dict(zip(cases, blames, strict=True))),
            (
                "★ all three blocks report the same symptom (a failed tool); each layer declares its own cause"
                if distinct
                else "the three blames are still not distinct"
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

    ★ 修后：单位检查**读值里的单位**（`"1 CNY"` / `"1 千元"`），不再只比两边的声明。
    值里带了一个不同/未声明的单位 → 当场报到具体位置；值里**不带**单位的那种
    仍然无法判（那需要真值或声明），这一条残留如实写在证据里，不假装通过。
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

    def verdict(cell: Any) -> Any:
        candidate = _candidate(sid="T-01", source="s", artifact=_artifact("out.csv", ({"amount": cell},), ("amount",)))
        return unit_check(declared, candidate, CheckContext(ontology=ontology))

    matching = verdict("1 CNY")
    wrong_unit = verdict("1 千元")
    no_unit = verdict("1")
    # ★ 判据：值里写了另一个单位必须被抓；写了同一个单位必须放行；不带单位的仍判不了。
    caught_wrong_unit = not wrong_unit.ok
    return Observation(
        found=caught_wrong_unit,
        action="value_level_unit_read" if caught_wrong_unit else "value_level_unit_not_read",
        evidence=(
            f"a declared unit is checkable (CNY passes: {matching.ok})",
            f"a value reading '1 千元' -> ok={wrong_unit.ok} ({'; '.join(wrong_unit.facts)[:80]})",
            f"a value carrying no unit ('1') -> ok={no_unit.ok} (still unjudgeable without a declaration)",
        ),
    )


def a11_provenance_content() -> Observation:
    """★★ A11：溯源引用的**真实性**（见仓库根 `溯源缺口-引用真实性未校验-20261003.md`）。

    注入：快照只有 3 行，候选声称 30 行、每行都引用它 → `row=3..29` 引用的行不存在。
    ★ 修后：快照内容由外侧读好、以 `SnapshotBody` 注入判定器，链**逐点重放**：
    引用的行必须在、值必须对得上。名字在注册表里不再等于引用为真。
    """
    from bobanana6.core.model import SnapshotBody
    from bobanana6.core.provenance import validate_points

    rows = 30
    candidate = _candidate(
        sid="T-04",
        source="panel-a",
        artifact=_artifact("panel.csv", tuple({"obligor": f"X{i}"} for i in range(rows)), ("obligor",)),
        provenance=_provenance_for_rows("panel.csv", rows, "panel-a"),
    )
    required = tuple(f"panel.csv:row={index}" for index in range(rows))
    body = SnapshotBody(
        ref="panel-a",
        rows=tuple({"obligor": f"A-{index:03d}"} for index in range(3)),  # ★ 快照真的只有 3 行
    )
    report = validate_points(required, candidate.provenance, {"panel-a": _snapshot("panel-a")}, {"panel-a": body})
    return Observation(
        found=not report.ok,
        action="rejected" if not report.ok else "passed",
        evidence=(
            f"snapshot has 3 rows; the candidate cites {rows} rows (row=3..29 do not exist)",
            f"coverage={report.coverage:.2f}, breaks={len(report.breaks)}, "
            + f"unreachable={len(report.unreachable_points)}",
            (
                "★ the chain now replays the citation against the frozen body"
                if not report.ok
                else "★ the chain still only checks that the snapshot *name* is known"
            ),
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


# --------------------------------------------------------------------------- #
# 压力任务 A 上的读数（`架构设计.md §15.3`：注入集该打【压力集 + 已知缺陷】）
#
# ★ 只【加读数】：上面主验题那 11 条的代码一行没动。
# ★ 每条的 evidence 第一行写明这是哪道题上的读数，方便和主验题逐条对照。
# ★ 只有真的读题的攻击才在这里有变体；不读题的（构造夹具）在两题上读数必然相同。
# --------------------------------------------------------------------------- #

PRESSURE = ACCEPTANCE / "stress"


def _pressure_parts() -> tuple[Any, Any, Any]:
    """压力任务 A 的 (document, ontology, plan) —— 与 run 用的是同一条装配路径。"""
    from bobanana6.contract import parse_contract_file
    from bobanana6.decompose import Decomposer
    from bobanana6.ontology import load_ontology_file

    document = parse_contract_file(PRESSURE / "task.yaml")
    ontology = load_ontology_file(PRESSURE / "ontology.yaml").freeze()
    plan = Decomposer(strategy="source").split(document.contract, ontology, None)
    return document, ontology, plan


def a01_on_pressure() -> Observation:
    """A01 在压力题上：23 条源边 + 1 个合并块，依赖有没有被【声明】而不是硬切掉。"""
    _document, _ontology, plan = _pressure_parts()
    merge = plan.subtasks[-1]
    declared = tuple(merge.inputs)
    matched = all(dict(merge.expects).get(name) == plan.interfaces.get(name) for name in declared)
    reads = sum(1 for block in plan.subtasks if block.mode == "read")
    fine = len(declared) > 1 and matched
    return Observation(
        found=fine,
        action="declare_cross_block_dependency" if fine else "cut",
        evidence=(
            f"[pressure task A] plan: {len(plan.subtasks)} block(s) = {reads} read + 1 merge",
            f"the merge declares {len(declared)} input(s): {declared[:2]} … {declared[-1:]}",
            f"every declared input matches its producer's interface: {matched}",
            (
                f"only {len({path.stem for path in (PRESSURE / 'input').glob('*')})} declared sources have"
                " an input file — the run drops the dead one before splitting, the static plan does not"
            ),
        ),
    )


def a03_on_pressure() -> Observation:
    """A03 在压力题上：任务里【真的有】一个真冲突（s02 vs s20 的 ENT-03|2022）。

    问的是：跑的那条路走得到它吗。判定内核会记 Conflict（A03 主验读数已证），
    但装配关拿到的是 `pieces: Artifact`、不是两个候选的 `claims` —— 所以两个块
    永远不会被放在一起比。
    """
    from bobanana6.core.assemble import check_assembly
    from bobanana6.core.checkers.base import CheckContext
    from bobanana6.core.kernel import classify
    from bobanana6.core.model import Claim, Provenance

    document, ontology, plan = _pressure_parts()
    conflict = None
    generate = PRESSURE / "generate.py"
    if generate.is_file():  # 题面自己声明了这个冲突，别在注入里重写它
        import importlib.util

        spec = importlib.util.spec_from_file_location("_stress_meta", generate)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        conflict = dict(module.CONFLICT)

    point = Provenance(value_ref="part-s02.csv:row=2", snapshot_ref="s02", locator="L")
    # 两个块各读一份源，对同一个 (实体, 年) 给出不同值 —— 都不是错的
    left = replace(
        _candidate(sid="T-02", source="s02", artifact=_artifact("panel.csv", ({"revenue": "3010000.0"},), ("revenue",))),
        claims=(Claim(field="revenue", value=3010000.0, source_id="s02", provenance=point),),
    )
    right = replace(
        _candidate(sid="T-20", source="s20", artifact=_artifact("panel.csv", ({"revenue": "9000000.0"},), ("revenue",))),
        claims=(Claim(field="revenue", value=9000000.0, source_id="s20", provenance=point),),
    )
    bundle = _bundle(_constraint("C-revenue", "range", "panel.csv", {"column": "revenue", "min": 0, "max": 1e12}))
    kernel = classify(bundle, left, right, CheckContext(ontology=ontology))
    rows = ({"obligor": "ENT-03", "revenue": "3010000.0"},)
    wide = ({"obligor": "ENT-03", "revenue": "9000000.0"},)
    pieces = {
        block.output: _artifact(block.output, wide if block.output == "panel.csv" else rows, ("obligor", "revenue"))
        for block in plan.subtasks
    }
    report = check_assembly(plan, document.contract, bundle, pieces, CheckContext(ontology=ontology), snapshots={})
    codes = tuple(report.codes())
    compared = any("conflict" in code for code in codes)
    return Observation(
        found=compared,  # ★ ① 问的是【跑的那条路】有没有注意到它，不是内核有没有这个能力
        action="record_conflict" if compared else "not_compared",
        evidence=(
            f"[pressure task A] the task itself declares a real conflict: {conflict}",
            (
                "★ the capability is there: handed both candidates, the kernel records "
                f"Conflict x{len(kernel.conflicts)}"
            ),
            f"the assembly stage only ever gets artifacts; its codes={codes} carry no conflict",
            "★ 两个块的候选永远不会被放在一起 → 这道题里的真冲突不被记录（跨块不比较，见 #63）",
        ),
    )


def a06_on_pressure() -> Observation:
    """A06 在压力题上：题目自带一个永远取不到的源（s24）→ 该降级并标记；全缺才拒跑。"""
    import shutil
    import tempfile

    from bobanana6.adapters import EchoToolResultClient

    def _run(*, dead_all: bool) -> Any:
        from acceptance.harness import run_task

        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "stress"
            shutil.copytree(
                PRESSURE,
                task,
                ignore=shutil.ignore_patterns("_run", "__pycache__", "generate.py", "truth"),
            )
            if dead_all:
                for path in (task / "input").glob("*"):
                    path.unlink()
            return run_task(task, client=EchoToolResultClient(), env={}, concurrency=32)

    partial = _run(dead_all=False)  # s24 按题目设计本来就没有输入文件
    total = _run(dead_all=True)
    marked = next((note for note in partial.report.notes if "unavailable" in note), "")
    degraded_by_design = bool(partial.refused is None and marked)
    still_refuses = total.refused == "sources"
    return Observation(
        found=degraded_by_design,
        action="degrade_or_mark_unavailable" if degraded_by_design and still_refuses else "refuse_run",
        evidence=(
            (
                f"[pressure task A] one dead source by design -> refused={partial.refused!r}, "
                f"blocks={len(partial.blocks)}, note={marked!r}"
            ),
            f"all {len(list((PRESSURE / 'input').glob('*')))} inputs removed -> refused={total.refused!r}",
            "★ 同一个门：少一个输入就降级并标记，一个源都没有才拒跑",
        ),
    )


def a08_on_pressure() -> Observation:
    """A08 在压力题上：块数从 3 涨到 23，`target="*"` 还是不是看不见合并结果。"""
    from bobanana6.core.assemble import check_assembly
    from bobanana6.core.checkers.base import CheckContext

    document, ontology, plan = _pressure_parts()
    names = [block.output for block in plan.subtasks]
    rows = ({"obligor": "ENT-01", "exposure": "1"},)
    wide = ({"obligor": "ENT-01", "exposure": "999999"},)
    pieces = {
        name: _artifact(name, wide if name == "panel.csv" else rows, ("obligor", "exposure")) for name in names
    }
    bundle = _bundle(_constraint("G-1", "presence", "*", {"min_rows": 1}, "assembly-wide bound"))
    report = check_assembly(plan, document.contract, bundle, pieces, CheckContext(ontology=ontology), snapshots={})
    detail = " ".join(v.detail for v in report.violations if v.check == "global_invariant")
    blind = "not found" in detail
    return Observation(
        found=bool(not blind and not report.passed),
        action="artifact_not_found" if blind else ("assembly_rejects" if not report.passed else "passed"),
        evidence=(
            f"[pressure task A] {len(pieces)} piece(s), the panel visibly disagrees with the parts",
            f"global_invariant detail: {detail[:80]!r}",
            "★ 与主验题同一读数：`*` 在多块装配上永远看不见产物（缺口与块数无关）",
        ),
    )


def a09_on_pressure() -> Observation:
    """A09 在压力题上：⑨ 说「关键字段只在已下线的源里」—— 那条出口在这题上走得到吗。"""
    from acceptance.harness import run_task
    from bobanana6.adapters import EchoToolResultClient

    document, _ontology, _plan = _pressure_parts()
    present = {path.stem for path in (PRESSURE / "input").glob("*")}
    dead = sorted({str(source.ref) for source in document.contract.sources} - present)
    run = run_task(PRESSURE, client=EchoToolResultClient(), env={}, concurrency=32)
    exited = {
        str(run.verdicts[block].evidence.get("exit"))
        for block in run.blocks
        if getattr(run.verdicts.get(block), "evidence", None)
    }
    declined = sorted(exit_ for exit_ in exited if exit_ and "decline" in exit_)
    note = next((item for item in run.report.notes if "unavailable" in item), "")
    return Observation(
        found=bool(declined),
        action="decline" if declined else "no_block_to_decline",
        evidence=(
            (
                f"[pressure task A] declared sources={len(document.contract.sources)}, "
                f"no input file for {dead} (the task's ⑨ design)"
            ),
            f"the run plans {len(run.blocks)} block(s) and drops the dead source before splitting: {note!r}",
            f"exits seen on the blocks: {sorted(exit_ for exit_ in exited if exit_)}",
            "★ 没有「不可能的块」→ decline 这条出口在压力题上走不到（真值侧只保证不许编 ENT-12）",
        ),
    )


#: 压力题上【真的读题】的那几条。其余六条是构造夹具，换题读数必然相同。
PRESSURE_READINGS: Mapping[str, Any] = {
    "A01-mesh-dependency": a01_on_pressure,
    "A03-real-conflict": a03_on_pressure,
    "A06-persistent": a06_on_pressure,
    "A08-global-invariant": a08_on_pressure,
    "A09-impossible": a09_on_pressure,
}


def injectors_for(target: str = "main") -> Mapping[str, Any]:
    """两套读数：`main` = 主验题（`INJECTORS` 原样），`stress` = 压力任务 A 的变体。

    ★ 不读题的六条在 `stress` 下与 `main` 共用同一个注入器 —— 这是【事实】，
    不是省略：夹具注入器与题无关，读数当然一样。
    """
    if target == "stress":
        return {**INJECTORS, **PRESSURE_READINGS}
    return INJECTORS
