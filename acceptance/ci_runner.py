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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bobanana6.assemble_impl.runtime import block_bundle as runtime_block_bundle
from bobanana6.contract import compile_bundle, parse_contract_file
from bobanana6.core.model import Artifact, Column, Snapshot, SnapshotBody
from bobanana6.ontology import load_ontology_file
from bobanana6.services.ci import BlockCapture, build_snapshot, run_ci
from bobanana6.sources.reader import rows_from_csv, rows_from_json

__all__ = ["SnapshotDrift", "TaskSnapshotSource", "capture_run", "ci_for_run"]


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
