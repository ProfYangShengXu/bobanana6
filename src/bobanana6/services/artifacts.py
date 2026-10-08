"""Git-backed artifact traceability: **每个块跑完 = 一个 commit**（规格 §2 第1层）。

为什么用 git 而不是自造一套痕迹格式（规格 §0.1，Cloudflare Artifacts 原话）：
**自造协议有 bootstrap 问题** —— 模型不认，你得再教它；而 git 的数据模型本来
就是"追状态 / 时间旅行 / 存大量小数据"的东西。所以这里只做两件事：

  ① 把块写过的文件提交进一个**真的 git 仓库**（作者 = 块 id，信息 = summary + verdict +
     它用的输入），于是任何产物都能 `git log -- <path>` 追出"哪块、什么时候、用什么输入"。
  ② 大产物**只提交指针**（sha256 + 字节数 + 行数 + 路径），正文进内容寻址存储（CAS）——
     我们的产物有 212KB CSV / 631KB 中间产物，直接进 git 会把仓库撑爆（规格 §0.5①）。

★ 归档不是免费的（AgentFootprint：框架残留比交付产物大几个数量级）⇒ 带**字节预算**：
超预算的正文**不进 CAS**，但**指针照样提交**（`stored: false`）。处置选"只留指针"而不是
"具名拒"：拒收会让一个块因为**归档卫生**而失败 —— 那正是这份规格要消灭的误伤；
"滚动"会删证据。只留指针保留 git 里的痕迹（路径 + 摘要 + 规模），只丢正文。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ARCHIVE_BUDGET_BYTES",
    "INLINE_MAX_BYTES",
    "ArtifactStore",
    "BlockRecord",
    "FileRecord",
]


#: 超过这个字节数的产物**不进 git 的树**：提交里只放指针（规格 §0.5①）。
INLINE_MAX_BYTES = 64 * 1024
#: 一次 run 能往 CAS 里放的总字节上限（规格 §0.5④ / AgentFootprint 的保留量教训）。
ARCHIVE_BUDGET_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class FileRecord:
    """One produced file as the trace records it."""

    path: str
    sha256: str
    bytes: int
    lines: int
    #: True = 正文进了 git 的树；False = 只提交了指针（正文在 CAS 里）。
    inline: bool
    #: False = 连 CAS 都没放（归档预算用尽）—— 痕迹仍留指针。
    # blame: fail-closed — 省略时必须说"没存"，不许默认宣称存了（改前是 True，是危险方向）
    stored: bool = False

    def as_pointer(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "lines": self.lines,
            "stored": self.stored,
        }


@dataclass(frozen=True)
class BlockRecord:
    """One block = one commit: who, when, from what inputs, with what verdict."""

    block_id: str
    commit: str
    verdict: str
    #: (输入名, 产出它的 commit sha) —— "用什么输入产出的"的确定答案。
    inputs: tuple[tuple[str, str], ...] = ()
    files: tuple[FileRecord, ...] = ()

    def file(self, path: str) -> FileRecord | None:
        for record in self.files:
            if record.path == path:
                return record
        return None

    def has(self, path: str) -> bool:
        return self.file(path) is not None


class ArtifactStore:
    """A git repo of per-block commits + a CAS for the bodies that do not fit git.

    ``root``       = 痕迹的落点（一个目录；里面自己 `git init`）。
    ``source_root``= 块真正写文件的目录（工作区）。提交时从这里读正文。
    ``tree``       = git 的工作树：`<block id>/<相对路径>`；大文件在那里是**指针 JSON**。
    """

    def __init__(
        self,
        root: Path,
        *,
        source_root: Path | None = None,
        inline_max_bytes: int = INLINE_MAX_BYTES,
        budget_bytes: int = ARCHIVE_BUDGET_BYTES,
        git: str = "git",
    ) -> None:
        self.root = Path(root)
        self.source_root = Path(source_root) if source_root is not None else None
        self.tree = self.root / "tree"
        self.cas = self.root / "cas"
        self.inline_max_bytes = inline_max_bytes
        self.budget_bytes = budget_bytes
        # ★ 预算必须是**磁盘上**的账，不是**本次进程**的账：痕迹仓库跨 run 复用
        #   （`_run/artifacts` 不随工作区清掉），只数本次的新增等于永远不到上限 ——
        #   正是 AgentFootprint 说的"框架残留越攒越多"。所以先把已有的 CAS 体量算进来。
        self.archived_bytes = self._existing_bytes()
        #: 超预算被丢掉的正文（只留指针）—— 具名记录，不静默。
        self.evicted: list[FileRecord] = []
        self._git = git
        self._records: dict[str, BlockRecord] = {}
        self._ensure_repo()

    # -- git plumbing ------------------------------------------------------- #

    # git plumbing: ``check=True`` 是保护性默认（命令失败必须报错）
    def _git_run(
        self, *args: str, check: bool = True  # blame: fail-closed — 省略 check 即"命令失败要报错"，方向保守
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            # ★ 痕迹仓库通常落在**本仓库的工作树里面**（`acceptance/**/_run/artifacts`）。
            #   本仓库挂在 Windows 挂载点上，git 对"可疑属主"会**拒答任何子命令**
            #   （连 `git config user.name` 都是 exit 128）—— 于是 `git init` 成功、
            #   紧接着的 `config` 就炸。这不是放宽权限，是仓库里其它 git 调用共用的
            #   同一条豁免（见 `acceptance/harness.py::pin_commit` 的同款注释）。
            [self._git, "-c", "safe.directory=*", *args],
            cwd=str(self.tree),
            capture_output=True,
            text=True,
            check=check,
        )

    def _ensure_repo(self) -> None:
        self.tree.mkdir(parents=True, exist_ok=True)
        if (self.tree / ".git").is_dir():
            return
        self._git_run("init", "-q")
        self._git_run("config", "user.name", "bobanana6")
        self._git_run("config", "user.email", "artifacts@bobanana6.local")

    def _existing_bytes(self) -> int:
        """CAS 里已经躺着多少正文 —— 上次 run 留下的也算进这次的预算。"""
        if not self.cas.is_dir():
            return 0
        return sum(path.stat().st_size for path in self.cas.rglob("*") if path.is_file())

    def head(self) -> str:
        result = self._git_run("rev-parse", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    # -- the per-block commit ---------------------------------------------- #

    def commit_block(
        self,
        *,
        block_id: str,
        verdict: str,
        paths: Iterable[str],
        summary: str = "",
        inputs: Sequence[tuple[str, str]] = (),
    ) -> BlockRecord:
        """Commit everything this block produced; return the record it left behind."""
        if self.source_root is None:
            raise ValueError("ArtifactStore.source_root is required to commit a block")
        files: list[FileRecord] = []
        for raw in sorted({str(path) for path in paths if str(path)}):
            record = self._stage(block_id, raw)
            if record is not None:
                files.append(record)
        manifest = {
            "block": block_id,
            "verdict": verdict,
            "inputs": [{"name": name, "commit": commit} for name, commit in inputs],
            "summary": summary,
            "files": [record.as_pointer() for record in files],
        }
        target = self.tree / block_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "commit.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        self._git_run("add", "-A")
        status = self._git_run("status", "--porcelain").stdout.strip()
        if not status:
            # 什么都没变（块没写任何东西）—— 不留空 commit，但记录仍指向 HEAD。
            commit = self.head()
        else:
            message = self._message(block_id, verdict, summary, inputs)
            self._git_run(
                "commit",
                "-q",
                "--author",
                f"{block_id} <{block_id}@bobanana6.local>",
                "-m",
                message,
            )
            commit = self.head()
        record = BlockRecord(
            block_id=block_id,
            commit=commit,
            verdict=verdict,
            inputs=tuple(inputs),
            files=tuple(files),
        )
        for entry in files:
            self._records[entry.path] = record
        return record

    def _stage(self, block_id: str, relative: str) -> FileRecord | None:
        source = self.source_root / relative  # type: ignore[operator]
        if not source.is_file():
            return None
        body = source.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        lines = len(body.decode("utf-8", errors="replace").splitlines())
        target = self.tree / block_id / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # ★ 归档预算管**所有**提交正文（inline 的与 CAS 的都算）—— 超了就只留指针。
        #   选"只留指针"而不是"具名拒"：拒收会让块因为归档卫生而失败（正是本规格要
        #   消灭的误伤）；"滚动"会删证据。指针仍进 git（路径 + 摘要 + 规模都可追）。
        record = FileRecord(relative, digest, len(body), lines, inline=False, stored=False)
        if self.archived_bytes + len(body) > self.budget_bytes:
            self.evicted.append(record)
            self._write_pointer(target, record)
            return record
        if len(body) <= self.inline_max_bytes:
            target.write_bytes(body)
            record = FileRecord(relative, digest, len(body), lines, inline=True, stored=True)
        else:
            self._archive(digest, body)
            record = FileRecord(relative, digest, len(body), lines, inline=False, stored=True)
            self._write_pointer(target, record)
        self.archived_bytes += len(body)
        return record

    @staticmethod
    def _write_pointer(target: Path, record: FileRecord) -> None:
        target.write_text(
            json.dumps(record.as_pointer(), ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _archive(self, digest: str, body: bytes) -> None:
        """Put the body in the CAS (the run's budget was checked by the caller)."""
        target = self.cas / digest[:2] / digest[2:]
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(body)

    @staticmethod
    def _message(
        block_id: str,
        verdict: str,
        summary: str,
        inputs: Sequence[tuple[str, str]],
    ) -> str:
        lines = [f"[{block_id}] verdict={verdict}"]
        # ★ 第一行必须是**独立的** subject：git 的 subject 是"第一个空行之前"的全部内容，
        #   不空行的话 `git log --oneline` 会把 inputs / summary 一起塞进那一行，
        #   "扫一眼就知道哪块产出了它"就没了。
        lines.append("")
        lines.append("inputs:")
        if inputs:
            lines.extend(f"  {name} <- {commit or '(no producing commit)'}" for name, commit in inputs)
        else:
            lines.append("  (none declared)")
        if summary.strip():
            lines.append("summary:")
            lines.extend(f"  {line}" for line in summary.strip().splitlines())
        return "\n".join(lines)

    # -- lookups ------------------------------------------------------------ #

    def record_for(self, path: str) -> BlockRecord | None:
        """The commit that last produced this path (``None`` = never traced)."""
        return self._records.get(str(path))

    def records(self) -> tuple[BlockRecord, ...]:
        return tuple(self._records.values())

    def log(self, path: str) -> tuple[tuple[str, str, str], ...]:
        """``(commit, author, subject)`` for a traced path, newest first — via real ``git log``."""
        result = self._git_run(
            "log", "--format=%H\x1f%an\x1f%s", "--", f"*/{path}", check=False
        )
        entries: list[tuple[str, str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                entries.append((parts[0], parts[1], parts[2]))
        return tuple(entries)
