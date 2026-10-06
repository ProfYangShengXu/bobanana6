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
import shutil
from collections.abc import Callable, Mapping
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
    # ★ `retry` / `retry_after_error` 是 loop 的**续跑**理由（`PhaseResult.cont`），
    #   不是终态：下一轮会把 `exit_reason` 覆盖掉，预算用尽时是 `budget_exhausted`。
    #   这两条映射今天【到不了】（见 tests/test_agentloop_loop.py 里的看门狗）——
    #   留着是因为词表按 `RecoveryAction` 对齐；真要收 `retry_local`，得先让运行期
    #   把它记成"发生过一次重试"（那是壳的改动，不是采集的改动）。
    "retry": "retry_local",
    "retry_after_error": "retry_local",
}

#: ★ `#30`：**计划层**的出口。`refuse_split` / `single_agent` 不是 run 级 exit、也不在
#: 任何块 verdict 里 —— 它们由切分决策产生（`SplitPlan.decision` / `.fallback`）。
#: 只读 run 级与块级时，这两条出口【结构上不可能出现】——那不是"没走到"，是"没被读"。
_PLAN_EXITS: Mapping[str, str] = {
    "refuse_split": "refuse_split",
    "single_agent": "single_agent",
}


@dataclass(frozen=True)
class TaskRun:
    """One acceptance task's outcome, judged."""

    task: str
    report: AcceptanceReport
    #: The gate that refused the run before any worker started, if any.
    refused: str | None = None
    #: ★ `#44`: the codes that gate named (empty when nothing was refused).
    refused_codes: tuple[str, ...] = ()
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
    #: ★ `#33`: the workspace this run wrote code into (``None`` unless asked for).
    workspace: Path | None = None
    #: ★ `#43`: where the inheritance guard dropped its observations (``None`` unless asked for).
    observations_dir: Path | None = None


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
    """这一跑真正走过哪些出口 —— **三层信号合起来**才算完整（`#30` 量的）。

    * **run 级** —— `ok`（装配过 + 全部块合格）与 `exit_reason`（`passed` / `declined` / `failed`）；
    * **块级** —— 每个 verdict 的 `evidence["exit"]`（loop 的终态理由，含具名门）；
    * **计划层** —— `SplitPlan.decision` / `.fallback`（`refuse_split` / `single_agent`）。

    ★ 缺了第三层时，「该不该拆」这条判据走得再真也进不了图 —— `#25` 修好的
    `refuse_split` / `single_agent` 因此在那之前的每一张图上都是 `not_hit`。
    """
    seen: set[str] = set()
    if result.ok:
        seen.add("deliver")
    run_reason = str(getattr(result, "exit_reason", ""))
    if run_reason in _EXIT_FROM_REASON:
        seen.add(_EXIT_FROM_REASON[run_reason])
    for verdict in result.verdicts.values():
        reason = str(verdict.evidence.get("exit", ""))
        mapped = _EXIT_FROM_REASON.get(reason)
        if mapped is not None:
            seen.add(mapped)
    plan = getattr(result, "plan", None)
    if plan is not None:
        for name in (getattr(plan, "decision", ""), getattr(plan, "fallback", "")):
            mapped = _PLAN_EXITS.get(str(name))
            if mapped is not None:
                seen.add(mapped)
    return seen


def _contract_sources(task_dir: Path, task_file: str = "task.yaml") -> tuple[str, ...]:
    """The declared source refs of the task, read for the degradation note only."""
    import yaml

    document = yaml.safe_load((task_dir / task_file).read_text(encoding="utf-8"))
    return tuple(str(row.get("ref", "")) for row in (document.get("sources", ()) or ()) if isinstance(row, dict))


def _contract_deliverables(task_dir: Path, task_file: str = "task.yaml") -> tuple[str, ...]:
    """The declared deliverable paths — read to **prepare the workspace**, nothing else.

    ★ 用途只有一个：工作区播种时按它们建好父目录（见 ``run_task`` 里的注记）。
    ★ 读不到就返回空 —— 这不是判定，缺了只影响"目录先建好"这个便利，
      不许因此让任何一跑失败。
    """
    import yaml

    try:
        document = yaml.safe_load((task_dir / task_file).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 准备工作，读不出来就不建
        return ()
    rows = document.get("deliverables") or ()
    if not isinstance(rows, (list, tuple)):
        return ()
    names: list[str] = []
    for row in rows:
        name = str((row or {}).get("name", "")) if isinstance(row, dict) else str(row or "")
        if name and not name.startswith(("/", "\\")) and ".." not in Path(name).parts:
            names.append(name)
    return tuple(names)


def run_task(
    task_dir: str | Path,
    *,
    client: Any = None,
    env: Mapping[str, str] | None = None,
    strategy: str = "source",
    expected_blame: str | None = None,
    offline: bool = False,
    concurrency: int | None = None,
    task_file: str = "task.yaml",
    workspace: bool = False,
    workspace_seed: Callable[[Path], None] | None = None,
    observe: bool = False,
) -> TaskRun:
    """Run one acceptance task through the real assembly point.

    ``client`` exists for the deterministic mechanism tests: it is substituted *at*
    ``cli.build_client`` (the single construction site) rather than by reaching
    around the assembly point.

    ``task_file`` selects which contract file to run. It exists because a task
    directory can hold **several versions of the same question** and the interesting
    one is sometimes not ``task.yaml`` — e.g. the counterexample's
    ``party-task.yaml`` declares ``sources_share_state`` and is refused a split,
    while its ``task.yaml`` is the single-source counterpart. Without this knob the
    refusal could only be run *outside* the harness, so its exit never reached the
    coverage map（`#30`）.
    """
    from bobanana6 import cli
    from bobanana6.assemble_impl import RunRefused, RunResult

    base = Path(task_dir).resolve()
    _materialise(base)
    out = base / "_run" / "reports"
    # ★ `#33`: 代码类任务要有工作区 —— 它是 `input/` 的一份**可写**拷贝：冻结的测试文件、
    #   模块、接口都在里面，块只改自己写区里的那几个文件。播种在验收层（准备 fixture），
    #   写区守卫与冻结比对在壳里。
    workspace_dir = base / "_run" / "workspace"
    if workspace:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        # ★ 保持与 locator 同一套相对路径：locator 写的是 `input/...`，工作区里也就是
        #   `<ws>/input/...` —— 冻结比对与写区检查都按这一条比较（不猜布局）。
        workspace_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base / "input", workspace_dir / "input")
        # ★★ 工作区必须**能完成这道题**：契约声明的产物目录要先存在。
        #   实测（13f）：deliverables 是 `output/*.csv`，而块要生成一份 1681 行的
        #   CSV 只能靠 `code.run`（手写不了），于是 Python 里
        #   `df.to_csv('output/x.csv')` 直接
        #     `OSError: Cannot save file into a non-existent directory: 'output'`
        #   —— `files.write` 会自建父目录，`code.run` / `shell.run` **不会**。
        #   ⇒「块自己算、再落盘」这条路在**环境层面**就是断的；
        #     那是壳的账，不是模型的账（模型再怎么听话也写不出来）。
        #   ★ 按**契约自己声明的**交付物建目录，不特判任何题目。
        for name in _contract_deliverables(base, task_file):
            parent = (workspace_dir / name).parent
            if parent != workspace_dir:
                parent.mkdir(parents=True, exist_ok=True)
        if workspace_seed is not None:
            # ★ 反证钩子：播种之后、开跑之前动工作区（例如把测试改松）——
            #   "测试文件被改"必须在装配期被抓住，而不是靠没人去改。
            workspace_seed(workspace_dir)
    # ★ `#43`: 继承守卫的观测目录 —— 拒收时把"池子 + 交上来的行 + 逐键差异"落在这里。
    #   只落盘、不改判定；不开这个开关就【一个字节都不写】（既有读数不受影响）。
    observations_dir = base / "_run" / "observations"
    if observe:
        shutil.rmtree(observations_dir, ignore_errors=True)
        observations_dir.mkdir(parents=True, exist_ok=True)
    snapshots = base / "_run" / "snapshots"
    argv = [
        "run",
        str(base / task_file),
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
    if workspace:
        argv.extend(["--workspace", str(workspace_dir)])
    if observe:
        argv.extend(["--observe-dir", str(observations_dir)])
    if concurrency is not None:
        # ★ The declared worker pool is a *deployment* fact, not a task property:
        #   a 20–40 block plan legitimately needs the deployment to say it can serve
        #   that concurrency, or the capability check reports ``capability_short``.
        argv.extend(["--concurrency", str(concurrency)])
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
            refused_codes = tuple(refusal.codes)
        else:
            refused_codes = ()
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
            refused_codes=refused_codes,
            inputs_given=given,
            workspace=workspace_dir if workspace else None,
            observations_dir=observations_dir if observe else None,
        )

    exits = _exits_from_run(result)
    base_sources = _contract_sources(base, task_file)
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

    # ★ A06: a degraded run has to say so -- "we ran without source X" must not be
    #   something the reader only discovers by diffing the panel against the truth.
    if result.fetch_failures:
        missing = ", ".join(sorted(result.fetch_failures))
        notes.append(f"sources unavailable: {missing} ({len(result.fetch_failures)} of {len(base_sources)})")

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
        workspace=workspace_dir if workspace else None,
        observations_dir=observations_dir if observe else None,
    )
