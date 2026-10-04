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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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
from ..services import TOOL_NAMES, BoundChecks, artifact_payload, build_toolset
from ..sources import SnapshotStore, SourceError
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
    #: ★ A11: whoever turns a frozen carrier into rows core may judge. Injected at
    #: the assembly point; ``None`` means the content check cannot run and the
    #: chain honestly degrades to the name check.
    reader: SnapshotReader | None = None
    limits: Limits = DEFAULT_LIMITS
    budget: Budget = field(default_factory=lambda: Budget(max_calls=64, max_wall_s=3600, max_output=8192))
    #: The capabilities the deployment declares. ``None`` means "nothing was
    #: declared", and then capability matching is not evaluated at all rather
    #: than being reported as a gap (an undeclared pool is not a missing pool).
    worker_pool: tuple[WorkerCapabilities, ...] | None = None
    event_log: EventLog = field(default_factory=EventLog)
    report_dir: Path | None = None
    exit_reason: str = "unknown"
    fetch_failures: dict[str, str] = field(default_factory=dict)

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

        # 4. run the blocks.
        context = CheckContext(
            ontology=ontology,
            snapshots=snapshots,
            snapshot_bodies=self._snapshot_bodies(snapshots),
            unmapped_fields=bundle.unmapped_fields,
            now=self.now,
            limits=self.limits,
            custom_checks=self._custom_checks(),
        )
        self.runtime.context = context
        pieces: dict[str, Any] = {}
        piece_provenance: dict[str, tuple[Provenance, ...]] = {}
        candidates: dict[str, Candidate] = {}
        verdicts: dict[str, Verdict] = {}
        decisions: list[Convergence] = []
        for subtask in plan.subtasks:
            available = [name for name in subtask.inputs if name in pieces]
            if subtask.inputs and not available:
                # ★ ①(c) 前置门：计划知道它要哪些输入，一件都没到就【不许开工】。
                #   这是「输入取不回来」（生产它的块失败了），不是「它没去取」——
                #   不能靠模型自觉发现、更不能让它凭名字编一张表出来（E-0 就是这么编的）。
                #   0 次模型调用，按具名理由记失败；缺失的一件走 A06 的降级与标记。
                evidence = {
                    "exit": "inputs_unavailable",
                    "cause": "context",
                    "missing_inputs": list(subtask.inputs),
                }
                self._note_gate(subtask, False, evidence)
                # ★ `failed=("runtime",)` **不是"运行期崩了"**：它与下面 `BlockRuntimeError`
                #   那一格共用同一个标签，而两者是不同的失败 —— 这里是**前置门**（输入一件都没到，
                #   0 次模型调用）。真因在 `evidence` 里（`exit="inputs_unavailable"`），
                #   读读数时先看它，不要从标签猜。
                verdicts[subtask.id] = Verdict(ok=False, failed=("runtime",), evidence=evidence)
                continue
            inherited = tuple(
                sorted({p.snapshot_ref for name in subtask.inputs for p in piece_provenance.get(name, ())})
            )
            toolset = self._toolset(
                plan, subtask, bundle, ontology, snapshots, pieces, piece_provenance, inherited, context
            )
            try:
                candidate = self.runtime.run(subtask, bundle, ontology, toolset, self.budget)
            except BlockDeclined as declined:
                self._note_gate(subtask, False, {"exit": declined.exit_reason, "declined": True})
                verdicts[subtask.id] = Verdict(ok=False, failed=("declined",), evidence={"exit": declined.exit_reason})
                continue
            except BlockRuntimeError as failed:
                # ★ §7⑫: a withheld candidate was rejected by the *judge*, and the
                #   judge still holds which constraints did it -- that is the
                #   declaration, recorded here on the real event stream.
                failure_evidence = self._failure_evidence(failed)
                failure_detail: dict[str, Any] = {"exit": failed.exit_reason}
                if failure_evidence.get("rejected_by"):
                    failure_detail["constraint_rejected"] = True
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
                )
                continue
            candidates[subtask.id] = candidate
            piece_provenance[subtask.output] = candidate.provenance
            judge = self.runtime.last_judge
            verdict = judge.last_verdict if judge is not None and judge.last_verdict is not None else judge.admit(candidate)  # type: ignore[union-attr]
            verdicts[subtask.id] = verdict
            # ★ judged against this block's slice, not the whole plan's bundle
            decisions.append(converge(block_bundle(bundle, subtask, ontology), (candidate,), context))
            artifact = candidate.artifact(subtask.output)
            if artifact is not None:
                pieces[subtask.output] = artifact
            # ★ §7⑫: this verdict comes from the judge, so its ``failed`` names the
            #   constraints that rejected the block -- the judge is the party that
            #   declares it. Written here (not inferred downstream) so attribution
            #   reads a declaration in a *real* run, not only in a hand-made trace.
            gate_detail: dict[str, Any] = {"failed": list(verdict.failed)}
            if not verdict.ok:
                gate_detail["constraint_rejected"] = True
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
            else:
                upstream[name] = {
                    "schema": plan.interfaces.get(name, ""),
                    **artifact_payload(artifact),
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
