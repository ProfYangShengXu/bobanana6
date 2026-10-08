"""CI 的**数据层**（规格 §三①）：把快照里的【指针】换成 `core` 要的对象。

★ 与 `services/ci.py` 的分工，一个字都不许串：

```
services/ci.py          = 调度 core 已有的判据（不碰环境、不读盘）      ← 判定侧
acceptance/ci_runner.py = 打开文件 / 读 git 痕迹仓库 / 重算对账          ← 数据侧
```

为什么必须分开：`core/` 的判据要能在**任何环境**里逐字复现（`core/__init__.py`
自己写了"pure stdlib, deterministic, NO I/O"）。一旦把重算对账塞进 core，
它就同时破坏了「无 I/O」与「箭头只朝 core」两条 —— 那两条是 `tests/test_arch_guard.py` 在守的。

**快照里的指针有三类**（都不放全文，规格 §三②）：

```
contract_file  {path, sha256}   → 任务契约（正文在仓库里，重解析 + 摘要校验）
ontology_file  {path, sha256}   → 本体（同上）
artifacts      {ref, sha256, …} → 大产物正文在 git 痕迹仓库里（`services/artifacts.py` 的树 + CAS）
```

★ 摘要对不上 ⇒ `SnapshotDrift`，**具名拒**。这不是洁癖：验收跑错版本 = 白验
（本项目既有教训），"指针指的东西变了"必须比"结果变了"先被发现。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bobanana6.assemble_impl.runtime import block_bundle as runtime_block_bundle
from bobanana6.contract import compile_bundle, parse_contract_file
from bobanana6.core.model import Artifact, Column, Snapshot, SnapshotBody
from bobanana6.ontology import load_ontology_file
from bobanana6.services.ci import (
    BlockCapture,
    build_snapshot,
    run_ci,
    run_content_checks,
)
from bobanana6.sources.reader import rows_from_csv, rows_from_json

__all__ = [
    "ContentExecutorImpl",
    "SnapshotDrift",
    "TaskSnapshotSource",
    "capture_run",
    "ci_for_run",
    "content_report_for_run",
]


class SnapshotDrift(RuntimeError):
    """快照记的摘要与实际读到的东西对不上（具名拒，不静默跑）。"""


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def capture_run(
    run: Any,
    *,
    task_dir: str | Path,
    task_file: str = "task.yaml",
    ontology_file: str = "ontology.yaml",
    artifact_root: str | Path | None = None,
) -> dict[str, Any]:
    """把一个跑测的读法收成一份**快照**（规格 §三②：摘要 + 指针）。

    `run` 只需要有 `blocks / candidates / verdicts / plan / snapshots` 这些属性
    （`acceptance/harness.py::TaskRun` 就是），于是这里不必 import harness。
    """
    base = Path(task_dir).resolve()
    plan = run.plan
    if plan is None:
        raise ValueError("this run carries no plan; a snapshot needs one to be replayable")
    outputs = {subtask.id: subtask.output for subtask in plan.subtasks}
    refs_root = Path(artifact_root) if artifact_root is not None else None

    blocks: list[BlockCapture] = []
    for block_id in run.blocks:
        candidate = run.candidates.get(block_id)
        artifacts = tuple(candidate.artifacts) if candidate is not None else ()
        refs = {
            artifact.name: f"{block_id}/{artifact.name}"
            for artifact in artifacts
            if refs_root is not None
        }
        blocks.append(
            BlockCapture(
                subtask_id=block_id,
                output=outputs.get(block_id, artifacts[0].name if artifacts else block_id),
                verdict=run.verdicts[block_id],
                artifacts=artifacts,
                artifact_refs=refs,
                provenance=tuple(candidate.provenance) if candidate is not None else (),
            )
        )

    contract_path = base / task_file
    ontology_path = base / ontology_file
    snapshots: dict[str, dict[str, Any]] = {
        str(ref): {"path": str(snapshot.body_path), "body_path": str(snapshot.body_path), "captured_at": snapshot.captured_at}
        for ref, snapshot in (getattr(run, "snapshots", None) or {}).items()
    }
    return build_snapshot(
        task_id=str(plan.task_id),
        plan=plan,
        blocks=blocks,
        contract_pointer={"path": task_file, "sha256": _digest_bytes(contract_path.read_bytes())},
        ontology_pointer=(
            {"path": ontology_file, "sha256": _digest_bytes(ontology_path.read_bytes())}
            if ontology_path.is_file()
            else {}
        ),
        snapshots=snapshots,
        frozen_files={},
    )


@dataclass
class TaskSnapshotSource:
    """`services/ci.py::SnapshotSource` 的实作：**只有这里碰环境**。"""

    document: Mapping[str, Any]
    task_dir: Path
    artifact_root: Path | None = None
    bundle_object: Any = None
    _contract: Any = field(default=None, init=False, repr=False)

    # -- 契约 / 本体 ------------------------------------------------------- #

    def _verify(self, pointer: Mapping[str, Any], *, what: str) -> Path:
        if not pointer:
            raise SnapshotDrift(f"the snapshot carries no pointer for the {what}")
        path = Path(self.task_dir) / str(pointer["path"])
        if not path.is_file():
            raise SnapshotDrift(f"the {what} pointed at {path} is gone")
        found = _digest_bytes(path.read_bytes())
        if found != str(pointer.get("sha256")):
            raise SnapshotDrift(
                f"the {what} at {path} changed since the snapshot "
                f"(recorded {pointer.get('sha256')}, found {found})"
            )
        return path

    def document_of_contract(self) -> Any:
        if self._contract is None:
            path = self._verify(self.document.get("contract_file") or {}, what="contract")
            self._contract = parse_contract_file(path)
        return self._contract

    def contract(self) -> Any:
        return self.document_of_contract().contract

    def ontology(self) -> Any:
        pointer = self.document.get("ontology_file") or {}
        if not pointer:
            return None
        return load_ontology_file(self._verify(pointer, what="ontology")).freeze()

    def bundle(self, plan: Any) -> Any:
        """整份约束包。跑测自己给的那一份优先（那就是它真正判过的东西）。"""
        if self.bundle_object is not None:
            return self.bundle_object
        return compile_bundle(self.document_of_contract(), plan)

    def block_bundle(self, block_id: str, plan: Any) -> Any:
        """★ 复用 `runtime.block_bundle` —— **不在这里重写"哪些约束判这个块"**。

        自己写一份 `keep()` 就是判定逻辑的第二个副本（规格 §四③），而且它会漂：
        改了 `runtime.block_bundle`（例如字段级规则向本块产出重指向），CI 报告就与真跑不一致。
        """
        subtask = next((one for one in plan.subtasks if one.id == block_id), None)
        if subtask is None:
            raise SnapshotDrift(f"the snapshot names block {block_id!r}, which the plan does not contain")
        return runtime_block_bundle(self.bundle(plan), subtask, self.ontology())

    # -- 快照（溯源用）----------------------------------------------------- #

    def snapshot(self, entry: Mapping[str, Any]) -> Snapshot:
        path = Path(str(entry.get("body_path") or entry.get("path") or ""))
        if not path.is_file():
            raise SnapshotDrift(f"the snapshot body at {path} is gone")
        return Snapshot(
            ref=str(entry.get("ref") or path.stem),
            body_path=path,
            captured_at=str(entry.get("captured_at", "")),
        )

    def snapshot_body(self, entry: Mapping[str, Any]) -> SnapshotBody | None:
        path = Path(str(entry.get("body_path") or entry.get("path") or ""))
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".json":
            columns, rows = rows_from_json(text)
        elif path.suffix.lower() == ".csv":
            columns, rows = rows_from_csv(text)
        else:
            columns, rows = (Column(name="text", dtype="str"),), ({"text": text},)
        return SnapshotBody(ref=str(entry.get("ref") or path.stem), columns=columns, rows=rows)

    # -- 大产物：正文在 git 痕迹仓库里（树 or CAS）------------------------- #

    def artifact(self, pointer: Mapping[str, Any]) -> Artifact:
        ref = str(pointer.get("ref") or "")
        if not ref or self.artifact_root is None:
            raise SnapshotDrift(
                f"artifact {pointer.get('name')!r} is pointer-only but no artifact store was given "
                f"(ref={ref!r})"
            )
        raw = (self.artifact_root / "tree" / ref).read_bytes()
        if raw.lstrip()[:1] == b"{":
            document = json.loads(raw.decode("utf-8"))
            if isinstance(document, Mapping) and "sha256" in document:
                raw = self._from_cas(str(document["sha256"]), document)
        found = _digest_bytes(raw)
        recorded = str(pointer.get("sha256") or "")
        if recorded and found != recorded:
            raise SnapshotDrift(
                f"artifact {pointer.get('name')!r} at {ref} changed since the snapshot "
                f"(recorded {recorded}, found {found})"
            )
        text = raw.decode("utf-8", errors="replace")
        if ref.lower().endswith(".json"):
            columns, rows = rows_from_json(text)
        else:
            columns, rows = rows_from_csv(text)
        return Artifact(name=str(pointer["name"]), columns=columns, rows=rows)

    def _from_cas(self, digest: str, pointer: Mapping[str, Any]) -> bytes:
        assert self.artifact_root is not None
        path = self.artifact_root / "cas" / digest[:2] / digest[2:]
        if not path.is_file():
            raise SnapshotDrift(
                f"artifact {pointer.get('name')!r} has only a pointer: {path} is not in the CAS "
                f"(the archive budget may have evicted its body)"
            )
        return path.read_bytes()


def ci_for_run(
    run: Any,
    *,
    task_dir: str | Path,
    task_file: str = "task.yaml",
    artifact_root: str | Path | None = None,
) -> tuple[dict[str, Any], Any]:
    """收快照 + 跑三层门，一次做完。返回 `(snapshot, report)`。"""
    document = capture_run(
        run, task_dir=task_dir, task_file=task_file, artifact_root=artifact_root
    )
    source = TaskSnapshotSource(
        document=document,
        task_dir=Path(task_dir).resolve(),
        artifact_root=Path(artifact_root) if artifact_root is not None else None,
        bundle_object=run.bundle,
    )
    return document, run_ci(document, source)


# --------------------------------------------------------------------------- #
# 第 ④ 格 · 内容层的数据侧：跑命令 / 重算对账 / 生成断言
# --------------------------------------------------------------------------- #
#
# ★ 分界（规格 §二②）：`services/ci.py` 只调度（发指令、收报告、定口径）；
#   跑外部命令 / 重算 / 读盘**只能在这里**。`core/` 一行都不动。
#
# ★ `generated` 那一档是本任务的重点：没有现成检查时，**派一个 worker 读规格 +
#   看真实数据，写出【可执行的内容断言】** —— 检查器跟着任务生成，不是人手写。

CONTENT_DIR = "content_checks"
GENERATED_MODULE = "generated_checks.py"
GENERATED_DATA = "generated_input.json"

#: ★ 生成时的**硬边界**：输入只有题面 + 真实数据。判分器（官方 checker / truth）
#: 一个字节都不许进 prompt —— 看判分器等于把答案抄进检查里，那道题就废了。
GENERATION_FORBIDDEN_PATHS = ("truth", "checker", "judge", "grader", "expected")

GENERATION_INSTRUCTION = """You are writing the CONTENT CHECK for one task. Read the spec below and the
real data summary, then produce a Python module with **executable assertions** about the content.

Hard requirements (the module is rejected otherwise):
1. Define `def run(artifacts: dict) -> list[dict]`. `artifacts` maps a deliverable name to
   `{"columns": [...], "rows": [...]}`. Return a list of findings, each
   `{"who": str, "expected": str, "observed": str, "ok": bool}`.
2. The module must run **standalone**: `if __name__ == "__main__":` reads a JSON file path from
   `sys.argv[1]` (same shape as `artifacts`) and prints `json.dumps(run(...))`.
3. Standard library only. No network. No file reads other than that argv[1].
4. Assert only what the spec and the data actually support -- an assertion you cannot justify from
   the material below is worse than no assertion.

SPEC
----
%(spec)s

DATA SUMMARY (columns and a bounded sample of real rows)
--------------------------------------------------------
%(data)s
"""


class ContentExecutorImpl:
    """`services/ci.py::ContentExecutor` 的实作 —— **只有这里碰环境**。"""

    def __init__(
        self,
        *,
        task_dir: str | Path,
        document: Mapping[str, Any],
        source: Any | None = None,
        client: Any | None = None,
        instruction: str = "",
        timeout_s: float = 120.0,
    ) -> None:
        self.task_dir = Path(task_dir).resolve()
        self.document = document
        self.source = source
        self.client = client
        self.instruction = instruction
        self.timeout_s = timeout_s
        #: 生成时到底喂了什么 —— 测试用它证明判分器没进 prompt。
        self.last_generation_prompt = ""

    # -- ① external_runner：外部 checker（唯一能验【外部正确性】的一档）---- #

    def external(self, spec: Mapping[str, Any]) -> list[Any]:
        from bobanana6.services.ci import STRENGTH_EXTERNAL, ContentFinding

        command = [str(part) for part in spec["command"]]
        cwd = Path(str(spec.get("cwd") or "."))
        if not cwd.is_absolute():
            cwd = self.task_dir / cwd
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return [
                ContentFinding(
                    check="external_runner",
                    strength=STRENGTH_EXTERNAL,
                    who=" ".join(command),
                    expected="the external checker runs",
                    observed=f"{type(exc).__name__}: {exc}",
                    ok=False,
                )
            ]
        tail = (completed.stdout or completed.stderr or "").strip().splitlines()[-3:]
        return [
            ContentFinding(
                check="external_runner",
                strength=STRENGTH_EXTERNAL,
                who=" ".join(command),
                expected="exit 0",
                observed=f"exit {completed.returncode}" + (f"; {tail}" if tail else ""),
                ok=completed.returncode == 0,
            )
        ]

    # -- ② invariants：自造对账（只能验【内部一致性】）------------------- #

    def invariants(self, specs: Sequence[Mapping[str, Any]]) -> list[Any]:
        from bobanana6.services.ci import STRENGTH_INTERNAL, ContentFinding

        artifacts = self._artifacts()
        findings: list[Any] = []
        for index, spec in enumerate(specs):
            who = f"invariant[{index}] {spec.get('left')} {spec.get('op')} {spec.get('right')}"
            left_column, right_column = str(spec["left"]), str(spec["right"])
            op = str(spec["op"])
            tol = float(spec.get("tol", 0.0))
            checked = 0
            violations: list[str] = []
            for name, artifact in artifacts.items():
                columns = {column["name"] for column in artifact["columns"]}
                if left_column not in columns or right_column not in columns:
                    continue
                for row_index, row in enumerate(artifact["rows"]):
                    left, right = row.get(left_column), row.get(right_column)
                    if left is None or right is None:
                        continue
                    checked += 1
                    if not _holds(left, op, right, tol):
                        violations.append(f"{name} row {row_index}: {left!r} {op} {right!r} is false")
            if not checked:
                # ★ 一条都没比到 = **没有覆盖**，不是"通过"（没验过 ≠ 验过了）。
                findings.append(
                    ContentFinding(
                        check="invariants",
                        strength=STRENGTH_INTERNAL,
                        who=who,
                        expected=f"columns {left_column!r} / {right_column!r} exist in some deliverable",
                        observed=f"no comparable column pair in {sorted(artifacts)}",
                        ok=False,
                    )
                )
                continue
            findings.append(
                ContentFinding(
                    check="invariants",
                    strength=STRENGTH_INTERNAL,
                    who=who,
                    expected=f"{left_column} {op} {right_column} for all {checked} comparable rows (tol={tol})",
                    observed="all hold" if not violations else f"{len(violations)} violated; {violations[0]}",
                    ok=not violations,
                )
            )
        return findings

    # -- ③ generated：派 worker 写出可执行的断言 -------------------------- #

    def generated(self) -> tuple[list[Any], str]:
        from bobanana6.services.ci import STRENGTH_INTERNAL, ContentFinding

        if self.client is None:
            return (
                [
                    ContentFinding(
                        check="generated",
                        strength=STRENGTH_INTERNAL,
                        who="generate",
                        expected="a worker writes an executable content assertion",
                        observed="no client was injected -- generation cannot run",
                        ok=False,
                    )
                ],
                "",
            )
        out_dir = self.task_dir / "_run" / CONTENT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        module_path = out_dir / GENERATED_MODULE
        data_path = out_dir / GENERATED_DATA
        payload = self._artifact_payload()
        data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

        prompt = GENERATION_INSTRUCTION % {"spec": self._spec_text(), "data": _summarise(payload)}
        self.last_generation_prompt = prompt
        # ★ 有界：**一次生成 + 一次自检**，不许无限迭代（规格 §二③）。
        response = self.client.complete(
            [{"role": "user", "content": prompt}], [], 4096
        )
        text = str(getattr(response, "text", "") or "")
        code = _extract_code(text)
        if not code:
            return (self._generation_failure("the worker returned no python code block"), "")
        module_path.write_text(code, encoding="utf-8")

        # ★ 独立跑：新进程、无会话状态（不依赖生成它的那次会话）。
        completed = subprocess.run(
            [sys.executable, str(module_path), str(data_path)],
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            check=False,
        )
        if completed.returncode != 0:
            return (
                self._generation_failure(
                    f"the generated module does not run standalone: exit {completed.returncode}; "
                    f"{(completed.stderr or '').strip().splitlines()[-1:] }"
                ),
                str(module_path),
            )
        try:
            raw = json.loads(completed.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            return (self._generation_failure(f"the generated module printed no JSON findings: {exc}"), str(module_path))
        findings = [
            ContentFinding(
                check="generated",
                strength=STRENGTH_INTERNAL,
                who=str(entry.get("who", "generated")),
                expected=str(entry.get("expected", "")),
                observed=str(entry.get("observed", "")),
                ok=bool(entry.get("ok", False)),
            )
            for entry in raw
        ]
        if not findings:
            return (self._generation_failure("the generated module asserted nothing"), str(module_path))
        return findings, str(module_path)

    def _generation_failure(self, why: str) -> list[Any]:
        """★ 生成失败 ⇒ **具名报**，绝不静默降级成 `none`（规格 §二③ / §六①）。"""
        from bobanana6.services.ci import STRENGTH_INTERNAL, ContentFinding

        return [
            ContentFinding(
                check="generated",
                strength=STRENGTH_INTERNAL,
                who="generate",
                expected="an executable content assertion",
                observed=why,
                ok=False,
            )
        ]

    # -- 取材 -------------------------------------------------------------- #

    def _artifact_payload(self) -> dict[str, Any]:
        artifacts = self._artifacts()
        return {
            name: {"columns": [c["name"] for c in artifact["columns"]], "rows": artifact["rows"]}
            for name, artifact in artifacts.items()
        }

    def _artifacts(self) -> dict[str, dict[str, Any]]:
        """快照里的产物正文（内联的直接读；指针的交给数据侧既有的解析）。"""
        found: dict[str, dict[str, Any]] = {}
        for block in self.document.get("blocks") or ():
            for entry in block.get("artifacts") or ():
                if "inline" in entry:
                    found[str(entry["inline"]["name"])] = entry["inline"]
                elif self.source is not None:
                    artifact = self.source.artifact(entry["pointer"])
                    found[artifact.name] = {
                        "name": artifact.name,
                        "columns": [{"name": c.name, "dtype": c.dtype} for c in artifact.columns],
                        "rows": [dict(row) for row in artifact.rows],
                    }
        return found

    def _spec_text(self) -> str:
        if self.instruction:
            return self.instruction
        path = self.task_dir / "instruction.md"
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
        return "(no instruction was supplied)"


def _holds(left: Any, op: str, right: Any, tol: float) -> bool:
    """列级不变量的四种比较 —— 与契约声明的 `op` 一一对应。"""
    try:
        if op == "le":
            return float(left) <= float(right) + tol
        if op == "lt":
            return float(left) < float(right) + tol
        if op == "ge":
            return float(left) >= float(right) - tol
        if op == "gt":
            return float(left) > float(right) - tol
        if op == "eq":
            return float(left) == float(right) or abs(float(left) - float(right)) <= tol
        return left == right
    except (TypeError, ValueError):
        return str(left) == str(right)


def _summarise(payload: Mapping[str, Any], *, max_rows: int = 20) -> str:
    lines: list[str] = []
    for name, artifact in payload.items():
        lines.append(f"deliverable {name!r}: columns={list(artifact['columns'])} rows={len(artifact['rows'])}")
        for row in list(artifact["rows"])[:max_rows]:
            lines.append(f"  {json.dumps(row, ensure_ascii=False, default=str)}")
    return "\n".join(lines) or "(no artifacts were produced)"


def _extract_code(text: str) -> str:
    """从 worker 的回答里取那段 python —— 取不到就返回空串（由调用方具名报）。"""
    fence = "```"
    if fence not in text:
        return ""
    parts = text.split(fence)
    for index in range(1, len(parts), 2):
        block = parts[index]
        first = block.splitlines()[0].strip().lower() if block.strip() else ""
        if first in ("python", "py", ""):
            body = "\n".join(block.splitlines()[1:]) if first else block
            if "def run(" in body:
                return body
    return ""


def content_report_for_run(
    run: Any,
    *,
    task_dir: str | Path,
    client: Any = None,
    artifact_root: str | Path | None = None,
    instruction: str = "",
) -> tuple[dict[str, Any], Any]:
    """收快照 → 按契约声明的两个维度跑内容检查 → 返回 `(snapshot, content_report)`。"""
    from bobanana6.contract import parse_contract_file

    base = Path(task_dir).resolve()
    document = capture_run(run, task_dir=base, artifact_root=artifact_root)
    contract_file = str((document.get("contract_file") or {}).get("path") or "task.yaml")
    checks = parse_contract_file(base / contract_file).content_checks
    source = TaskSnapshotSource(
        document=document,
        task_dir=base,
        artifact_root=Path(artifact_root) if artifact_root is not None else None,
        bundle_object=getattr(run, "bundle", None),
    )
    executor = ContentExecutorImpl(
        task_dir=base, document=document, source=source, client=client, instruction=instruction
    )
    return document, run_content_checks(checks, task_id=str(document["task_id"]), executor=executor)
