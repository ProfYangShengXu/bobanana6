"""Independent judgment for one block, and the evidence a real command produces.

``LoopIO.verify`` needs a :class:`GateVerdict`, and the whole point of the
completion gate is that the verdict does **not** come from the worker's own
report (架构设计 §7.3). Two independent sources feed it here:

  * ``core/kernel.py::admit`` -- the constraint bundle judged against the
    candidate the worker submitted;
  * an optional shell command declared by a ``custom`` constraint, whose exit
    status is recorded through ``gate.record_evidence`` and turned into a
    verdict by ``gate.evaluate``. Recording is what makes "checked, then
    changed" detectable, and ``attribute_exit`` is what stops a piped or
    backgrounded command from donating its exit code to the pipeline.

The command comes from the contract (a human wrote it), never from model output:
``custom`` supplies a *name*, the host supplies the code.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agentloop.gate import (
    GateVerdict,
    VerificationEvidence,
    evaluate,
    record_evidence,
)
from ..core.checkers.base import CheckContext, CheckOutcome, fail, ok
from ..core.kernel import admit
from ..core.model import Candidate, Constraint, ConstraintBundle, Subtask, Verdict

__all__ = [
    "BlockJudge",
    "RecordedEvidence",
    "command_unrunnable",
    "judge_environment_problems",
    "shell_command_check",
    "verdict_violations",
]

#: The name a host registers to let a ``custom`` constraint run a shell command.
SHELL_COMMAND_CHECK = "shell_command"

#: ★ `#44`: 判据命令【自己】没跑起来时的**结构化**证据（两条，都不猜字符串）：
#:   · POSIX 的 126（找到但不可执行）/ 127（找不到）；
#:   · 命令点名的那个 `-m <module>` 导不进来（`python3 -m pytest` 少了 pytest 就是这一类）。
#: ★ 刻意**不**收 "No such file or directory" 之类的通用话术：产出没写出文件时，
#:   测试断言里天天出现这句 —— 把它当环境问题就是冤枉了判定器。
_UNRUNNABLE_EXITS = (126, 127)


def _segments(command: str) -> tuple[tuple[str, ...], ...]:
    """按操作符把一条 shell 命令切成段（认不出来就返回空 —— 宁可不预检，不误报）。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ()
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in ("&&", "||", ";", "|"):
            segments.append([])
            continue
        segments[-1].append(token)
    return tuple(tuple(segment) for segment in segments if segment)


def _named_modules(command: str) -> tuple[str, ...]:
    """命令里显式点名的 `-m <module>`（`python3 -m pytest …`）。"""
    modules: list[str] = []
    for segment in _segments(command):
        for index, token in enumerate(segment[:-1]):
            if token == "-m":
                modules.append(segment[index + 1])
    return tuple(modules)


def command_unrunnable(command: str, output: str, exit_code: int) -> str:
    """命令【自己】没跑起来时说一句为什么（空串 = 它跑起来了，失败是产出的事）。

    ★ 只认结构化证据：POSIX 的 126/127，与"我们点名的那个 `-m <module>` 导不进来"。
    测试断言里出现 "No such file or directory" 不算 —— 那可能是产出没写出文件。
    """
    if exit_code in _UNRUNNABLE_EXITS:
        return f"the shell could not run the command (exit {exit_code}): {output.strip()[:200] or '<no output>'}"
    for module in _named_modules(command):
        # 解释器有两种说法：`No module named pytest`（`-m` 那条路，无引号）与
        # `ModuleNotFoundError: No module named 'pytest'`。只认**我们点名的那个**模块，
        # 而且要卡词边界（`pytestfoo` 不算 `pytest`）。
        if re.search(rf"no module named ['\"]?{re.escape(module)}['\"]?\b", output, re.IGNORECASE):
            return (
                f"the interpreter cannot import the module this command asks for ({module!r}): "
                f"{output.strip()[:200]}"
            )
    return ""


def judge_environment_problems(
    constraints: Sequence[Constraint],
    *,
    cwd: Path,
    timeout_s: float = 10.0,
) -> tuple[tuple[str, str], ...]:
    """★ `#44`: 派活前把"判据命令在这个环境里根本跑不起来"找出来（具名、可指到解释器）。

    照 ontology 门的先例：跑不起来 ⇒ 具名拒 + **0 次模型调用**，而不是跑完整轮之后记成
    "判据太严"。只检查**环境**：解释器/命令在不在、点名的模块导不导得进、`cd` 的目录在不在
    （★ 只判**绝对**目标 —— 见下条）；**不碰命令的输入文件**（那些正是模型要产出的）。

    ★★ `#46`：只判**运行期不会改变的东西**。判据命令里的 `_run/workspace` 这类目录是
    **壳在运行期自己创建**的（工作区在第一次写产物时才落盘），门在预检时要求它已经存在，
    就会：① 拦死所有"从没跑过"的新题；② 给"历史上跑过、目录残留"的老题放行（凭运气）；
    ③ 让「删掉 `_run` 重跑」这个正常操作失败。所以：

      · 解释器/命令在不在 —— 判（`PATH` 查找，或**绝对路径**必须可执行）；
      · **绝对** `cd` 目标 / 绝对路径 —— 判（作者事先写下的路径，没人替他创建）；
      · **相对** `cd` 目标 —— 不判存在性（它可能正是壳运行期要创建的那个目录）。
    """
    problems: list[tuple[str, str]] = []
    for constraint in constraints:
        if constraint.kind != "custom" or constraint.spec.get("check") != SHELL_COMMAND_CHECK:
            continue
        command = constraint.spec.get("command")
        if not isinstance(command, str) or not command:
            continue
        for problem in _command_environment_problems(command, cwd=cwd, timeout_s=timeout_s):
            problems.append((f"{constraint.id}:{problem[0]}", problem[1]))
    return tuple(problems)


def _command_environment_problems(command: str, *, cwd: Path, timeout_s: float) -> tuple[tuple[str, str], ...]:
    problems: list[tuple[str, str]] = []
    effective_cwd = cwd
    #: ★ `#46`: 只有"此刻真的看得见"的目录才拿来判相对路径。`cd` 到一个运行期才会被壳
    #:   创建出来的目录之后，相对路径无从判断 —— 宁可不判，也不拿运行期状态当判据。
    cwd_known = True
    for segment in _segments(command):
        if not segment:
            continue
        head = os.path.expandvars(segment[0])
        if head == "cd" and len(segment) > 1:
            raw = os.path.expandvars(segment[1])
            target = Path(raw)
            if target.is_absolute():
                # 作者事先写下的绝对路径：没人替他创建 ⇒ 不存在就是真问题。
                if target.is_dir():
                    effective_cwd, cwd_known = target, True
                else:
                    problems.append(("cwd_missing", f"`cd {raw}`: {target} is not a directory"))
                    cwd_known = False
                continue
            resolved = effective_cwd / target
            if resolved.is_dir():
                effective_cwd, cwd_known = resolved, True
            else:
                # ★ `#46`: 相对目录可能是壳在运行期创建的（工作区第一次写产物才落盘）
                #   ⇒ 不作为拒跑理由；但从此不再声称知道 cwd。
                cwd_known = False
            continue
        runnable = True
        if "/" in head:
            path = Path(head)
            resolved = path if path.is_absolute() else (effective_cwd / path)
            judgeable = path.is_absolute() or cwd_known
            if judgeable and not (resolved.is_file() and os.access(resolved, os.X_OK)):
                problems.append(("interpreter_missing", f"{head!r} is not an executable file ({resolved})"))
                runnable = False
            elif not judgeable and not resolved.is_file():
                # 解释器藏在一个现在还看不见的目录里 ⇒ 连模块都没法探。
                runnable = False
            interpreter = str(resolved)
        else:
            found = shutil.which(head)
            if found is None:
                problems.append(("interpreter_missing", f"{head!r} was not found on PATH"))
                runnable = False
            interpreter = found or head
        if not runnable:
            # 解释器都不在：别再拿它去探模块（那只会再抛一次"文件不存在"）
            continue
        for module in _modules_in(segment):
            probe = subprocess.run(
                [interpreter, "-c", f"import {module}"],
                # ★ `#46`: cwd 不可见时退到"我们确实站着的地方"探模块 —— 第三方包能不能
                #   导入不取决于工作区，别为了一个还没创建的目录把这条预检丢掉。
                cwd=str(effective_cwd if cwd_known else cwd),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            if probe.returncode != 0:
                tail = ((probe.stdout or "") + (probe.stderr or "")).strip().splitlines()
                problems.append(
                    (
                        "module_missing",
                        f"the interpreter {interpreter!r} cannot import {module!r}: {tail[-1] if tail else '<no output>'}",
                    )
                )
    return tuple(problems)


def _modules_in(segment: Sequence[str]) -> tuple[str, ...]:
    return tuple(segment[index + 1] for index, token in enumerate(segment[:-1]) if token == "-m")


@dataclass
class RecordedEvidence:
    """Evidence recorded during one verification, in order."""

    records: list[VerificationEvidence] = field(default_factory=list)

    def append(self, evidence: VerificationEvidence) -> None:
        self.records.append(evidence)

    def last(self) -> VerificationEvidence | None:
        return self.records[-1] if self.records else None


def shell_command_check(
    evidence: RecordedEvidence,
    clock: Callable[[], float],
    *,
    timeout_s: float = 120.0,
) -> Callable[[Constraint, Candidate, CheckContext], CheckOutcome]:
    """Build the ``custom`` check that runs a command and records what happened.

    ★ `#44`: 返回 ``CheckOutcome`` 而不是 ``(bool, facts)`` 元组 —— 多出来的那一个字段是
    ``cause``：命令**自己**没跑起来时由检查器声明 ``"tool"``（宿主检查器的两种返回都收，
    见 `core/checkers/custom.py`）。
    """

    def run(constraint: Constraint, candidate: Candidate, context: CheckContext) -> CheckOutcome:
        command = constraint.spec.get("command")
        if not isinstance(command, str) or not command:
            return fail(constraint, "spec.command must be a non-empty string")
        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return fail(constraint, f"command timed out after {timeout_s}s: {command}")
        output = (completed.stdout or "") + (completed.stderr or "")
        recorded = record_evidence(command, int(completed.returncode), output, created_at=clock())
        evidence.append(recorded)
        facts = [f"{command!r} exited {recorded.exit_code}"]
        if completed.returncode != 0:
            # ★ `#44`: 命令【自己】没跑起来（解释器/包缺失）不是"产出不合格"——
            #   把它声明成 `tool`，归因层就不会建议 `relax_constraint`。
            unrunnable = command_unrunnable(command, output, int(completed.returncode))
            if unrunnable:
                return fail(constraint, *facts, unrunnable, cause="tool")
            return fail(constraint, *facts, output.strip()[:400] or "<no output>")
        if not recorded.attributable:
            # ★ An exit status that a pipe or a background job could have donated
            #   is not evidence that this command succeeded.
            return fail(constraint, *facts, "exit status is not attributable to a single command")
        return ok(constraint, *facts)

    return run


@dataclass
class BlockJudge:
    """Judge one block, and say whether the system may call it done."""

    bundle: ConstraintBundle
    subtask: Subtask
    context: CheckContext
    required_scope: str = "full"
    #: Newest edit time of the block's produced files; injected, never read here
    #: (``gate.is_stale`` compares timestamps, and a judge that reads the clock
    #: cannot be replayed).
    # blame: fail-closed — 省略即视为"产物刚刚被改过"⇒ 既有验证一律算过期（与 edit_clock 同一条）
    last_edit_at: float = float("inf")
    evidence: RecordedEvidence = field(default_factory=RecordedEvidence)
    #: The verdict the loop blocked on, so the runner can report it without
    #: re-running the checks (a command check must not execute twice).
    last_verdict: Verdict | None = None

    def admit(self, candidate: Candidate) -> Verdict:
        return admit(self.bundle, candidate, self.context)

    def verify(self, candidate: Candidate) -> GateVerdict:
        """The gate the loop blocks on. Without evidence this is the kernel's call."""
        verdict = self.admit(candidate)
        self.last_verdict = verdict
        if not verdict.ok:
            return GateVerdict(status="failed", scope=self.required_scope, violations=verdict_violations(verdict))
        recorded = self.evidence.last()
        if recorded is None:
            return GateVerdict(status="passed", scope=self.required_scope)
        return evaluate(recorded, required_scope=self.required_scope, last_edit_at=self.last_edit_at)


def verdict_violations(verdict: Verdict) -> tuple[str, ...]:
    """The concrete rule each failure broke, in judgment order.

    ``<constraint id>: <observed facts>`` -- the id names the rule, the facts name
    what was actually seen. JSON Schema's output model (``keywordLocation`` +
    ``message``) and RFC 9457's ``detail`` are the precedents: a rejection has to
    say *which* constraint and *what* it saw, or the next attempt is a guess.
    """
    rendered: list[str] = []
    for constraint_id in verdict.failed:
        facts = verdict.evidence.get(constraint_id, "")
        rendered.append(f"{constraint_id}: {facts}" if facts else constraint_id)
    return tuple(rendered)


def verdict_summary(verdict: Verdict, judge: BlockJudge) -> dict[str, Any]:
    """A report-shaped view of one verdict (``Verdict.evidence`` is external:report)."""
    return {
        "ok": verdict.ok,
        "failed": list(verdict.failed),
        "evidence": dict(verdict.evidence),
        "soft_scores": dict(verdict.soft_scores),
        "gate": judge.required_scope,
    }
