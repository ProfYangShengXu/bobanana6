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
③ 执行边界   `shell.run` / `code.run` 的 cwd 一定在工作区内；后台任务在块结束时收尸。
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
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

__all__ = [
    "BoundWorkspace",
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
DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 600.0
#: ★★ 方案 B：`code.run` 每次执行前，把那段代码**原样**落盘到工作区里的这个目录。
#:
#: 为什么必须是**工作区里的相对目录**（而不是 `/tmp`）：落盘的全部意义就是让那段代码
#: 变成**磁盘上可被 `files.patch` 按原文精确修改的对象**（"行号会漂，文本不会漂"）。
#: 模型此前把代码塞在 tool-call 参数里 ⇒ 想改一行只能重贴一整段 ⇒ `files.patch` 永远够不着。
CODE_DIR = "_code"
#: ★ 输出"换页"的两个上限（照 flash §2.4）：正文只留**末尾** N 行 / M 字节，先到者为准。
#: 保留末尾而不是开头：**报错与结果总在最后**，开头多是 import 与噪音。
#: 完整输出落盘，路径在返回的 `full_output_path` 里 —— 截断不丢信息。
CODE_OUTPUT_MAX_LINES = 2000
CODE_OUTPUT_MAX_BYTES = 50 * 1024
#: ★ 全量输出文件（`step-NNNN.out`）自己的上限（8 MiB）。它写的是**全量 stdout**，
#: 不设闸就是一个新的磁盘外泄面；绑到上限时**如实标** `full_output_truncated`，不假装"全量"。
CODE_OUTPUT_FULL_MAX_BYTES = 8 * 1024 * 1024
#: 扫描工作区时忽略的目录名：编译/测试缓存的副产品不是产物，也不是越权写。
#: ★★ `CODE_DIR` 也在这里：`code.run` 落盘的是**壳自己的草稿纸**，不是块交出来的产物 ——
#:    `_digests` 看不见它 ⇒ ledger 的 `changed()` 不含它 ⇒ 通道 2 的对账
#:    （`_reconcile_workspace`）不会把它收成候选产物。**这是唯一的排除点**（单点收口）。
IGNORED_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", CODE_DIR}
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
    #: ★★ 方案 B：这一块草稿纸的**归属** —— 落盘路径里的那个子目录名（`_code/<block>/…`）。
    #: 同一个 run 里所有块共用同一个工作区，而每个块的 `BoundWorkspace` 都是新的、序号从 1
    #: 开始 —— 不打这块牌子，**每个块的第一条 `code.run` 都会覆盖上一个块的存底**，
    #: 返回给模型的 `code_path` 就指向了别人的代码。由
    #: `services/registry.py::build_registry` 从 `toolset.subtask` 上号（那里同时拿得到两者）。
    block: str = ""  # blame: fail-closed — 省略即落到 `_code/unscoped/`（不冒充任何块），不会覆盖别块
    #: Long-lived helpers owned by this block (the code session, background jobs).
    _code_session: Any = None
    _background: list[subprocess.Popen[bytes]] = field(default_factory=list)
    #: ★★ 方案 B：这一块调过多少次 `code.run` —— 它就是落盘文件名的**确定性序号**
    #:（`_code/step-0001.py`）。不用时间戳：同一段代码重跑要得到同一个名字，否则不可复现。
    #: `reset` **不重置它**（reset 清的是 Python 命名空间，不是"代码存底"）。
    _code_calls: int = 0  # blame: fail-closed — 省略即从 0 开始编号，最保守（不覆盖已存在的存底）
    #: 落盘失败过几次（**如实报**，不假装存了；见硬边界⑦：落盘是辅助不是前置门）。
    _code_dump_failures: list[str] = field(default_factory=list)  # blame: whitelist — 只是"失败过什么"的证据清单，缺省空 = 没失败过

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

    def _check_region(self, path: str) -> None:
        """★ 写区互斥的执行点：**撞到别人的东西**才拒（空区 ≠ 没有权限）。

        ★★ 原来这里是"不在本块 partition 里的路径一律不许写" —— 太严：块连临时文件
        都建不了（实测 `handoff` 轮 T-02 写 `_tmp_answer.json` 被拒，只能塞一小撮样品
        进 `rows` 被判"不全"）。写区互斥要防的是"别撞别人的交付物/来源"，
        **不是"只许碰自己那一份"**。规则与 `produce.py::_reconcile_workspace` /
        `_persist_files` 一致 —— 三处不一样的话，换个工具就换个规矩。
        """
        if may_write(path, self.write_region, self.frozen_paths):
            return
        allowed = sorted({_normalized(name) for name in self.write_region})
        raise WriteOutsideRegion(
            f"block may not write {_normalized(path)!r}: its write region is {allowed!r}"
        )

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
        """Run a shell command with its cwd **inside** the workspace (Hermes `terminal`)."""
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
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=seconds,
            check=False,
        )
        return {
            "background": False,
            "exit_code": completed.returncode,
            # ★★ 绝对路径，不是 `"."`（实测那一条见 `_DRIVER` 上方注记）：
            #   每一轮返回都给模型一次校准机会 —— "我现在站在哪里"。不改任何判据。
            "cwd": str(cwd),
            "stdout": (completed.stdout or "")[:4000],
            "stderr": (completed.stderr or "")[:4000],
            "truncated": len(completed.stdout or "") > 4000 or len(completed.stderr or "") > 4000,
        }

    def run_code(self, code: str, *, reset: bool = False) -> Mapping[str, Any]:
        """★★ 方案 B：**先落盘、再跑那个文件**（对模型仍然只有一个 `code` 参数）。

        ```
        ① 把这段代码**原样**写成工作区里的 `_code/step-NNNN.py`（序号 = 确定性规则）
        ② 再把同一个字符串送进持久 Python 会话执行（送进去的那段与改前**逐字节相同**）
        ③ 输出超上限时：正文只留**末尾**（2000 行 / 50KB，先到者为准），
           完整输出落盘，路径在 `full_output_path` 里 —— 截断是换页，不是销毁
        ```

        ``reset=True`` 的语义**一个字没变**：清的是 Python 会话的命名空间（与解释器），
        **不是**清文件 —— 存底的代码留着，序号接着往下走。

        ★ 两条硬边界在这里落地：**不加参数**（还是 `code` + `reset`）；**落盘是辅助不是前置门**
          （写不成就把失败如实放进返回、代码照跑）。返回**只增不减**：`ok` / `stdout` / `value`
          / `error` / `cwd` 一个都不动。
        """
        if not isinstance(code, str) or not code.strip():
            raise WorkspaceError("code must be a non-empty string")
        self._note()
        root = self._require_root()
        if reset and self._code_session is not None:
            self._code_session.close()
            self._code_session = None
        if self._code_session is None or not self._code_session.alive:
            self._code_session = _CodeSession(root)
        # ★★ ① 先落盘 —— 于是这段代码变成磁盘上一个可被 `files.patch` 按原文改的对象。
        self._code_calls += 1
        saved = self._save_code(self._code_calls, code)
        answer = dict(self._code_session.execute(code))
        # ★★ `code.run` 的返回也带上**绝对** cwd：以前它一个 `cwd` 字段都没有，
        #   模型跑完代码看不到自己站在哪 —— 实测（`qwen4b` 第 34 轮）它正是在
        #   连续 27 轮 `code.run` 之后自己 `mkdir -p <repo>/…/output` 猜了一个输出目录。
        #   与 `run()` 同一个真值来源（`_require_root()` 已经 resolve 过），不改任何判据。
        answer["cwd"] = str(root)
        answer["code_path"] = saved
        if not saved:
            # ★ 硬边界⑦的"如实报"：没存成就说没存成，不假装有路径。
            answer["code_save_error"] = self._code_dump_failures[-1]
        # ★★ 元数据排到 `stdout` 前面（只读审计 #71）：上下文压缩按**头部**截 2000 字符，
        #   排在几千字符 stdout 之后的 `code_path`/`full_output_path` 会被整段砍掉。
        return _metadata_first(self._page_code_output(answer, self._code_calls))

    def _code_root(self) -> str:
        """这一块的草稿纸目录（工作区相对）：`_code/<块>/`（块未知时 `_code/unscoped/`）。"""
        return f"{CODE_DIR}/{_slug(self.block)}"

    def _save_code(self, seq: int, code: str) -> str:
        """把这段代码**原样**写成 `_code/<块>/step-NNNN.py`，返回工作区相对路径；失败返回 `""`。

        ★ 原样：不加工、不重排、不加壳 —— 模型之后要用 `files.patch` 按**原文**改它。
          用 `write_bytes` 而不是 `write_text`：文本模式会把 `\\n` 翻成 `os.linesep`，
          那会让"逐字节相同"在非 Linux 上不成立（只读审计 #72）。
        ★ 路径是**工作区相对**的：与 `files.write` / `files.patch` 的路径口径**同一个**
          （两者都以工作区为基准）⇒ 返回的 `code_path` 可以直接当 `files.patch` 的 `path` 用。
        ★ 按**块**分子目录：见 `BoundWorkspace.block` 的注记（跨块重名会把别人的代码覆盖掉）。
        """
        relative = f"{self._code_root()}/step-{seq:04d}.py"
        try:
            target = self._require_root() / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(code.encode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            self._code_dump_failures.append(f"{type(exc).__name__}: {exc}")
            return ""
        # ★ 只作**证据**记账：不过 `WriteBudget` 的闸 —— 硬边界⑦说落盘不许把块拦下来。
        self.write_budget.note_bytes(len(code.encode("utf-8")))
        return relative

    def _page_code_output(self, answer: dict[str, Any], seq: int) -> dict[str, Any]:
        """★ 截断 = 换页：正文只留末尾，完整输出落盘并给出路径（不销毁任何东西）。"""
        text = str(answer.get("stdout") or "")
        paged = _tail_page(text, CODE_OUTPUT_MAX_LINES, CODE_OUTPUT_MAX_BYTES)
        if paged == text:
            answer["truncated"] = False
            return answer
        relative = f"{self._code_root()}/step-{seq:04d}.out"
        # ★ 全量那份也有上限（8 MiB）——写的是全量 stdout，不设闸就是新的磁盘外泄面。
        body = text.encode("utf-8")
        capped = len(body) > CODE_OUTPUT_FULL_MAX_BYTES
        if capped:
            body = body[-CODE_OUTPUT_FULL_MAX_BYTES:]
        try:
            target = self._require_root() / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        except (OSError, UnicodeError, ValueError) as exc:
            # ★ 换不成页就**不销毁**：宁可把全量给它，也不留一份"截了却取不回"的输出。
            self._code_dump_failures.append(f"{type(exc).__name__}: {exc}")
            answer["truncated"] = False
            answer["page_error"] = f"{type(exc).__name__}: {exc}"
            return answer
        # ★ 截断必须**可见**，而且要说得清"丢了多少、原文多长"（否则模型会以为输出就这么短）：
        #   正文里留一句"留了末尾哪一段、原文共多少、全文在哪"。
        total_lines = len(text.splitlines())
        total_bytes = len(text.encode("utf-8"))
        answer["stdout"] = (
            f"…[output truncated: kept the last {len(paged.splitlines())} lines / "
            f"{len(paged.encode('utf-8'))} bytes of {total_lines} lines / {total_bytes} bytes; "
            f"full output at {relative}]\n{paged}"
        )
        answer["truncated"] = True
        answer["full_output_path"] = relative
        # ★ 结构化读数：总量与原文规模分开给，别只留一句自然语言。
        answer["full_output_lines"] = total_lines
        answer["full_output_bytes"] = total_bytes
        if capped:
            # ★ 绑到上限就如实标：不假装那份文件是"全量"。
            answer["full_output_truncated"] = True
        self.write_budget.note_bytes(len(body))
        return answer

    # -- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        """Kill the code session and every background job -- no orphan processes."""
        if self._code_session is not None:
            self._code_session.close()
            self._code_session = None
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


def _tail_page(text: str, max_lines: int, max_bytes: int) -> str:
    """把一段输出换页：留**末尾**，两个上限都满足（= 谁先到就按谁截）。

    ★ 保留末尾而不是开头：报错与结果在最后，开头多是 import 与噪音。
    ★ 返回的一定是原文的**连续后缀**（不重排、不加壳）—— 被截掉的部分去
      `full_output_path` 里拿得回来，所以这是换页不是销毁。
    """
    if not text:
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    page = "".join(lines)
    encoded = page.encode("utf-8")
    if len(encoded) > max_bytes:
        # 按字节切到末尾；切点可能落在字符中间 ⇒ 用 `ignore` 丢掉那半个（不影响后半段的完整）。
        page = encoded[-max_bytes:].decode("utf-8", errors="ignore")
    return page


def _slug(text: str) -> str:
    """把一个块 id 变成安全的目录名（只留 `[A-Za-z0-9._-]`，其余换成 `_`）。

    ★ 块 id 来自契约/切分器（今天形如 `T-01`），但**路径安全**不该赌它的形状：
      带 `/` 或 `..` 的 id 会逃出草稿纸目录。空串落到 `unscoped`（不冒充任何块）。
    """
    cleaned = "".join(char if (char.isalnum() or char in "._-") else "_" for char in str(text))
    return cleaned.strip("._") or "unscoped"


def _metadata_first(answer: Mapping[str, Any]) -> dict[str, Any]:
    """把**元数据键排到 `stdout` 前面**（键序 = JSON 里的字面顺序）。

    ★★ 实测（只读审计 #71）：上下文压缩按**头部**截 2000 字符（`compress.tool_result_char_cap`），
      而 `code_path` / `full_output_path` 排在几千字符的 stdout **之后** ⇒ 两键被整段砍掉，
      "被截的内容去 `full_output_path` 拿"这句话在长会话里**等于没有**。放前面就是修法。
    ★ 只重排、不增不减：未知键原样留在后面。
    """
    priority = (
        "ok",
        "code_path",
        "full_output_path",
        "truncated",
        "full_output_truncated",
        "code_save_error",
        "page_error",
        "error",
    )
    head = {key: answer[key] for key in priority if key in answer}
    return {**head, **{key: value for key, value in answer.items() if key not in head}}


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

    ★ Why observe instead of trusting each tool: `shell.run` and `code.run` can
    create files the shell never sees an argument for (`> out.csv`,
    `open(...).write(...)`). A ledger that only knew about `files.write` would
    leave a hole exactly where "run some code" was added -- and the judge must
    see everything the block put on disk.
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


# --------------------------------------------------------------------------- #
# The persistent code session
# --------------------------------------------------------------------------- #

#: The driver runs inside the workspace and speaks one JSON line per request.
#: It is deliberately tiny: read a line, exec it in a namespace, answer a line.
#: Everything the code prints is captured into the answer, never onto our pipe.
_DRIVER = r"""
import ast, contextlib, io, json, sys, traceback

namespace = {"__name__": "__worker__"}

def _run(code):
    source = io.StringIO()
    value = None
    try:
        module = ast.parse(code, mode="exec")
        last = module.body[-1] if module.body else None
        tail = ast.Expression(last.value) if isinstance(last, ast.Expr) else None
        with contextlib.redirect_stdout(source), contextlib.redirect_stderr(source):
            if tail is not None and len(module.body) == 1:
                value = eval(compile(tail, "<worker>", "eval"), namespace)
            else:
                exec(compile(module, "<worker>", "exec"), namespace)
                if tail is not None:
                    value = eval(compile(tail, "<worker>", "eval"), namespace)
        return {"ok": True, "stdout": source.getvalue(), "value": None if value is None else repr(value)}
    except BaseException as exc:
        return {"ok": False, "stdout": source.getvalue(),
                "error": "".join(traceback.format_exception_only(type(exc), exc)).strip()}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        request = json.loads(line)
    except Exception:
        continue
    if request.get("op") == "exit":
        break
    answer = _run(str(request.get("code", "")))
    sys.stdout.write(json.dumps(answer) + "\n")
    sys.stdout.flush()
"""


class _CodeSession:
    """A `python3` child that keeps one namespace alive between snippets.

    Restarted transparently if the snippet killed it (`sys.exit`, a hard crash):
    a dead session is a fact to report, not a reason to stop answering.
    """

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd
        self._process: subprocess.Popen[str] | None = None
        self._open()

    def _open(self) -> None:
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-u", "-c", _DRIVER],
                cwd=str(self._cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            raise WorkspaceUnavailable(f"cannot start a code session: {exc}") from exc

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def execute(self, code: str) -> Mapping[str, Any]:
        if not self.alive:
            self._open()
        process = self._process
        assert process is not None and process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(json.dumps({"code": code}) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except (BrokenPipeError, OSError):
            self.close()
            return {"ok": False, "error": "the code session died before answering; it was restarted"}
        if not line:
            self.close()
            return {"ok": False, "error": "the code session produced no answer; it was restarted"}
        try:
            answer = json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"the code session answered with non-JSON: {line[:200]!r}"}
        if not isinstance(answer, Mapping):
            return {"ok": False, "error": "the code session answered with a non-object"}
        return dict(answer)

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None and process.poll() is None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill is best effort
            pass
