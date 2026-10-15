"""★ `#47` —— 块自己的工作区工具：写文件、改文件、跑命令、跑代码。

抄的是 Hermes 的**行为契约**（`write_file` 的"整份覆盖 + 自动建父目录"、`patch` 的
"精确替换、非唯一即拒"、`terminal` 的"前台/后台 + cwd + 超时"、`execute_code` 的
"持久会话 + reset"），**不是**那四个文件的装配：task_id / 容器镜像路径 / 审批门 /
敏感路径检查 / 跨 profile 检查 / 配置缓存 / 错误节流一律不抄（见 `FORK-NOTES.md §2`）。

三条边界是本模块存在的理由，也是它敢被块拿去用的前提：

```
① 路径边界   一切路径都是【工作区相对】；绝对路径 / `..` / 盘符 / 符号链接逃逸一律具名拒。
② 写区边界   块只能写它 partition 声明的那几个路径（`write_region`）；
              写区为空 = 没有工作区权限，不是"随便写"。
③ 执行边界   `shell.run` 的 cwd 一定在工作区内；后台任务在块结束时收尸。
```

**为什么要有 `WorkspaceLedger`**：写和执行的产物不能"写完就算"——壳要在块结束时
把它们变成**可追溯的候选产物**（见 `assemble_impl/produce.py` 的通道 2 对账）。
ledger 在**第一次**动工作区之前拍一张前后对照的"之前"（懒拍：没用到通道 2 的块
一个字节都不多读），块结束时 diff 出"新增/改动的文件"。这样 `files.write` 与
`shell.run` 里 `>` 重定向产生的文件走的是**同一条**记录路径，不存在"后者没人管"。
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import count, pairwise
from pathlib import Path
from typing import Any

from .textops import DEFAULT_LIMIT, line_window

__all__ = [
    "BoundWorkspace",
    "ReadOutsideRegion",
    "WorkspaceError",
    "WorkspaceLedger",
    "WorkspaceUnavailable",
    "WriteBudget",
    "WriteBudgetExceeded",
    "WriteOutsideRegion",
    "may_write",
    "relative_parts",
]


#: 单个窗口/单次写入的字节上限（与 `produce.MAX_CODE_BYTES` 同一量级：越过它就该分次写）。
MAX_WRITE_BYTES = 200_000
#: ★ 一块能往工作区写的**总字节上限**（默认 64 MiB）。
#: 写区规则从"不在写区里就拒"改成"只拒撞到别人的东西"之后，旧规则顺带防着的
#: "块乱写堆满盘"就没人管了 —— 这个常量就是那道替补闸。默认值只在这里写一次，
#: 上限通过 `WriteBudget(limit=...)` 从外部传（测试给一个小上限不必改函数体）。
_WORKSPACE_WRITE_BUDGET_BYTES = 64 * 1024 * 1024
#: 一条命令默认/最长跑多久（秒）。超时是**具名**结果，不是静默截断。
#: ★ 这两个数**同时写进 `core/ports.py` 的 `shell.run` 描述**（模型必须知道默认只有 60s）。
DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 600.0
#: ★★ 输出"换页"预算（`shell.run`）：被截时**头尾各留一段**，中间省略并明说省了多少。
#: 头看命令回显 / 表头 / 前几行数据，尾看报错 / 最终值 / traceback —— 两头都有人要。
#: 行数与字节数各一组上限，"先到者为准"。★ 只留头会看不见报错，只留尾会看不见表结构。
SHELL_OUTPUT_HEAD_LINES = 40
SHELL_OUTPUT_HEAD_BYTES = 4 * 1024
SHELL_OUTPUT_TAIL_LINES = 200
SHELL_OUTPUT_TAIL_BYTES = 8 * 1024
#: ★ 完整输出（stdout+stderr）在被截时落盘的目录（工作区相对）。
#: ★★ 它**不是产物**：进了 `IGNORED_PARTS` ⇒ `_digests` 看不见它 ⇒ ledger 的 `changed()`
#:   不含它 ⇒ 通道 2 的对账（`_reconcile_workspace`）不会把它收成候选 —— 照 `_code/` 的先例。
#: ★★ 这条排除规则现在同时覆盖 `shell.run` 的全量输出与**工具返回换页**的全量落盘
#:   （`BoundWorkspace.spill_tool_output`）：两者的"不是产物"只有一个定义处。
SHELL_OUTPUT_DIR = "_shell"
#: 落盘文件里 stdout 与 stderr 的分界：两股流各存各的，合到一份文件里要能分清。
SHELL_OUTPUT_STDERR_DIVIDER = "\n----- stderr -----\n"
#: ★ 落盘文件名里的**进程内**序号。同一个 run 里所有块共用一个工作区，序号必须全局单调，
#: 否则两个块各自从 1 开始，后落的那份会盖掉前一份（只读审计 #65 的同一形态）。
_SHELL_OUTPUT_SEQ = count(1)
#: ★ 再加一段**每次进程启动**都不同的随机尾巴：工作区跨 run 是持久目录，容器 PID 命名空间里
#: 两次 run 可能拿到同一个 pid ⇒ 只靠 `<pid>-<seq>` 会让后一次覆盖前一次，旧 `full_output_path`
#: 就指向了新内容（审计 #111）。
_SHELL_OUTPUT_RUN = f"{os.getpid()}-{os.urandom(3).hex()}"
#: ★ 工具返回换页落盘的**进程内**序号（与 `_SHELL_OUTPUT_SEQ` 同一条纪律，
#:   但两股落盘各自编号，任一方的编号都不依赖另一方是否被用过）。
_TOOL_OUTPUT_SEQ = count(1)
#: 扫描工作区时忽略的目录名：编译/测试缓存的副产品不是产物，也不是越权写。
IGNORED_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", SHELL_OUTPUT_DIR}
)
IGNORED_SUFFIXES = (".pyc", ".pyo")


class WorkspaceError(RuntimeError):
    """A workspace operation the caller caused -- refuse by name, never guess.

    The caller named the path/command, so the declaring layer is the model.
    """

    blame = "model"


class WorkspaceUnavailable(WorkspaceError):
    """The deployment cannot serve this at all (no workspace / cannot start a process).

    Not the caller's fault and not fixable by re-asking: repair the tool/host.
    """

    blame = "tool"


class WriteOutsideRegion(WorkspaceError):
    """The path is not in this block's write region (or the block has none)."""


class ReadOutsideRegion(WorkspaceError):
    """The path is not in this block's **readable** region.

    ★ A read refusal must not reuse ``WriteOutsideRegion``: reporting "you may not
    write" for a failed read makes the caller (and attribution) fix the wrong
    layer. The rule is the same one -- a block may read exactly what it may write
    (``may_write``) -- only the name and the verb in the message differ.
    """


class WriteBudgetExceeded(WorkspaceError):
    """This block has written more than its total workspace write budget."""


def relative_parts(path: str) -> tuple[str, ...]:
    """Validate a caller-supplied path and return its parts (relative only)."""
    if not isinstance(path, str) or not path.strip():
        raise WorkspaceError("path must be a non-empty workspace-relative string")
    raw = path.strip().replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise WorkspaceError(f"path {path!r} must be relative to the workspace, not absolute")
    parts = tuple(part for part in raw.split("/") if part not in ("", "."))
    if not parts:
        raise WorkspaceError(f"path {path!r} names the workspace root, not a file")
    if any(part == ".." for part in parts):
        raise WorkspaceError(f"path {path!r} escapes the workspace")
    return parts


def _normalized(path: str) -> str:
    return "/".join(relative_parts(path))


def may_write(path: str, write_region: tuple[str, ...], frozen_paths: tuple[str, ...] | None) -> bool:
    """这块能不能写这个路径。★ 三处调用点必须走同一个判定，否则换个工具就换个规矩。

    ``write_region`` 是本块 partition 声明的那几个路径（自己那份，永远可以写）；
    ``frozen_paths`` 是"谁都不许写"的路径（别人的交付物 + 任务声明的来源）。
    ``frozen_paths is None`` = 调用方**没有声明**别人的东西是哪些 ⇒ 退回旧规则，
    只有写区内的路径可以写（fail-closed：不知道就别放开）。
    其余一律放行 —— 那是块自己的草稿纸。
    """
    normalized = _normalized(path)
    allowed = {_normalized(name) for name in write_region}
    if normalized in allowed:
        return True
    if frozen_paths is None:
        return False
    return normalized not in {_normalized(name) for name in frozen_paths}


@dataclass
class WriteBudget:
    """本块在工作区里的**净占用**闸 —— 一道闸，多个写入口共用。

    ★ 判定看的是**当前占用**（本块写过的路径现在在磁盘上的字节和），**不是**累计写入量：
    "堆满盘" = 净占用。一个块写了 1GB 又删掉、或合法地反复覆写同一个大文件，都不该被拒
    —— 按累计判会误伤后者（高精度低召回：宁可漏，不可错杀）。
    ``written_total`` 只作**证据**（诊断"它在反复写什么"），不参与判定。

    ``limit`` 默认取 `_WORKSPACE_WRITE_BUDGET_BYTES`；要从外部改上限就传
    ``WriteBudget(limit=...)``，函数体里不写死数字。
    """

    limit: int = _WORKSPACE_WRITE_BUDGET_BYTES  # blame: fail-closed — 省略即给一个有上限的写区预算（64 MiB），方向保守；数值由壳定而不是由契约声明，已在注释里说明
    #: 本块写过的路径（去重）：判定看的是它们**现在**在磁盘上的字节和。
    paths: set[str] = field(default_factory=set)
    #: 累计写入量 —— 只作证据，不参与判定。
    written_total: int = 0
    #: 只给副作用对账用一次观察量的路径（同一份文件不再按当前大小重复记账）。
    _observed: set[str] = field(default_factory=set)

    def note_write(self, path: str) -> None:
        """Remember that this block wrote ``path`` (idempotent)."""
        self.paths.add(_normalized(path))

    def note_bytes(self, amount: int) -> None:
        """Add to the cumulative-written evidence (never used for the decision)."""
        self.written_total += max(0, amount)

    def note_observed(self, path: str, size: int) -> None:
        """Record a file the ledger saw change, once (its size is an approximation)."""
        normalized = _normalized(path)
        self.paths.add(normalized)
        if normalized in self._observed:
            return
        self._observed.add(normalized)
        self.written_total += max(0, size)

    def _size(self, root: Path | None, name: str) -> int:
        if root is None:
            return 0
        target = Path(root) / name
        try:
            return target.stat().st_size if target.is_file() else 0
        except OSError:  # a file that vanished mid-scan is not a current occupant
            return 0

    def occupancy(self, root: Path | None) -> int:
        """本块写过的路径**现在**在磁盘上的字节和（写过的又删了的 = 0）。"""
        return sum(self._size(root, name) for name in self.paths)

    def project(self, root: Path | None, path: str, *, new_size: int) -> int:
        """这次写完之后本块的净占用预测（写之前算，好早拒）。

        已经在本块账上的路径：用新大小替换它当前的大小；不在账上的：整份都是新增
        （哪怕是覆写一份预先存在的文件 —— 改动之后它也算本块的足迹）。
        """
        normalized = _normalized(path)
        current = self.occupancy(root)
        if normalized in self.paths:
            return current - self._size(root, normalized) + max(0, new_size)
        return current + max(0, new_size)

    def ensure(self, total: int, *, where: str, attempted: int | None = None) -> None:
        """Refuse by name once the block's (projected) occupancy is over the limit."""
        if total <= self.limit:
            return
        detail = (
            f"this block would occupy {total} bytes in the workspace; the limit is {self.limit} bytes"
        )
        if attempted is not None:
            detail += f", and this write adds {attempted} more"
        detail += f" (it has written {self.written_total} bytes in total)"
        raise WriteBudgetExceeded(f"{where}: {detail}")


@dataclass
class BoundWorkspace:
    """The block's working area, bound to one block and one write region.

    ``root is None`` means the deployment declared no workspace; the toolset
    never advertises these tools in that case, and every entry point refuses
    rather than silently writing somewhere else.
    """

    root: Path | None
    write_region: tuple[str, ...] = ()
    #: ★★ 谁都不许写的路径：别的块的交付物 + 任务声明的来源（见 `_check_region`）。
    #: **`None` = 没有声明 ⇒ 退回旧规则（不在写区里就拒）** —— 默认必须 fail-closed：
    #: 一个不知道"别人的东西是哪些"的调用方，不该因此获得"随便写"的权限。
    #: `runtime` 会显式传入它，那才是放宽的唯一入口。
    frozen_paths: tuple[str, ...] | None = None
    ledger: WorkspaceLedger | None = None
    #: ★ 本块的总写入字节闸（见 `WriteBudget`）。默认一块一份；`runtime` 会把这个对象
    #: 同时交给答案 `files` 通道，于是"写工具写的 + 答案写写的"在同一本账上累加。
    write_budget: WriteBudget = field(default_factory=WriteBudget)
    #: Long-lived helpers owned by this block (background jobs).
    _background: list[subprocess.Popen[bytes]] = field(default_factory=list)
    #: 完整输出落盘失败过几次（**如实报**，不假装存了；见硬边界⑦：落盘是辅助不是前置门）。
    _shell_spill_failures: list[str] = field(default_factory=list)  # blame: whitelist — 只是"失败过什么"的证据清单，缺省空 = 没失败过

    # -- boundary helpers -------------------------------------------------- #

    def _require_root(self) -> Path:
        if self.root is None:
            raise WorkspaceUnavailable("this deployment has no workspace; file tools cannot run")
        root = Path(self.root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _inside(self, root: Path, path: str) -> Path:
        """Resolve ``path`` inside ``root``, refusing anything that escapes it.

        ``realpath`` both sides so a symlink planted inside the workspace cannot
        be used to step outside it (the same rule `agentloop/sandbox.py` uses).
        """
        candidate = Path(os.path.realpath(root.joinpath(*relative_parts(path))))
        if candidate != root and root not in candidate.parents:
            raise WorkspaceError(f"path {path!r} escapes the workspace")
        return candidate

    def _check_region(
        self,
        path: str,
        *,
        verb: str = "write",  # blame: fail-closed — 省略即写报文（既有三处调用点逐字不变）
        error: type[WorkspaceError] = WriteOutsideRegion,  # blame: fail-closed — 省略即写失败；读失败必须显式传 ReadOutsideRegion
    ) -> None:
        """★ 写区互斥的执行点：**撞到别人的东西**才拒（空区 ≠ 没有权限）。

        ★★ 原来这里是"不在本块 partition 里的路径一律不许写" —— 太严：块连临时文件
        都建不了（实测 `handoff` 轮 T-02 写 `_tmp_answer.json` 被拒，只能塞一小撮样品
        进 `rows` 被判"不全"）。写区互斥要防的是"别撞别人的交付物/来源"，
        **不是"只许碰自己那一份"**。规则与 `produce.py::_reconcile_workspace` /
        `_persist_files` 一致 —— 三处不一样的话，换个工具就换个规矩。

        ★ `read()` 走的是**同一条**判定（`may_write`），只在失败时换一个具名异常：
        读失败报"你不能写"会让归因读错层（见 `ReadOutsideRegion`）。既有调用点的
        默认值（`verb="write"` / `WriteOutsideRegion`）使它们的报文**逐字不变**。
        """
        if may_write(path, self.write_region, self.frozen_paths):
            return
        allowed = sorted({_normalized(name) for name in self.write_region})
        raise error(f"block may not {verb} {_normalized(path)!r}: its write region is {allowed!r}")

    def _note(self) -> None:
        if self.ledger is not None:
            self.ledger.note_use()

    # -- the tool surface --------------------------------------------------- #

    def write(self, path: str, content: str, append: bool = False) -> Mapping[str, Any]:
        """Whole-file overwrite (Hermes `write_file`), creating parent directories.

        ★ One deliberate extension to that contract: ``append=True`` appends to
        the file instead of replacing it. It exists because the symptom this
        block is here to fix is *"the deliverable does not fit one answer"* --
        the Hermes contract alone leaves no honest way to write a file in
        several calls. Same write region, same ledger, same reconciliation.
        """
        if not isinstance(content, str):
            raise WorkspaceError(f"content must be text, got {type(content).__name__}")
        size = len(content.encode("utf-8"))
        if size > MAX_WRITE_BYTES:
            raise WorkspaceError(
                f"content is {size} bytes, over the {MAX_WRITE_BYTES}-byte "
                "limit for one call -- write the file in several calls (append=true)"
            )
        self._check_region(path)
        self._note()
        root = self._require_root()
        target = self._inside(root, path)
        old_size = target.stat().st_size if target.is_file() else 0
        projected = self.write_budget.project(root, path, new_size=old_size + size if append else size)
        # ★ 早拒比晚拒好：按"这次写完之后本块的**净占用**"预检（累计会误伤反复覆写同一个大文件）。
        self.write_budget.ensure(projected, where=f"files.write({path!r})", attempted=size)
        self.write_budget.note_write(path)
        self.write_budget.note_bytes(size)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with target.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        return _written(root, target)

    def read(
        self,
        path: str,
        offset: int | str = 0,  # blame: fail-closed — 省略即从头读，一次仍是**一窗**（不整份灌上下文）
        limit: int | str = DEFAULT_LIMIT,  # blame: fail-closed — 省略即一页（200 行），不整份灌上下文
    ) -> Mapping[str, Any]:
        """Read back a file this block is allowed to write (a numbered line window).

        ★ Why this exists (measured, not hypothesised): a block that must write its
        deliverable in several passes has **no way to read back** what it wrote --
        `sources.*` only resolves authorized sources, `traces.*` only reads process
        records. The 4B T-01 run wrote `output/jd-matrix.md`, tried
        `sources.read` on it (PermissionError), and then spent 33 `shell.run`
        round-trips on `cat`/`grep -c` to re-read its own draft. That is the
        shell's debt, not the model's.

        ★ The permission rule is the **same predicate** as writing (`may_write`):
        a block may read exactly what it may write -- on a deployment that declares
        `frozen_paths`, that is its deliverable plus any non-frozen path (the same
        set the write tools accept, including its own scratch files), and **never**
        the frozen paths (another block's deliverable or a task-declared source).
        This is a genuine read, not a back door.

        ★ It is **not** "reading the sources": `produce.LoopIOImpl.read_sources`
        only flips on a successful `sources.*` call, so re-reading one's own draft
        never satisfies read-before-produce.

        ★ Output is the existing numbered page (`textops.line_window`, the same one
        `sources.read` uses): `offset` is a 0-based line offset, `limit` a line
        count, and every line carries its 1-based number. No new pagination.

        ★★ The region is checked **twice**: once on the name the caller gave, and
        once on the **real** path `_inside` resolved it to. Without the second
        check a symlink planted inside the write region (`mine.csv -> other/out.csv`)
        would make a frozen path readable by alias -- the same `validatePath`
        realpath re-check MCP's filesystem server does. A read must not be a back
        door to a frozen path even when the name looks allowed.
        """
        self._check_region(path, verb="read", error=ReadOutsideRegion)
        root = self._require_root()
        target = self._inside(root, path)
        # ★ realpath re-check（同一条 `may_write`，判的是**真实相对路径**）。
        self._check_region(_relative(root, target), verb="read", error=ReadOutsideRegion)
        if not target.is_file():
            raise WorkspaceError(f"cannot read {path!r}: no such file in the workspace")
        raw = target.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Name the failure (blame=model): the caller picked a binary carrier,
            # this is not a broken shell. Same reasoning as `patch_edits`.
            raise WorkspaceError(f"cannot read {path!r}: it is not valid UTF-8 text ({exc})") from None
        window = line_window(text, offset=offset, limit=limit)
        return {
            "path": _normalized(path),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            **dict(window),
        }

    def patch(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> Mapping[str, Any]:
        """Exact text replacement; a non-unique match is refused (Hermes `patch`)."""
        if not isinstance(old_string, str) or old_string == "":
            raise WorkspaceError("old_string must be a non-empty string")
        if not isinstance(new_string, str):
            raise WorkspaceError(f"new_string must be text, got {type(new_string).__name__}")
        self._check_region(path)
        self._note()
        root = self._require_root()
        target = self._inside(root, path)
        if not target.is_file():
            raise WorkspaceError(f"cannot patch {path!r}: no such file in the workspace")
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old_string)
        if occurrences == 0:
            raise WorkspaceError(f"cannot patch {path!r}: old_string does not appear in the file")
        if occurrences > 1 and not replace_all:
            raise WorkspaceError(
                f"cannot patch {path!r}: old_string appears {occurrences} times; "
                "give a longer unique old_string or set replace_all=true"
            )
        replacements = occurrences if replace_all else 1
        # ★ 净占用：只按改过之后的那份文件算（缩水会退还，涨出来的才算）。
        delta = replacements * (len(new_string.encode("utf-8")) - len(old_string.encode("utf-8")))
        old_size = target.stat().st_size
        projected = self.write_budget.project(root, path, new_size=max(0, old_size + delta))
        self.write_budget.ensure(projected, where=f"files.patch({path!r})", attempted=max(0, delta))
        self.write_budget.note_write(path)
        self.write_budget.note_bytes(max(0, delta))
        target.write_text(text.replace(old_string, new_string), encoding="utf-8")
        return {**_written(root, target), "replacements": replacements}

    def patch_edits(self, path: str, edits: Any) -> Mapping[str, Any]:
        """★ 定点修改（多条）：每条对【原文件】匹配、唯一、互不重叠，整体原子。

        语义照 flash 的 `edit`（`设计文档-bobanana6_flash-20261008.md §2.3` /
        `bobanana6_flash/src/bobanana6_flash/tools/edit.py`）：

        ```
        ① 用【原文】匹配，不用行号 —— 行号会漂，文本不会漂
        ② 每条 oldText 必须在原文件里【恰好出现一次】（0 次 = 找不到，>1 次 = 不唯一）
        ③ 多条各自对【原文件】匹配，彼此【不许重叠】（重叠就拒，不做合并语义）
        ④ 原子：任何一条不通过 ⇒ 一个字节都不落盘
        ⑤ 失败【具名说清】：哪一条 / 期望什么 / 文件里那一段附近实际是什么
        ```

        ★ 与 `patch()` 走**同一条**边界：`_check_region`（写区/冻结）+ `_note`（ledger）
          + `WriteBudget`。所以它既不是绕过写区的后门，也不会漏记 —— 块结束时
          `_reconcile_workspace` 照样把它 diff 成候选产物。

        ★ `edits` 接受真数组，也接受一段 JSON 文本（宽松端点 / 模型偶发把数组写成字符串）。
        """
        if isinstance(edits, str):
            try:
                edits = json.loads(edits)
            except json.JSONDecodeError as exc:
                raise WorkspaceError(
                    f"edits must be an array of {{oldText, newText}}, got a string that is not JSON: {exc}"
                ) from None
        if not isinstance(edits, Sequence) or isinstance(edits, (str, bytes)) or not edits:
            raise WorkspaceError(
                f"edits must be a non-empty array of {{oldText, newText}} objects, got {type(edits).__name__}"
            )

        self._check_region(path)
        self._note()
        root = self._require_root()
        target = self._inside(root, path)
        if not target.is_file():
            raise WorkspaceError(f"cannot patch {path!r}: no such file in the workspace")
        raw = target.read_bytes()
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # ★ 不具名的 `UnicodeDecodeError` 会冒到 registry 的通用兜底，`cause` 变成空
            #   （归因落到"无人声明"）。这里改成具名的 `WorkspaceError`（blame=model）：
            #   是调用方挑错了文件（xlsx / 图片 / GBK csv），不是壳坏了。
            raise WorkspaceError(f"cannot patch {path!r}: it is not valid UTF-8 text ({exc})") from None
        # ★★ "只改那几处" = **文件里其它字节一个都不动**。CRLF 载体必须原样保住：
        #   `Path.read_text/write_text` 的 universal newline 会把整份文件的行尾从 CRLF
        #   翻成 LF —— 只改一行却动了全文（判定器只看见整份 sha256 变了，而事件流里
        #   没有行号，事后无法定位）。所以：匹配在 LF 化副本上做（oldText 写 `\n` 即可），
        #   落盘前再按原样换回 CRLF。
        crlf = "\r\n" in decoded
        original = decoded.replace("\r\n", "\n") if crlf else decoded

        spans: list[tuple[int, int, str, int]] = []
        for index, raw in enumerate(edits):
            if not isinstance(raw, Mapping):
                raise WorkspaceError(
                    f"edits[{index}] must be an object {{oldText, newText}}, got {type(raw).__name__}"
                )
            old = raw.get("oldText")
            new = raw.get("newText")
            if not isinstance(old, str) or old == "":
                raise WorkspaceError(
                    f"edits[{index}].oldText must be a non-empty string (there is nothing to find otherwise); "
                    f"got {dict(raw)!r}"
                )
            if not isinstance(new, str):
                raise WorkspaceError(f"edits[{index}].newText must be text, got {type(new).__name__}")
            occurrences = original.count(old)
            if occurrences == 0:
                raise WorkspaceError(
                    f"edits[{index}].oldText was not found in {path!r}.\n"
                    f"  expected (what you asked to replace): {_clip(old)}\n"
                    f"  got (what is actually there, near its first line): {_nearby(original, old)}"
                )
            if occurrences > 1:
                raise WorkspaceError(
                    f"edits[{index}].oldText appears {occurrences} times in {path!r} "
                    "(not unique; this tool will not guess which one).\n"
                    "  expected: a span that occurs exactly once -- include the line above/below to make it unique\n"
                    f"  got: {_clip(old)}  <- {occurrences} matches"
                )
            position = original.index(old)
            spans.append((position, position + len(old), new, index))

        spans.sort()
        for (start_one, end_one, _new_one, index_one), (start_two, _end_two, _new_two, index_two) in pairwise(spans):
            if start_two < end_one:
                raise WorkspaceError(
                    f"edits[{index_one}] and edits[{index_two}] match overlapping spans "
                    f"(offsets {start_one}-{end_one} and {start_two}).\n"
                    "  expected: non-overlapping replacements, each matched against the ORIGINAL file\n"
                    "  got: two spans that intersect -- merge them into one edit"
                )

        # ★ 全部校验通过才动盘（原子）：先在内存里算完**最终全文**，再**单次**写。
        #   账本与 `patch()` 同一套：净占用先过闸。
        pieces: list[str] = []
        cursor = 0
        for start, end, new, _index in spans:
            pieces.append(original[cursor:start])
            pieces.append(new)
            cursor = end
        pieces.append(original[cursor:])
        final_text = "".join(pieces)
        encoded = (final_text.replace("\n", "\r\n") if crlf else final_text).encode("utf-8")
        delta = len(encoded) - len(raw)
        projected = self.write_budget.project(root, path, new_size=max(0, len(encoded)))
        self.write_budget.ensure(projected, where=f"files.patch({path!r})", attempted=max(0, delta))
        self.write_budget.note_write(path)
        self.write_budget.note_bytes(max(0, delta))
        target.write_bytes(encoded)
        return {**_written(root, target), "replacements": len(spans)}

    def run(
        self,
        command: str,
        *,
        background: bool = False,
        timeout: float | str | None = None,
        workdir: str = "",
    ) -> Mapping[str, Any]:
        """Run a shell command with its cwd **inside** the workspace (Hermes `terminal`).

        ★ Output over budget is **paged, not destroyed**: the body keeps a head + tail
        preview that says how many lines/bytes were dropped, and the full stdout+stderr
        is spilled to `full_output_path` (workspace-relative, excluded from deliverables).
        ★ A timeout kills the **whole process tree**, not just the shell (`start_new_session`
        + `killpg`) -- the shell's grandchildren are what otherwise survive.
        """
        if not isinstance(command, str) or not command.strip():
            raise WorkspaceError("command must be a non-empty string")
        self._note()
        root = self._require_root()
        cwd = self._inside(root, workdir) if workdir else root
        if not cwd.is_dir():
            cwd.mkdir(parents=True, exist_ok=True)
        seconds = _timeout(timeout)
        if background:
            process = subprocess.Popen(
                command, shell=True, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            self._background.append(process)
            return {"background": True, "pid": process.pid, "cwd": str(cwd), "command": command}
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # ★ 自成进程组（组长 = 这个 shell）：超时才好 `killpg` 收整棵树（审计 #32）。
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            _reap(process)  # 收尸：不留僵尸，也**绝不**等逃逸出进程组的后代
            raise
        stdout = stdout or ""
        stderr = stderr or ""
        out_head, out_omitted, out_tail = _head_tail_page(stdout)
        err_head, err_omitted, err_tail = _head_tail_page(stderr)
        truncated = out_omitted is not None or err_omitted is not None
        if not truncated:
            # ★ 反向：没超预算 ⇒ **逐字原样**，连键序都与改前一致（值一个不动、不落盘）。
            return {
                "background": False,
                "exit_code": process.returncode,
                "cwd": str(cwd),
                "stdout": stdout,
                "stderr": stderr,
                "truncated": False,
            }
        # ★★ 截断时元数据键排在正文**之前**：上下文压缩按**头部**截 2000 字符
        #   （`agentloop/compress.py::truncate_tool_results`），排在几 KB 预览之后的
        #   `full_output_path` 会被整段砍掉（上一轮 #71 的同一形态）。键序 = JSON 字面顺序。
        full = _full_shell_output(stdout, stderr)
        where = self._spill_shell_output(full)
        if not where:
            # ★★ 落盘失败就**不销毁**：宁可把全量正文给它，也不留一份"截了却取不回"的输出
            #    （照上一个回合删掉的那个"先落盘再跑"工具的同一守卫）。如实报错，命令照跑（硬边界⑦）。
            return {
                "background": False,
                "exit_code": process.returncode,
                "cwd": str(cwd),
                "stdout": stdout,
                "stderr": stderr,
                "truncated": False,
                "full_output_error": self._shell_spill_failures[-1],
            }
        answer: dict[str, Any] = {
            "background": False,
            "exit_code": process.returncode,
            # ★★ 绝对路径，不是 `"."`（实测：模型靠它定位自身时得到的 `"."` 毫无信息）：
            #   每一轮返回都给模型一次校准机会 —— "我现在站在哪里"。不改任何判据。
            "cwd": str(cwd),
            "truncated": True,
            "full_output_path": where,
            "full_output_lines": len(full.splitlines()),
            "full_output_bytes": len(full.encode("utf-8")),
        }
        answer["stdout"] = _render_page(out_head, out_omitted, out_tail, where)
        answer["stderr"] = _render_page(err_head, err_omitted, err_tail, where)
        return answer

    def _spill_shell_output(self, full: str) -> str:
        """把完整 stdout+stderr **原样**写到 `_shell/`，返回工作区相对路径；失败返回 `""`。

        `SHELL_OUTPUT_DIR` 在 `IGNORED_PARTS` 里 ⇒ `_digests` 看不见它 ⇒ ledger 的
        `changed()` 不含它 ⇒ 通道 2 的对账不会把它收成候选产物（照 `_code/` 的先例）。
        """
        relative = f"{SHELL_OUTPUT_DIR}/out-{_SHELL_OUTPUT_RUN}-{next(_SHELL_OUTPUT_SEQ):04d}.log"
        try:
            target = self._require_root() / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(full.encode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            self._shell_spill_failures.append(f"{type(exc).__name__}: {exc}")
            return ""
        # ★ 只作**证据**记账，不过 `WriteBudget` 的闸 —— 落盘不许把命令拦下来。
        self.write_budget.note_bytes(len(full.encode("utf-8")))
        return relative

    def spill_tool_output(self, call_id: str, text: str) -> str:
        """把一条超阈值的工具返回**原样**落盘，返回工作区相对路径；失败返回 `""`。

        与 `_spill_shell_output` 同一个忽略区（`SHELL_OUTPUT_DIR` ∈ `IGNORED_PARTS`）
        ⇒ 落盘文件**不是产物**：`_digests` 看不见它 ⇒ ledger 的 `changed()` 不含它
        ⇒ 通道 2 的对账（`_reconcile_workspace`）不会把它收成候选，也不会在
        `frozen_paths=None` 的部署里被判 `write_outside_region`。

        ★ 文件名同时带 `call_id`（给人看的定位）、`_SHELL_OUTPUT_RUN`（进程内唯一，
          防跨 run 覆盖）与全局单调序号（防同 run 内跨块/跨轮覆盖 —— `call_id` 只保证
          响应内唯一，多块共工作区时会重复）。
        ★ 落盘失败**不抛**：调用方（`agentloop/tool_output.py`）据 `""` 决定"宁可不换页"。
        """
        stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in call_id)[:24] or "out"
        relative = f"{SHELL_OUTPUT_DIR}/tool-{stem}-{_SHELL_OUTPUT_RUN}-{next(_TOOL_OUTPUT_SEQ):04d}.txt"
        try:
            target = self._require_root() / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return ""
        # ★ 与 shell 落盘同一条：只作**证据**记账，不过 `WriteBudget` 的闸。
        self.write_budget.note_bytes(len(text.encode("utf-8")))
        return relative

    # -- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        """Kill every background job -- no orphan processes."""
        for process in self._background:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self._background.clear()


def _timeout(raw: float | str | None) -> float:
    if raw in (None, ""):
        return DEFAULT_TIMEOUT_S
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        raise WorkspaceError(f"timeout must be a number of seconds, got {raw!r}") from None
    if value <= 0:
        raise WorkspaceError(f"timeout must be > 0, got {value}")
    if value > MAX_TIMEOUT_S:
        raise WorkspaceError(f"timeout must be <= {MAX_TIMEOUT_S}s, got {value}")
    return value


def _full_shell_output(stdout: str, stderr: str) -> str:
    """stdout + stderr 合成一份落盘文本；两股流之间给一条显式分界（合到一份要能分清）。

    ★ 只有一股流时**原样**返回它（不加分界）：`full_output_lines` 是给模型看的规模读数，
      给 stderr-only 的输出凭空加一行分界会把 5000 行报成 5002（只读审计 #114）。
    """
    if not stderr:
        return stdout
    if not stdout:
        return stderr
    return f"{stdout}{SHELL_OUTPUT_STDERR_DIVIDER}{stderr}"


def _kill_tree(process: subprocess.Popen[str]) -> None:
    """超时收**整棵进程树**（照 flash `_kill_tree` 的形）。

    Popen 带 `start_new_session=True` ⇒ 子进程自己就是组长；`killpg` 一次收掉 shell 与
    它的孙进程。进程组已经不在了（`ProcessLookupError`）或没权限（`PermissionError`）时，
    退回杀单进程；两次都落空也只是"它已经没了"，绝不能因此盖掉 TimeoutExpired。
    `os.killpg` 在 Windows 上不存在（`AttributeError`），同样走退回路径。
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        return
    except (OSError, AttributeError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def _reap(process: subprocess.Popen[str]) -> None:
    """超时后收尸，但**绝不阻塞在逃逸出进程组的后代身上**（只读审计 #105）。

    `setsid` / daemonize 的孙进程脱离了我们的进程组，`killpg` 收不到它；而它还握着
    stdout/stderr 的写端 ⇒ 裸 `communicate()` 会一直等 EOF，把整块**挂死**（连
    `TimeoutExpired` 都来不及抛）。`wait` 只盯**直接子进程**，收完就把管道这一端关掉，
    从此不再等那个逃逸的写者。
    """
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _fit_head(text: str, max_lines: int, max_bytes: int) -> str:
    """从 `text` 的**开头**取一段（字节、行数两个上限，先到者为准）。"""
    raw = text.encode("utf-8")
    head = raw[:max_bytes].decode("utf-8", errors="ignore") if len(raw) > max_bytes else text
    lines = head.splitlines(keepends=True)
    return "".join(lines[:max_lines]) if len(lines) > max_lines else head


def _fit_tail(text: str, max_lines: int, max_bytes: int) -> str:
    """从 `text` 的**末尾**取一段（字节、行数两个上限，先到者为准）。"""
    raw = text.encode("utf-8")
    tail = raw[-max_bytes:].decode("utf-8", errors="ignore") if len(raw) > max_bytes else text
    lines = tail.splitlines(keepends=True)
    return "".join(lines[-max_lines:]) if len(lines) > max_lines else tail


def _head_tail_page(text: str) -> tuple[str, tuple[int, int] | None, str]:
    """把一段输出换页成「头 + 尾」：返回 `(头, 省略读数或 None, 尾)`。

    ★ 为什么头尾都留（而不是照 flash `bash` 只留尾）：bb6 的任务里"先看表头 / 前几行数据"
      与"看末尾报错"一样常见，只留尾会让它连表结构都看不到。中间那段**可见地**省略，
      并给出行数/字节数读数 —— 截断必须看得见（上一轮 #71 的教训）。
    """
    if not text:
        return text, None, ""
    total_bytes = len(text.encode("utf-8"))
    total_lines = len(text.splitlines())
    if total_lines <= SHELL_OUTPUT_HEAD_LINES + SHELL_OUTPUT_TAIL_LINES and total_bytes <= (
        SHELL_OUTPUT_HEAD_BYTES + SHELL_OUTPUT_TAIL_BYTES
    ):
        return text, None, ""
    head = _fit_head(text, SHELL_OUTPUT_HEAD_LINES, SHELL_OUTPUT_HEAD_BYTES)
    # ★ 尾从头之后的**剩余**里取：保证两段不重叠（否则同一段会出现两次）。
    tail = _fit_tail(text[len(head):], SHELL_OUTPUT_TAIL_LINES, SHELL_OUTPUT_TAIL_BYTES)
    if len(head) + len(tail) >= len(text):
        return text, None, ""
    kept_lines = len(head.splitlines()) + len(tail.splitlines())
    omitted_lines = max(0, total_lines - kept_lines)
    omitted_bytes = max(0, total_bytes - len(head.encode("utf-8")) - len(tail.encode("utf-8")))
    return head, (omitted_lines, omitted_bytes), tail


def _render_page(head: str, omitted: tuple[int, int] | None, tail: str, where: str) -> str:
    """把省略读数与全量路径插进头尾之间 —— 截断必须**可见**。"""
    if omitted is None:
        return head
    lines, byte_count = omitted
    marker = (
        f"…[output truncated: omitted {lines} lines / {byte_count} bytes; "
        f"full stdout+stderr at {where}]…"
    )
    before = "" if (not head or head.endswith("\n")) else "\n"
    after = "" if (not tail or tail.startswith("\n")) else "\n"
    return f"{head}{before}{marker}{after}{tail}"


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _clip(text: str, limit: int = 300) -> str:  # blame: fail-closed — 省略即给错误报文一个有上限的短摘要，不会把整份文件倒进上下文
    """一段原文的短摘要（失败报文里给模型看的东西，不倾泻整份文件）。"""
    return text if len(text) <= limit else text[:limit] + "…"


def _nearby(original: str, old: str) -> str:
    """文件里**你说要改的那段附近**实际是什么（照 flash `edit.py::_nearby`）。

    先按 `old` 的第一行（去掉首尾空白）定位；定位不到就退回文件开头几行。
    失败报文没有它，模型只能盲重试 —— 这正是不具名报错最贵的地方。
    """
    head = old.split("\n")[0].strip()
    if head and head in original:
        index = original.index(head)
        return _clip(original[max(0, index - 120) : index + 300])
    return _clip("\n".join(original.split("\n")[:6]))


def _written(root: Path, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": _relative(root, path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "lines": len(data.decode("utf-8", errors="replace").splitlines()),
    }


# --------------------------------------------------------------------------- #
# The before/after ledger: how a block's direct writes become traceable
# --------------------------------------------------------------------------- #


@dataclass
class WorkspaceLedger:
    """Records what this block changed in the workspace, by observing it.

    ★ Why observe instead of trusting each tool: `shell.run` can create files the
    shell never sees an argument for (`> out.csv`, `cmd && mv ...`). A ledger
    that only knew about `files.write` would leave a hole exactly where "run a
    command" was added -- and the judge must see everything the block put on disk.
    """

    root: Path | None
    region: tuple[str, ...] = ()
    used: bool = False
    _before: dict[str, str] | None = None

    def note_use(self) -> None:
        """The first channel-2 call: remember the workspace as it was.

        Lazy on purpose -- a block that never uses channel 2 pays nothing (not
        even a directory listing), which is what keeps every existing reading
        byte-identical.
        """
        if self.used:
            return
        self.used = True
        self._before = _digests(self.root) if self.root is not None else {}

    def changed(self) -> tuple[str, ...]:
        """Workspace-relative paths this block created or modified, sorted."""
        if not self.used or self.root is None:
            return ()
        before = self._before or {}
        after = _digests(self.root)
        return tuple(sorted(path for path, digest in after.items() if before.get(path) != digest))


def _ignored(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in IGNORED_PARTS for part in parts):
        return True
    return relative.endswith(IGNORED_SUFFIXES)


def _digests(root: Path | None) -> dict[str, str]:
    """A digest per file under ``root`` (relative path -> sha256). Bounded by the tree."""
    if root is None or not root.exists():
        return {}
    base = Path(root)
    found: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        if _ignored(relative):
            continue
        try:
            found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:  # a file that vanished mid-scan is not a change we can claim
            continue
    return found
