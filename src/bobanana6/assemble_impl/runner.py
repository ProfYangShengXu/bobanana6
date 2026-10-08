"""The end-to-end pipeline: contract -> ontology -> gates -> blocks -> assembly -> reports.

This is where the pieces are *ordered*, and the order is itself a judgment the
design makes (``实现规格 §10``, ``架构设计 §8``):

  1. the ontology is frozen **before** any worker exists, because a moving
     semantics layer is what makes two blocks disagree;
  2. four gates run **before** the cluster starts -- ontology self-consistency,
     the split legality checks, the constraint-bundle consistency checks, and
     the fetch of every declared source. ``实现规格 §8`` is explicit that any hit
     refuses the run: starting a cluster on a plan that cannot be assembled only
     burns the budget;
  3. a source that fails to fetch is a *named* failure, never an empty snapshot;
  4. per block: run -> judge -> converge; then assembly over the produced pieces;
  5. if anything failed, attribute it to a layer and turn the blame into actions
     (``AGENTS §6``: a conclusion without an action is not a conclusion worth
     printing).

Nothing in this module decides whether a block is good -- it calls
``core/kernel.py`` and reports what came back.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..agentloop.attribution import (
    BLAME_ACTIONS,
    Attribution,
    Experiment,
    actions_for,
    attribute,
)
from ..agentloop.events import EventLog
from ..agentloop.metrics import tool_metrics
from ..contract import ContractDocument
from ..core.assemble import AssemblyReport, check_assembly
from ..core.capabilities import BlockSpec, Capability, WorkerCapabilities
from ..core.checkers.base import CheckContext
from ..core.consist import ConsistencyReport, check_consistency
from ..core.kernel import compare_across, converge
from ..core.model import (
    DEFAULT_LIMITS,
    Artifact,
    AssemblyOutcome,
    Budget,
    Candidate,
    Classification,
    ConstraintBundle,
    ConvergeDecision,
    Convergence,
    DeliverableSpec,
    Limits,
    Ontology,
    Provenance,
    Recon,
    Snapshot,
    SnapshotBody,
    SourceRef,
    SplitPlan,
    Subtask,
    TaskContract,
    Verdict,
)
from ..core.ontology import validate_ontology
from ..core.ports import DecomposerPort, OntologyPort, SnapshotReader, SourcePort
from ..core.split import SplitReport, check_split
from ..report import ReportBundle, build_reports
from ..services import (
    TOOL_NAMES,
    BoundChecks,
    artifact_payload,
    build_toolset,
    judge_environment_problems,
)
from ..services.artifacts import ArtifactStore, BlockRecord
from ..services.ci import invariant_findings, truncation_findings
from ..sources import SnapshotStore, SourceError
from .produce import artifact_from_disk
from .runtime import (
    BlockDeclined,
    BlockRuntime,
    BlockRuntimeError,
    ContractConstraintSource,
    block_bundle,
)

__all__ = ["RunRefused", "RunResult", "Runner"]

#: Severity order used when several blocks converge differently. The aggregate
#: reports the *most demanding* decision, because reporting DELIVER while one
#: block still needs arbitration would be the one lie that matters.
_DECISION_ORDER: tuple[ConvergeDecision, ...] = (
    ConvergeDecision.DELIVER,
    ConvergeDecision.REFINE_SPLIT,
    ConvergeDecision.NORMALIZE_DEFINITION,
    ConvergeDecision.RECORD_CONFLICT,
    ConvergeDecision.ARBITRATE,
)


class RunRefused(RuntimeError):
    """A pre-cluster gate refused the run. ``codes`` names the exact rule(s)."""

    def __init__(self, gate: str, detail: str, codes: tuple[str, ...] = ()) -> None:
        super().__init__(f"{gate}: {detail}")
        self.gate = gate
        self.detail = detail
        self.codes = codes


@dataclass(frozen=True)
class RunResult:
    task_id: str
    ontology: Ontology
    plan: SplitPlan
    bundle: ConstraintBundle
    recon: Recon
    snapshots: Mapping[str, Snapshot]
    candidates: Mapping[str, Candidate]
    verdicts: Mapping[str, Verdict]
    convergence: Convergence
    assembly: AssemblyReport
    split_report: SplitReport
    consistency: ConsistencyReport
    ontology_codes: tuple[str, ...]
    attribution: Attribution | None
    attribution_actions: tuple[str, ...]
    metrics: Mapping[str, Any]
    outcome: AssemblyOutcome
    exit_reason: str
    #: ★ A06: sources that could not be fetched. The run continued without them and
    #: this is where that is written down -- a degraded run has to say so.
    fetch_failures: Mapping[str, str] = field(default_factory=dict)
    reports: ReportBundle | None = None

    @property
    def ok(self) -> bool:
        return self.outcome.passed


@dataclass
class Runner:
    document: ContractDocument
    ontology_port: OntologyPort
    decomposer: DecomposerPort
    constraint_source: ContractConstraintSource
    runtime: BlockRuntime
    fetcher: SourcePort
    store: SnapshotStore
    #: ISO-8601 "now", injected because ``freshness`` never reads a clock.
    now: str
    #: Monotonic seconds for evidence timestamps, injected for the same reason.
    epoch: Callable[[], float]
    #: ★ `#33`: the workspace the blocks write code into (``None`` = none declared).
    #: The frozen-vs-workspace comparison happens outside; core only compares digests.
    workspace: Path | None = None
    #: ★ A11: whoever turns a frozen carrier into rows core may judge. Injected at
    #: the assembly point; ``None`` means the content check cannot run and the
    #: chain honestly degrades to the name check.
    reader: SnapshotReader | None = None
    limits: Limits = DEFAULT_LIMITS
    budget: Budget = field(default_factory=lambda: Budget(max_calls=64, max_wall_s=3600, max_output=8192))  # blame: fail-open — 与 runtime.py 的同一份默认**重复出现**⇒两处会漂移；该收成单一来源后由它派生，不是各自写一遍
    #: The capabilities the deployment declares. ``None`` means "nothing was
    #: declared", and then capability matching is not evaluated at all rather
    #: than being reported as a gap (an undeclared pool is not a missing pool).
    worker_pool: tuple[WorkerCapabilities, ...] | None = None
    event_log: EventLog = field(default_factory=EventLog)
    report_dir: Path | None = None
    exit_reason: str = "unknown"
    fetch_failures: dict[str, str] = field(default_factory=dict)
    #: ★★ 痕迹层（规格 §2 第1层）：每个块跑完 = 一个 commit。``None`` = 这个部署没有
    #: 工作区（或显式关掉）—— 那就没有可提交的文件，追溯也随之关闭（如实，不假装）。
    #: 默认由 `workspace` 派生（`<workspace>.parent/artifacts`），见 `_artifact_store`。
    artifacts: ArtifactStore | None = None

    # -- the pipeline ------------------------------------------------------ #

    def run(self) -> RunResult:
        contract = self.document.contract
        survey_note = f"{len(contract.sources)} declared sources"
        from ..core.model import InputSurvey

        survey = InputSurvey(source_refs=tuple(source.ref for source in contract.sources), notes=survey_note)

        # 1. semantics first, frozen before any worker exists.
        ontology = self.ontology_port.obtain(contract, survey)
        ontology = ontology.freeze()
        ontology_report = validate_ontology(ontology)
        ontology_codes = ontology_report.codes()
        if ontology_codes:
            raise RunRefused(
                "ontology",
                "the shared semantics are not self-consistent; refusing to start workers",
                ontology_codes,
            )

        # 2. freeze every source into a snapshot (or fail by name).
        snapshots = self._fetch_sources(contract)

        # 3. plan, bundle, and the gates that keep a bad plan from being shipped.
        recon = Recon(
            evidence=f"fetched {len(snapshots)} of {len(contract.sources)} declared sources",
            source_ids=tuple(sorted(snapshots)),
            findings=tuple(f"{ref}: {snapshot.source_kind}" for ref, snapshot in sorted(snapshots.items())),
        )
        plan = self.decomposer.split(contract, ontology, recon)
        bundle = self.constraint_source.compile(contract, plan, ontology)
        split_report = check_split(plan, contract, bundle, ontology, frozen_hash=ontology.content_hash(), limits=self.limits)
        if not split_report.ok:
            raise RunRefused("split", "the plan does not pass the legality checks", split_report.codes())
        consistency = check_consistency(
            plan,
            contract,
            bundle,
            ontology,
            budget=self.budget,
            # ★ The capability inventory is the tool surface this deployment can
            #   actually serve; without it the check refuses to guess.
            available_capabilities=TOOL_NAMES,
            limits=self.limits,
            frozen_hash=ontology.content_hash(),
        )
        if not consistency.start_allowed:
            raise RunRefused("consist", "the plan and its bundle are not self-consistent", consistency.codes())

        # ★ `#44`: 第四道门 —— 判据命令必须能在**这个环境**里跑起来。跑不起来（解释器/包/
        #   `cd` 目标缺失）⇒ 具名拒 + **0 次模型调用**，而不是跑完整轮之后把它记成"判据太严"
        #   （`壳问题三连-首轮AB暴露-20261005.md` §2）。只查环境，不碰命令的输入文件。
        judge_problems = judge_environment_problems(bundle.hard + bundle.soft, cwd=Path.cwd())
        if judge_problems:
            detail = "; ".join(f"{code}: {problem}" for code, problem in judge_problems)
            raise RunRefused(
                "judge",
                f"a judge command cannot run in this environment -- {detail}",
                tuple(code for code, _problem in judge_problems),
            )

        # 4. run the blocks.
        context = CheckContext(
            ontology=ontology,
            snapshots=snapshots,
            snapshot_bodies=self._snapshot_bodies(snapshots),
            frozen_files=self._frozen_files(snapshots),
            unmapped_fields=bundle.unmapped_fields,
            # ★ 字段声明里的 source{ref,locator} 由契约解析，在这里进判定上下文；
            #   判定器今天不读它（改语义不在本单元），但通道是接上的、可断言的。
            declared_field_sources=contract.declared_field_sources,
            now=self.now,
            limits=self.limits,
            custom_checks=self._custom_checks(),
        )
        self.runtime.context = context
        pieces: dict[str, Any] = {}
        #: ★★ 交接件：产物之外，把"上游写过哪些文件 + 它写给接手人的说明"也带下去。
        #: 键与 `pieces` 同源（交付物路径），所以下游按 `subtask.inputs` 找得到。
        handoffs: dict[str, dict[str, Any]] = {}
        piece_provenance: dict[str, tuple[Provenance, ...]] = {}
        candidates: dict[str, Candidate] = {}
        verdicts: dict[str, Verdict] = {}
        decisions: list[Convergence] = []
        #: 交付物路径 -> 负责产出它的块 id：输入缺失时要能追到"降级起点"。
        producer_of = {block.output: block.id for block in plan.subtasks}
        #: 交付物路径 -> 产出它的 commit（痕迹层）。下游的溯源基准就是它。
        commits: dict[str, str] = {}
        for subtask in plan.subtasks:
            #: ★★ 输入缺失**不再短路**（旧前置门：一件没到就 0 次调用、记 inputs_unavailable）。
            #:   实测代价（13f，8 块）：T-01 的答案信封不合法 ⇒ T-02..T-08 **全部**停工，
            #:   八个块实际只跑过 1 个。第一道工序填错工单，后面七道陪葬。
            #:   新约束：**照样开工，但不许编** —— 缺哪几份、谁答应的，作为已知事实带进
            #:   这一块的 brief（`produce.py::_input_gaps`）与证据（下面的 `input_gaps`）；
            #:   它自己的成败由它自己的产物决定，不再因上游失败被判死。
            #:   ★ E-0 反例（拿不到输入就按名字编一张表）仍由**溯源层**兜底：
            #:   编出来的行追不到真来源，会被拒（见 `produce.py` 的继承守卫）。
            gaps = self._input_gaps(subtask, pieces, producer_of)
            available = [name for name in subtask.inputs if name in pieces]
            if subtask.inputs and not available:
                # ★★ 旧前置门在这个条件下 `continue`：一件输入都没到就**不许开工**
                #    （0 次模型调用，记 `inputs_unavailable`）。实测代价（13f，8 块）：
                #    T-01 的答案信封不合法 ⇒ T-02..T-08 全部停工，八个块实际只跑过 1 个。
                #    现在**不 continue**：照样开工，只把"声明的输入一件都没到"作为降级起点
                #    记下来（`gaps` 照常带进 brief 与证据），成败由它自己的产物决定。
                all_inputs_missing = True
            else:
                all_inputs_missing = False
            gap_note = self._gap_note(gaps, all_inputs_missing=all_inputs_missing)
            inherited = tuple(
                sorted({p.snapshot_ref for name in subtask.inputs for p in piece_provenance.get(name, ())})
            )
            toolset = self._toolset(
                plan, subtask, bundle, ontology, snapshots, pieces, piece_provenance, inherited, context,
                handoffs,
            )
            try:
                candidate = self.runtime.run(
                    subtask,
                    bundle,
                    ontology,
                    toolset,
                    self.budget,
                    # ★★ 规格 ①：静态知识归位要按【全 DAG】算恒定键，
                    #   所以把整份计划交下去（不是「跑到哪算到哪」）。
                    dag_subtasks=tuple(plan.subtasks),
                )
            except BlockDeclined as declined:
                declined_detail: dict[str, Any] = {"exit": declined.exit_reason, "declined": True}
                declined_evidence: dict[str, str] = {"exit": declined.exit_reason}
                if gaps:
                    declined_detail["input_gaps"] = self._gap_event(gaps)
                    declined_evidence["input_gaps"] = gap_note
                # ★★ 判定失败 ≠ 产物不存在：它可能只是信封没封好，活早就干完了。
                published = self._publish_disk_artifact(
                    subtask, pieces, handoffs, commits=commits, verdict="declined"
                )
                if published is not None:
                    declined_evidence["artifact_on_disk"] = subtask.output
                    declined_detail["artifact_on_disk"] = subtask.output
                else:
                    # ★ 盘上没有产物，但这个块**跑过** —— 照样留一个 commit（规格 §2 第1层）。
                    self._trace_finished_block(
                        subtask,
                        verdict="declined",
                        summary=f"declined: {declined.exit_reason}",
                        commits=commits,
                    )
                self._note_gate(subtask, False, declined_detail)
                verdicts[subtask.id] = Verdict(
                    ok=False, failed=("declined",), evidence=declined_evidence
                )
                continue
            except BlockRuntimeError as failed:
                # ★ §7⑫: a withheld candidate was rejected by the *judge*, and the
                #   judge still holds which constraints did it -- that is the
                #   declaration, recorded here on the real event stream.
                failure_evidence = self._failure_evidence(failed)
                failure_detail: dict[str, Any] = {"exit": failed.exit_reason}
                if failure_evidence.get("rejected_by"):
                    failure_detail["constraint_rejected"] = True
                if failure_evidence.get("cause"):
                    failure_detail["cause"] = failure_evidence["cause"]
                if gaps:
                    # ★ 降级起点不能丢：这一块是在"缺输入"的前提下失败/成功的，报告要能追到。
                    failure_detail["input_gaps"] = self._gap_event(gaps)
                    failure_evidence["input_gaps"] = gap_note
                # ★★ 判定失败 ≠ 产物不存在（见 `_publish_disk_artifact`）：产物照样给下游，
                #   但 `verdicts` 仍是 ok=False —— 两件事独立，绝不因为它在盘上就改成通过。
                published = self._publish_disk_artifact(
                    subtask, pieces, handoffs, commits=commits, verdict="failed"
                )
                if published is not None:
                    failure_evidence["artifact_on_disk"] = subtask.output
                    failure_detail["artifact_on_disk"] = subtask.output
                else:
                    # ★ 盘上没有产物，但这个块**跑过** —— 照样留一个 commit（规格 §2 第1层）。
                    rejected = failure_evidence.get("rejected_by")
                    self._trace_finished_block(
                        subtask,
                        verdict="failed",
                        summary=f"failed: {failed.exit_reason}"
                        + (f"; rejected_by={'/'.join(str(one) for one in rejected)}" if rejected else ""),
                        commits=commits,
                    )
                self._note_gate(subtask, False, failure_detail)
                # ★ 同一个 `"runtime"` 标签的第二半：这里**多半是"候选被判定器拒收"**
                #   （`exit` 是 `degrade_single_agent` / `fix_constraint` 之类），
                #   真因在 `evidence["rejected_by"]` 与具名门里 —— 不是进程崩了。
                #   ★ 实测代价：24 源压力题上修前 1 块、修后 15–16 块都带这个标签，
                #   只看标签会把"读块各自撞字段规则"误读成"运行期不稳定"。
                verdicts[subtask.id] = Verdict(
                    ok=False,
                    failed=("runtime",),
                    evidence=failure_evidence,
                    cause=failure_evidence.get("cause", ""),
                )
                continue
            candidates[subtask.id] = candidate
            piece_provenance[subtask.output] = candidate.provenance
            judge = self.runtime.last_judge
            verdict = judge.last_verdict if judge is not None and judge.last_verdict is not None else judge.admit(candidate)  # type: ignore[union-attr]
            if gaps:
                # ★★ 它自己的产物决定了成败，但"这一条是从缺输入那一步开始降级的"必须留下痕迹 ——
                #   否则报告看起来一切正常（`exit` 里没有门了，`input_gaps` 就是那条痕迹）。
                verdict = replace(verdict, evidence={**verdict.evidence, "input_gaps": gap_note})
            # ★★ 改动 A ③：**契约声明的**对账关系（`content_checks.invariants`）重算一遍。
            #   判据来自声明，不硬编码（"权重和=1"这种规则必须由契约写出来）。
            #   ★ 分档：不一致 ⇒ **块级拒收**（verdict 记 failed + 具名理由），
            #   **绝不升格**成整跑失败；产物照旧进 pieces（既有"失败≠不存在"的规矩）。
            #   （`artifact` 要到下面才赋值 ⇒ 这里直接取，避免顺序耦合）
            content_failures, content_notices = self._content_judgements(
                subtask, candidate.artifact(subtask.output), pieces
            )
            if content_failures:
                verdict = replace(
                    verdict,
                    ok=False,
                    failed=(*verdict.failed, *(one["who"] for one in content_failures)),
                    evidence={
                        **verdict.evidence,
                        **{str(one["who"]): f"{one['violated']} (expected: {one['expected']}; got: {one['got']})" for one in content_failures},
                    },
                )
            if content_notices:
                # ★ 第 0 档：**只记录不拒**（截短判据跑够一轮再谈升档）。
                verdict = replace(
                    verdict,
                    evidence={**verdict.evidence, "content_notices": [dict(one) for one in content_notices]},
                )
            verdicts[subtask.id] = verdict
            # ★ judged against this block's slice, not the whole plan's bundle
            decisions.append(converge(block_bundle(bundle, subtask, ontology), (candidate,), context))
            artifact = candidate.artifact(subtask.output)
            if artifact is not None:
                pieces[subtask.output] = artifact
            # ★★ 痕迹层：这个块跑完 = 一个 commit（作者 = 块 id，信息 = summary + verdict +
            #   它用的输入）。判定失败也提交 —— 「产物在不在」与「谁通过了」是两件事。
            trace_paths = sorted(
                {str(name) for name in candidate.files} | ({subtask.output} if artifact is not None else set())
            )
            record = self._commit_block(
                subtask,
                verdict="ok" if verdict.ok else "failed",
                paths=trace_paths,
                summary=candidate.summary,
                commits=commits,
            )
            if artifact is not None and record is not None:
                # ★ 输出路径 -> 产出它的 commit：下游按它溯源（即使这份产物是行数据、
                #   磁盘上没有对应文件——"哪个块、什么时候产出的"仍然有确定答案）。
                commits[subtask.output] = record.commit
            if artifact is not None:
                # ★★ 交接件（`#66`）：产物之外，把上游**写过哪些文件**（客观，来自
                #   `candidate.files`）与**它写给接手人的说明**（自述，标注未判定）一起带上。
                #   ★ 再加产出它的 commit —— 下游溯源到"哪块、什么时候、用什么输入产出的"。
                handoffs[subtask.output] = {
                    "from": subtask.id,
                    "files": sorted(str(name) for name in candidate.files),
                    "note": candidate.summary,
                    "producer_verdict": "ok" if verdict.ok else "failed",
                    "producer_commit": record.commit if record is not None else "",
                    # ★ 规格 ②：下游要知道拿到的东西是不是【照着判分器做的】。
                    **self._cleanliness(),
                }
            # ★ §7⑫: this verdict comes from the judge, so its ``failed`` names the
            #   constraints that rejected the block -- the judge is the party that
            #   declares it. Written here (not inferred downstream) so attribution
            #   reads a declaration in a *real* run, not only in a hand-made trace.
            gate_detail: dict[str, Any] = {"failed": list(verdict.failed)}
            if gaps:
                gate_detail["input_gaps"] = self._gap_event(gaps)
            if not verdict.ok:
                gate_detail["constraint_rejected"] = True
                if verdict.cause:
                    # ★ `#44`: 检查器一致声明的原因（目前只有"判据命令自己没跑起来" ⇒ tool）
                    #   原文照录 —— 归因层读声明，不去猜字符串。
                    gate_detail["cause"] = verdict.cause
            self._note_gate(subtask, verdict.ok, gate_detail)

        # ★ `#63`: 装配关只拿 pieces —— 两个块的候选从来不会被放在一起，于是
        #   "两个权威源对同一实体给出不同值"这件事在架构上测不到。这里在块跑完之后
        #   把各块的候选按【实体键】对齐、按内核的三分法分类（口径不一致 / 真冲突），
        #   并把结果并进这一轮的分类里 —— 调用方（报告）照旧读同一个载荷。
        cross_block = compare_across(candidates.values(), context)
        if not cross_block.is_empty:
            self.event_log.record(
                kind="gate_check",
                task_id=self.document.contract.task_id,
                worker_id="runner",
                at=self.epoch(),
                ok=False,
                detail={
                    "conflicts": len(cross_block.conflicts),
                    "definition_mismatches": len(cross_block.definition_mismatches),
                    "concepts": sorted({c.concept_id for c in cross_block.conflicts}),
                },
            )
        convergence = self._aggregate(decisions, cross_block=cross_block)
        assembly = check_assembly(
            plan,
            contract,
            bundle,
            pieces,
            context,
            provenance=tuple(p for candidate in candidates.values() for p in candidate.provenance),
            snapshots=snapshots,
            limits=self.limits,
            blocks=self._block_specs(plan) if self.worker_pool is not None else None,
            workers=self.worker_pool,
        )

        outcome = AssemblyOutcome(
            passed=assembly.passed and all(v.ok for v in verdicts.values()),
            failed_blocks=tuple(sorted({block for block, verdict in verdicts.items() if not verdict.ok})),
            coverage={"blocks": f"{len(candidates)}/{len(plan.subtasks)}", "pieces": str(len(pieces))},
        )
        attribution, actions = self._attribute(outcome)
        metrics = self._metrics()

        self.exit_reason = (
            "passed"
            if outcome.passed
            else ("declined" if verdicts and all(v.failed == ("declined",) for v in verdicts.values()) else "failed")
        )
        reports = None
        if self.report_dir is not None:
            reports = build_reports(
                contract=contract,
                plan=plan,
                recon=recon,
                ontology=ontology,
                bundle=bundle,
                convergence=convergence,
                outcome=outcome,
                verdicts=verdicts,
                candidates=candidates,
                snapshots=snapshots,
                provenances=tuple(p for candidate in candidates.values() for p in candidate.provenance),
                fetch_failures=self.fetch_failures,
                metrics=metrics,
                attribution=self._attribution_view(attribution, actions),
                blocks=self._block_rows(plan) if self.worker_pool is not None else (),
                capability_matching=self._matching_view(assembly),
                exit_reason=self.exit_reason,
            )
            reports.write(self.report_dir)

        return RunResult(
            task_id=contract.task_id,
            ontology=ontology,
            plan=plan,
            bundle=bundle,
            recon=recon,
            snapshots=snapshots,
            candidates=candidates,
            verdicts=verdicts,
            convergence=convergence,
            assembly=assembly,
            split_report=split_report,
            consistency=consistency,
            ontology_codes=ontology_codes,
            attribution=attribution,
            attribution_actions=actions,
            metrics=metrics,
            outcome=outcome,
            exit_reason=self.exit_reason,
            fetch_failures=dict(self.fetch_failures),
            reports=reports,
        )

    # -- helpers ----------------------------------------------------------- #

    def _fetch_sources(self, contract: TaskContract) -> dict[str, Snapshot]:
        """Freeze every source we can, and refuse only when there is nothing left.

        ★ A06: one dead source used to refuse the whole run -- which throws away the
        sources that *did* answer. The rule is drawn where it belongs:

        * **nothing** could be fetched (or the only source is down) -> refuse at the
          ``sources`` gate. There is no task left to run.
        * **some** sources are unavailable -> continue with the rest and *mark* the
          missing ones (``fetch_failures`` -> ``SOURCES.md`` + the survey evidence).
          Refusing here would be the wrong exit: the defect is one input, not the run.

        An empty snapshot is still never manufactured -- "no data" must not read as
        "the data is empty".
        """
        snapshots: dict[str, Snapshot] = {}
        for source in contract.sources:
            try:
                snapshots[source.ref] = self.fetcher.fetch(SourceRef(ref=source.ref, locator=source.locator))
            except SourceError as exc:
                self.fetch_failures[source.ref] = str(exc)
        if not snapshots:
            # ★ The *why* travels with the refusal: "src-1 failed" sends the operator
            #   to the logs, "src-1: cannot read .../missing.csv" sends them to the file.
            detail = "; ".join(f"{ref}: {reason[:120]}" for ref, reason in sorted(self.fetch_failures.items()))
            raise RunRefused(
                "sources",
                f"no source could be fetched -- {detail or '<none declared>'}; there is no task left to run",
                ("fetch_failed",),
            )
        return snapshots

    def _custom_checks(self) -> Mapping[str, Callable[..., Any]]:
        """The ``custom`` checks this host is willing to run (see services/checks.py)."""
        from ..services import (
            SHELL_COMMAND_CHECK,
            RecordedEvidence,
            shell_command_check,
        )

        evidence = RecordedEvidence()
        return {SHELL_COMMAND_CHECK: shell_command_check(evidence, self.epoch)}

    def _frozen_files(self, snapshots: Mapping[str, Snapshot]) -> dict[str, tuple[str, str]]:
        """★ `#33`: 冻结载体 vs 工作区里那份 —— 只算两个摘要，判定在 `core`。

        只观测**契约声明了 locator 且工作区里确实存在**的那几份；没观测到的
        不写进上下文 —— `frozen_files` 判据会为它具名失败，而不是静默放过。
        """
        if self.workspace is None:
            return {}
        locators = {source.ref: source.locator for source in self.document.contract.sources}
        observed: dict[str, tuple[str, str]] = {}
        for ref, snapshot in sorted(snapshots.items()):
            locator = locators.get(ref, "")
            if not locator:
                continue
            path = self.workspace / locator
            if not path.is_file():
                continue
            observed[ref] = (
                self.store.digest(snapshot),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return observed

    def _snapshot_bodies(self, snapshots: Mapping[str, Snapshot]) -> dict[str, SnapshotBody]:
        """Read every fetched snapshot's frozen rows, once, before any judgment.

        A snapshot that cannot be read back is an error, not a reason to quietly
        fall back to the weaker name-only check. ★ It is raised as the **sources
        gate** (``RunRefused``), not as a generic failure: "failed" is the
        phenomenon, "the run was stopped at the sources gate" is the location
        (``AGENTS.md §5.2``). This is the *form* of the refusal, never its
        semantics -- without the carrier the content check does not run at all,
        so A11's hole is not reopened. Which gate stopped the run and which layer
        owns the failure are two different questions (the latter belongs to A07).
        """
        if self.reader is None:
            return {}
        bodies: dict[str, SnapshotBody] = {}
        for ref, snapshot in sorted(snapshots.items()):
            try:
                bodies[ref] = self.reader.body(snapshot)
            except (SourceError, OSError, ValueError, TypeError) as exc:
                raise RunRefused(
                    "sources",
                    f"the frozen carrier for {ref!r} cannot be read back "
                    f"({type(exc).__name__}: {exc}); the provenance check will not silently degrade to names",
                    (ref,),
                ) from exc
        return bodies

    def _toolset(
        self,
        plan: SplitPlan,
        subtask: Subtask,
        bundle: ConstraintBundle,
        ontology: Ontology,
        snapshots: Mapping[str, Snapshot],
        pieces: Mapping[str, Any],
        piece_provenance: Mapping[str, tuple[Provenance, ...]],
        inherited_sources: tuple[str, ...],
        context: CheckContext,
        handoffs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Any:
        # ★ The upstream tool hands over the *content* of the declared inputs, not
        #   a summary: a block that can only see a row count cannot merge anything.
        upstream = {}
        for name in subtask.inputs:
            artifact = pieces.get(name)
            if artifact is None:
                # ★ It names itself: "missing: true" without a name tells the caller
                #   that *something* is absent, not which input, and a consumer that
                #   cannot tell will keep asking for it (observed: the merge block
                #   burned its whole budget re-requesting an input nobody produced).
                upstream[name] = {"name": name, "schema": plan.interfaces.get(name, ""), "missing": True}
                # ★ 谁答应的这份输入（产出它的块 id）—— 下游 brief 与报告都要能追到
                #   "这一条从哪一步开始降级"（`produce.py::_input_gaps` 读它）。
                upstream[name]["producer"] = next(
                    (block.id for block in plan.subtasks if block.output == name), ""
                )
            else:
                upstream[name] = {
                    "schema": plan.interfaces.get(name, ""),
                    **artifact_payload(artifact),
                    # ★★ `#66` 交接件：上游写过哪些文件（客观）+ 它写给接手人的说明（自述）。
                    #   下游据此能直接复用上游的脚本、并沿用同一套口径，而不是从数据反推。
                    **({"handoff": dict(handoffs[name])} if handoffs and name in handoffs else {}),
                    # ★ A11: the part's own per-row citations travel with it, so the
                    #   consumer can inherit a truthful chain instead of inventing one.
                    "provenance": [
                        {
                            "value_ref": point.value_ref,
                            "snapshot_ref": point.snapshot_ref,
                            "locator": point.locator,
                            "source_row": point.source_row,
                        }
                        for point in piece_provenance.get(name, ())
                    ],
                }
        checks = BoundChecks(
            block=subtask.id,
            verdict=None,
            provenance_report=None,
            split_report=self._split_report_for(plan, bundle, ontology),
        )
        return build_toolset(
            plan=plan,
            subtask=subtask,
            contract=self.document.contract,
            ontology=ontology,
            authorized=self.document.contract.sources,
            fetched=snapshots,
            upstream=upstream,
            checks=checks,
            inherited_sources=inherited_sources,
            # ★ 题面声明（`ContractDocument.statement`）—— 每个块都拿到同一个 ref，
            #   由 `produce.py::_statement_entry` 把题面**全文**内联进它的 brief。
            statement=self.document.statement,
        )

    def _split_report_for(self, plan: SplitPlan, bundle: ConstraintBundle, ontology: Ontology) -> SplitReport:
        return check_split(plan, self.document.contract, bundle, ontology, frozen_hash=ontology.content_hash(), limits=self.limits)

    def _block_specs(self, plan: SplitPlan) -> tuple[BlockSpec, ...]:
        specs: list[BlockSpec] = []
        for subtask in plan.subtasks:
            needs = (
                frozenset({Capability.READ, Capability.COMPUTE})
                if subtask.mode == "read"
                else frozenset({Capability.COMPUTE, Capability.WRITE_DELIVERABLE})
            )
            specs.append(
                BlockSpec(
                    block_id=subtask.id,
                    dimension=subtask.axis,
                    needs=needs,
                    produces=self._deliverable_for(subtask.output),
                    partition=subtask.partition,
                )
            )
        return tuple(specs)

    def _deliverable_for(self, name: str) -> DeliverableSpec:

        for deliverable in self.document.contract.deliverables:
            if deliverable.name == name:
                return deliverable
        return DeliverableSpec(name=name)

    def _publish_disk_artifact(
        self,
        subtask: Subtask,
        pieces: dict[str, Any],
        handoffs: dict[str, dict[str, Any]],
        *,
        commits: dict[str, str],
        verdict: str = "failed",
        summary: str = "",
    ) -> Artifact | None:
        """Hand a failed block's on-disk deliverable to downstream anyway.

        ★★ 根因：壳原来把「块判定失败」和「产物不存在」当成一回事。它们独立 ——
        块可能只是**信封没封好**（散文开头、格式不对），它干出来的活早写到磁盘上了。
        13f 实测：T-03..T-06 四个块只是交答案时写了句散文，四份产物却都被下游当成
        `missing`，逼得 T-08 自己绕过 brief 去磁盘上找。

        边界（这条最要紧）：`pieces` 管「下游拿得到什么」，`verdicts` 管「谁通过了」。
        这里**只动** `pieces` / `handoffs`，调用方的 `verdicts[...]` 始终是 ok=False。

        ★ 不许静默降级：交接件里写明 `from_disk: true` + `producer_verdict`，并把产出
        它的那个 **commit** 一起带上（规格 §2 第1层）—— 下游 brief 与最终报告都看得见。
        """
        if self.workspace is None:
            return None
        path = Path(self.workspace) / subtask.output
        if not path.is_file():
            return None
        artifact = artifact_from_disk(subtask.output, path)
        pieces[subtask.output] = artifact
        # ★ 产物落进 git：即使这个块判定失败，它写过的文件也留一个 commit（痕迹）。
        record = self._commit_block(subtask, verdict=verdict, paths=[subtask.output], summary=summary)
        if record is not None:
            commits[subtask.output] = record.commit
        handoffs[subtask.output] = {
            "from": subtask.id,
            "files": [],
            "note": "",
            "from_disk": True,
            "producer_verdict": verdict,
            "producer_commit": record.commit if record is not None else "",
            # ★ 块失败也照记 cleanliness（产物的属性 ≠ 块的成功/失败）
            **self._cleanliness(),
        }
        return artifact

    # -- the trace layer (one block = one commit) --------------------------- #

    def _trace_finished_block(
        self,
        subtask: Subtask,
        *,
        verdict: str,
        summary: str,
        commits: Mapping[str, str] | None = None,
    ) -> BlockRecord | None:
        """★ 规格 §2 第1层：**每个块跑完 = 一个 commit** —— 包括没在盘上留文件的失败块。

        `_publish_disk_artifact` 只在磁盘上**真有**产物时才提交。纯行数据的块
        （主验题的读块、13f 的 T-03..T-06）失败时，磁盘上没有对应文件，
        于是**一个 commit 都没有** —— "哪块、什么时候、用什么输入、什么结果"在 git 里查不到。
        实测（`panel-three-sources` 离线跑）：四个块全失败 ⇒ `_run/artifacts/tree` **根本不存在**。

        这里兜底：产物不在盘上也提交。`commit.json` 带 `block / verdict / inputs / summary`，
        所以"这个块跑过、结果是失败"这件事本身留痕；`files` 为空是事实（它真没写出文件）。
        """
        return self._commit_block(
            subtask, verdict=verdict, paths=[subtask.output], summary=summary, commits=commits
        )

    def _content_judgements(
        self, subtask: Subtask, artifact: Artifact | None, pieces: Mapping[str, Any]
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        """★ 改动 A：把"产物打开能不能正常用"变成**确定性判据**。

        ① 分隔符：在 `produce.py`（两个通道都过）—— 那里才有原始正文
        ② 数值截短：**只记 notice**（第 0 档起步）
        ③ 契约声明的对账关系：在这里重算 —— 只有 runner 手上有 `ContractDocument`
        （`content_checks` 挂在契约文档上，不在 `TaskContract` 上）

        返回 `(violations, notices)`；两者都**只影响这一个块**，不影响整跑。
        """
        checks = getattr(self.document, "content_checks", None)
        # ★★ 规格 三①②（fail-closed，上一轮没落实）：**没声明 content_checks ⇒ 具名拒**，
        #   不许静默跳过 —— 第 ④ 格的内容层设计里就写着这一条。
        #   ★ 注意处置：这是**规格缺口**，不是这个块的错 ⇒ 记一条**具名拒绝**进证据，
        #   **不让块失败**（守卫纪律：拒了要说清，但别把没做成的判死）。
        if checks is None or not getattr(checks, "declared", False):
            self.event_log.record(
                kind="gate_check",
                task_id=self.document.contract.task_id,
                worker_id=subtask.id,
                at=self.epoch(),
                ok=True,
                detail={
                    "content_checks_not_declared": (
                        "the contract declares no content_checks, so the result-level judgements "
                        "(delimiter is always on; invariants need a declaration) never ran. "
                        "Silence is not 'none' -- declare `content_checks` (see the spec extractor)"
                    )
                },
            )
        invariants = tuple(getattr(checks, "invariants", ()) or ())
        payload = {
            str(name): {
                "columns": [{"name": column.name, "dtype": column.dtype} for column in piece.columns],
                "rows": [dict(row) for row in piece.rows],
            }
            for name, piece in pieces.items()
            if isinstance(piece, Artifact)
        }
        if artifact is not None:
            payload.setdefault(
                artifact.name,
                {
                    "columns": [{"name": c.name, "dtype": c.dtype} for c in artifact.columns],
                    "rows": [dict(row) for row in artifact.rows],
                },
            )
        violations: tuple[dict[str, Any], ...] = ()
        if invariants:
            violations = tuple(
                entry for entry in invariant_findings(invariants, payload) if not entry.get("ok")
            )
        notices = truncation_findings(payload)
        return violations, notices

    def _cleanliness(self) -> dict[str, Any]:
        """★ 规格 ②：本块结束时算出的 cleanliness（干净 / 污染）。

        它是**产物的属性**，不是块的成功/失败 —— 所以 `_publish_disk_artifact` 那条
        "判定失败但产物在盘上"的路径也照记（两件事不许混）。
        它**不参与判定**：不拒收产物、不让块失败、不停跑，只标读数的可信度。
        """
        recorded = getattr(self.runtime, "last_cleanliness", None)
        if not isinstance(recorded, dict) or not recorded:
            # ★ 缺省不是"没扫"，是"这次跑没有可扫的读类调用"—— 仍然给出可判的形状。
            return {"cleanliness": "clean", "cleanliness_evidence": [], "cleanliness_scanned": 0}
        return {
            "cleanliness": str(recorded.get("cleanliness", "clean")),
            "cleanliness_evidence": list(recorded.get("cleanliness_evidence") or ()),
            "cleanliness_scanned": int(recorded.get("cleanliness_scanned", 0)),
        }

    def _artifact_store(self) -> ArtifactStore | None:
        """The run's git-backed trace store, derived from the workspace.

        ``<workspace>.parent/artifacts`` —— 放在工作区**外面**：工作区里的东西会被块自己的
        ledger 当成"改动"，痕迹仓库不能混进去。
        """
        if self.artifacts is not None:
            return self.artifacts
        if self.workspace is None:
            return None
        root = Path(self.workspace).parent / "artifacts"
        self.artifacts = ArtifactStore(root, source_root=Path(self.workspace))
        return self.artifacts

    def _commit_block(
        self,
        subtask: Subtask,
        *,
        verdict: str,
        paths: Sequence[str],
        summary: str = "",
        commits: Mapping[str, str] | None = None,
    ) -> BlockRecord | None:
        """Commit what this block produced; inputs are named **with their producing commit**."""
        store = self._artifact_store()
        if store is None:
            return None
        inputs = tuple(
            (name, (commits or {}).get(name, "")) for name in getattr(subtask, "inputs", ()) or ()
        )
        try:
            record = store.commit_block(
                block_id=subtask.id,
                verdict=verdict,
                paths=paths,
                summary=summary,
                inputs=inputs,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # 痕迹不能把业务跑挂
            self.event_log.record(
                kind="gate_check",
                task_id=self.document.contract.task_id,
                worker_id=subtask.id,
                at=self.epoch(),
                ok=False,
                detail={"trace_unavailable": f"{type(exc).__name__}: {exc}"},
            )
            return None
        return record

    @staticmethod
    def _input_gaps(
        subtask: Subtask, pieces: Mapping[str, Any], producer_of: Mapping[str, str]
    ) -> tuple[tuple[str, str], ...]:
        """(declared input that did not arrive, the block that owed it) pairs.

        Order follows ``subtask.inputs`` so the report reads like the plan.
        """
        return tuple(
            (name, producer_of.get(name, "")) for name in subtask.inputs if name not in pieces
        )

    @staticmethod
    def _gap_note(gaps: tuple[tuple[str, str], ...], *, all_inputs_missing: bool = False) -> str:
        """The gap as one human-readable string for a verdict's evidence (``str -> str``)."""
        detail = "; ".join(f"{name} (produced by {producer or 'unknown'})" for name, producer in gaps)
        if all_inputs_missing:
            detail += " [every declared input is absent]"
        return detail

    @staticmethod
    def _gap_event(gaps: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
        """The gap as structured event detail (kept structured so a reader can filter it)."""
        return [{"input": name, "producer": producer} for name, producer in gaps]

    def _failure_evidence(self, failed: BlockRuntimeError) -> dict[str, str]:
        """Why the block produced nothing -- including a check that rejected it.

        ``exit=degrade_single_agent`` with no further detail answers "it failed"
        and stops there, which is the phenomenon layer (``AGENTS §5.2``). A
        withheld candidate was rejected by a *named* constraint, and the judge still
        holds that verdict, so the report can say which rule and on what facts.
        """
        evidence = {"exit": failed.exit_reason, "errors": "; ".join(failed.errors)}
        judge = self.runtime.last_judge
        withheld = judge.last_verdict if judge is not None else None
        if withheld is not None and not withheld.ok:
            evidence["rejected_by"] = ", ".join(withheld.failed)
            if withheld.cause:
                # ★ `#44`: 检查器一致声明的原因（例如"判据命令自己没跑起来" ⇒ tool）。
                #   放进证据里，报告与归因都能看见，而不是只看到一个"runtime"标签。
                evidence["cause"] = withheld.cause
            for constraint_id, fact in sorted(withheld.evidence.items()):
                if constraint_id in withheld.failed:
                    evidence[constraint_id] = fact
        return evidence

    def _block_rows(self, plan: SplitPlan) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            {
                "block": spec.block_id,
                "dimension": spec.dimension,
                "needs": sorted(capability.value for capability in spec.needs),
                "partition": spec.partition,
            }
            for spec in self._block_specs(plan)
        )

    @staticmethod
    def _matching_view(assembly: AssemblyReport) -> Mapping[str, Any]:
        """The §6.8 needs<->provides outcome, or an explicit "not evaluated"."""
        matched = assembly.matched
        if matched is None:
            return {"evaluated": False, "reason": "no worker pool was declared"}
        return {
            "evaluated": True,
            "assignments": [
                {"block": a.block_id, "worker": a.worker_id, "surplus": [c.value for c in a.surplus]}
                for a in matched.assignments
            ],
            "issues": [{"issue": str(i.issue), "block": i.block_id, "detail": i.detail} for i in matched.issues],
        }

    def _aggregate(self, decisions: list[Convergence], *, cross_block: Classification | None = None) -> Convergence:
        if not decisions:
            return Convergence(decision=ConvergeDecision.ARBITRATE, note="no block produced a candidate")
        worst = max(decisions, key=lambda c: _DECISION_ORDER.index(c.decision))
        merged = Classification(
            blind_spots=tuple(item for c in decisions for item in c.classification.blind_spots),
            conflicts=tuple(item for c in decisions for item in c.classification.conflicts)
            + tuple(cross_block.conflicts if cross_block is not None else ()),
            definition_mismatches=tuple(item for c in decisions for item in c.classification.definition_mismatches)
            + tuple(cross_block.definition_mismatches if cross_block is not None else ()),
        )
        # ★ 真冲突的处置是【记录】（`ConvergeDecision.RECORD_CONFLICT`），不是"当无事发生"：
        #   它只在比当前最坏决策更靠后时抬上去，绝不把更重的决策降下来。
        decision = worst.decision
        if cross_block is not None and cross_block.conflicts:
            recorded = ConvergeDecision.RECORD_CONFLICT
            if _DECISION_ORDER.index(recorded) > _DECISION_ORDER.index(decision):
                decision = recorded
        return Convergence(
            decision=decision,
            verdicts=tuple(v for c in decisions for v in c.verdicts),
            classification=merged,
            note=worst.note,
        )

    def _attribute(self, outcome: AssemblyOutcome) -> tuple[Attribution | None, tuple[str, ...]]:
        if outcome.passed:
            return None, ()
        failure = f"assembly failed on {len(outcome.failed_blocks)} block(s)"
        result = attribute(failure, self.event_log.events, experiment=Experiment.NOT_RUN)
        self.event_log.record(
            kind="attribute",
            task_id=self.document.contract.task_id,
            worker_id="runner",
            at=self.epoch(),
            ok=result.blame is not None,
            detail={"blame": result.blame, "confidence": result.confidence},
        )
        actions = actions_for(result.blame) if result.blame is not None else ()
        return result, tuple(actions)

    def _attribution_view(self, attribution: Attribution | None, actions: tuple[str, ...]) -> Mapping[str, Any] | None:
        if attribution is None:
            return None
        return {
            "failure": attribution.failure,
            "blame": attribution.blame,
            "confidence": attribution.confidence,
            "experiment": attribution.experiment,
            "actions": list(actions),
            "evidence": [f"{event.kind}({event.worker_id})" for event in attribution.evidence],
        }

    def _metrics(self) -> Mapping[str, Any]:
        tool = tool_metrics(self.event_log.events)
        return {
            "tool.calls": tool.calls,
            "tool.call_accuracy": tool.call_accuracy,
            "tool.argument_correctness": tool.argument_correctness,
            "tool.task_success_rate": tool.task_success_rate,
            "tool.failure_recovery_rate": tool.failure_recovery_rate,
            # ★ No delivery number: it needs labelled outcomes, and none were
            #   supplied. 0/0 would print as 0.0 and read as "nothing worked".
            "delivery": "not evaluated (no labelled outcomes were supplied)",
            "blame_actions": {str(blame): list(actions) for blame, actions in BLAME_ACTIONS.items()},
        }

    def _note_gate(self, subtask: Subtask, ok: bool, detail: Mapping[str, Any]) -> None:
        """Record one block's outcome on the structured event stream.

        ``gate_check`` is the only kind that fits a block outcome: the event
        vocabulary is fixed by ``agentloop/events.py`` and inventing a new kind
        would be a change to the runtime, not to the wiring.
        """
        self.event_log.record(
            kind="gate_check",
            task_id=self.document.contract.task_id,
            worker_id=subtask.id,
            at=self.epoch(),
            ok=ok,
            detail=dict(detail),
        )
