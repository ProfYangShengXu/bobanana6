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
from collections.abc import Mapping
from dataclasses import dataclass, field
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
#: 扫描工作区时忽略的目录名：编译/测试缓存的副产品不是产物，也不是越权写。
IGNORED_PARTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"})
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
    #: Long-lived helpers owned by this block (the code session, background jobs).
    _code_session: Any = None
    _background: list[subprocess.Popen[bytes]] = field(default_factory=list)

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
            return {"background": True, "pid": process.pid, "cwd": _relative(root, cwd), "command": command}
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
            "cwd": _relative(root, cwd),
            "stdout": (completed.stdout or "")[:4000],
            "stderr": (completed.stderr or "")[:4000],
            "truncated": len(completed.stdout or "") > 4000 or len(completed.stderr or "") > 4000,
        }

    def run_code(self, code: str, *, reset: bool = False) -> Mapping[str, Any]:
        """Run a snippet in the workspace, in a session that persists between calls.

        ``reset=True`` drops the session (fresh namespace, fresh interpreter) --
        that is the whole content of Hermes' `execute_code(reset=...)` contract
        once the orchestration around it is left behind.
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
        return self._code_session.execute(code)

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
