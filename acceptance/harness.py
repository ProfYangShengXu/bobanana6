"""把一道验收题跑成一份 :class:`AcceptanceReport`（`实现规格.md §14`）。

三条结构纪律在这里落地：

  * **只把 `input/` 交给系统** —— `truth/` 是判分器读的，运行期从不传进去。
    不隔离时「模型没答对」和「模型偷看了」分不开。
  * **三口径分开** —— 块级通过率 / 装配是否通过 / 归因准确率，各报各的。
  * **出口覆盖要声明** —— 从运行结果里认出走过哪些出口，其余的显式记 `not_hit`。

★ 这里不做判定：判定在 `core/`。本模块只负责「跑什么、怎么读结果」。
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .judge import AcceptanceReport, coverage_from_exits
from .truth import TruthVerdict, compare_rows, load_truth

__all__ = ["TaskRun", "run_task"]

#: 从运行结果里认得出的出口 → 结果里长什么样。
#: ★ 认不出的一律不写进 coverage（宁可不声明，也不发明一个没被走到的出口）。
_EXIT_FROM_REASON: Mapping[str, str] = {
    "degrade_single_agent": "degrade_single",
    "declined": "decline",
    "abort": "abort",
    "reshard": "reshard",
    "retry": "retry_local",
    "retry_after_error": "retry_local",
}


@dataclass(frozen=True)
class TaskRun:
    """One acceptance task's outcome, judged."""

    task: str
    report: AcceptanceReport
    #: The gate that refused the run before any worker started, if any.
    refused: str | None = None
    plan_shape: str = ""
    blocks: tuple[str, ...] = ()
    verdicts: Mapping[str, Any] = field(default_factory=dict)
    #: ★ What each block actually produced -- kept so a test can read the artifact
    #: (e.g. prove a value was normalised) instead of inferring it from a verdict.
    candidates: Mapping[str, Any] = field(default_factory=dict)
    assembly_codes: tuple[str, ...] = ()
    snapshot_refs: tuple[str, ...] = ()
    #: ★ The paths handed to the system -- so a test can prove `truth/` was not among them.
    inputs_given: tuple[str, ...] = ()
    #: ★ The fourth reading, kept apart from the three axes: is the answer *right*?
    truth: TruthVerdict | None = None


def _materialise(task_dir: Path) -> None:
    """Run the task's generator, if it has one. Input and truth land in separate dirs."""
    generator = task_dir / "generate.py"
    if not generator.is_file():
        return
    spec = importlib.util.spec_from_file_location(f"_acceptance_generate_{task_dir.name}", generator)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken generator is a broken task
        raise ImportError(f"cannot load {generator}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.generate()


def _exits_from_run(result: Any) -> set[str]:
    seen: set[str] = set()
    if result.ok:
        seen.add("deliver")
    for verdict in result.verdicts.values():
        reason = str(verdict.evidence.get("exit", ""))
        mapped = _EXIT_FROM_REASON.get(reason)
        if mapped is not None:
            seen.add(mapped)
    return seen


def run_task(
    task_dir: str | Path,
    *,
    client: Any = None,
    env: Mapping[str, str] | None = None,
    strategy: str = "source",
    expected_blame: str | None = None,
    offline: bool = False,
) -> TaskRun:
    """Run one acceptance task through the real assembly point.

    ``client`` exists for the deterministic mechanism tests: it is substituted *at*
    ``cli.build_client`` (the single construction site) rather than by reaching
    around the assembly point.
    """
    from bobanana6 import cli
    from bobanana6.assemble_impl import RunRefused, RunResult

    base = Path(task_dir).resolve()
    _materialise(base)
    out = base / "_run" / "reports"
    snapshots = base / "_run" / "snapshots"
    argv = [
        "run",
        str(base / "task.yaml"),
        "--ontology",
        str(base / "ontology.yaml"),
        "--strategy",
        strategy,
        "--out",
        str(out),
        "--snapshots",
        str(snapshots),
    ]
    if offline:
        argv.append("--offline")
    previous = Path.cwd()
    if client is not None:
        original = cli.build_client
        cli.build_client = lambda args, source_env: client  # type: ignore[assignment]
    else:
        original = None
    os.chdir(base)  # the task's locators are relative to its own directory
    try:
        args = cli._parser().parse_args(argv)
        app = cli.build_app(args, env=dict(env or {}))
        try:
            result: RunResult | None = app.run()
            refused: str | None = None
        except RunRefused as refusal:
            result = None
            refused = refusal.gate
    finally:
        os.chdir(previous)
        if original is not None:
            cli.build_client = original  # type: ignore[assignment]

    given = tuple(sorted(str(path.relative_to(base)) for path in (base / "input").glob("*")))
    if result is None:
        return TaskRun(
            task=base.name,
            report=AcceptanceReport(
                block_pass_rate=0.0,
                assembly_passed=False,
                attribution_accuracy=0.0,
                coverage=coverage_from_exits(()),
                notes=(f"the run was refused at the {refused!r} gate before any worker started",),
            ),
            refused=refused,
            inputs_given=given,
        )

    exits = _exits_from_run(result)
    total = len(result.verdicts)
    passed = sum(1 for verdict in result.verdicts.values() if verdict.ok)
    accuracy = 0.0
    notes: list[str] = []
    if expected_blame is not None:
        actual = result.attribution.blame if result.attribution is not None else None
        accuracy = 1.0 if actual == expected_blame else 0.0
        notes.append(f"attribution: expected {expected_blame!r}, got {actual!r}")
    else:
        notes.append("attribution was not evaluated: no labelled failure was supplied")
    if exits and "decline" in exits:
        notes.append("a block declined (the third legal exit)")

    coverage = coverage_from_exits(exits)
    if "taint" not in exits:
        notes.append("taint: no tool failed repeatedly in this task")

    # ★ 第四条读数：与真值对照。它不进那三个维度（§15.6 禁单一总分），单独报。
    verdict_truth: TruthVerdict | None = None
    truth_path = base / "truth" / "truth.json"
    if truth_path.is_file():
        document = load_truth(base)
        deliverable = str(document.get("deliverable", ""))
        produced = next(
            (
                candidate.artifact(deliverable)
                for candidate in result.candidates.values()
                if candidate.artifact(deliverable) is not None
            ),
            None,
        )
        rows = [] if produced is None else [dict(row) for row in produced.rows]
        verdict_truth = compare_rows(rows, document)
        notes.append(verdict_truth.render())

    return TaskRun(
        task=base.name,
        report=AcceptanceReport(
            block_pass_rate=(passed / total) if total else 0.0,
            assembly_passed=result.assembly.passed,
            attribution_accuracy=accuracy,
            coverage=coverage,
            notes=tuple(notes),
        ),
        plan_shape=result.plan.shape,
        blocks=result.plan.subtask_ids,
        verdicts=dict(result.verdicts),
        candidates=dict(result.candidates),
        assembly_codes=result.assembly.codes(),
        snapshot_refs=tuple(sorted(result.snapshots)),
        inputs_given=given,
        truth=verdict_truth,
    )
