"""★ 重复 `workspace/` 前缀：措辞（负向例子）+ 具名拒护栏。

现场（`acceptance/ab/agent-jd-research/`，`text-volume-l1` 探针）：块把产物写到
`_run/workspace/workspace/output/brief.md` —— 多了一层 `workspace/`，而判定器读的是
`_run/workspace/output/brief.md`。`produce.py::_workspace_line` 的 docstring 记着
上一轮同类失败（`qwen4b` 第 34 轮：43 次调用零产物）⇒ "再写清楚一点"这条路走过一次、
没走通，所以两头一起做：

```
① 提示里的负向例子：不要写成 `<root 名>/output/xxx`，正确是 `output/xxx`
② 护栏：路径第一段恰好等于工作目录自己的 basename ⇒ 具名拒 + 正确路径
   —— 从 basename 现算（不写死 "workspace"）；读写两侧都生效；frozen 优先不受影响
```

本文件钉住：

```
① 写 `workspace/out.md`（根名就是 workspace）⇒ 具名拒，消息含正确路径
② 写 `output/out.md` ⇒ 正常通过
③ 根名不是 workspace 的部署 ⇒ 同名规则照跑；`notes/workspace/x` 不被误伤
④ ★ 反向：关掉护栏 ⇒ ①必须红
⑤ 读侧同样生效（具名 `ReadOutsideRegion`，不是写的那只）
⑥ 提示里的负向例子用的是**现算**的目录名
```
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bobanana6.assemble_impl.produce import LoopIOImpl
from bobanana6.services import ReadOutsideRegion, WorkspaceError, WriteOutsideRegion
from bobanana6.services.workspace import BoundWorkspace


def _workspace(root: Path, write_region: tuple[str, ...] = (), frozen_paths: tuple[str, ...] | None = ()) -> BoundWorkspace:
    return BoundWorkspace(root=root, write_region=write_region, frozen_paths=frozen_paths)


# --------------------------------------------------------------------------- #
# ① 写 `workspace/out.md`（根名就是 workspace）⇒ 具名拒 + 正确路径
# --------------------------------------------------------------------------- #


def test_a_path_that_repeats_the_workspace_basename_is_refused_by_name(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _workspace(root, write_region=("out.md",))
    with pytest.raises(WriteOutsideRegion) as error:
        workspace.write("workspace/out.md", "x")
    message = str(error.value)
    assert "block may not write 'workspace/out.md'" in message, message
    assert "you are already inside the working directory" in message, message
    assert "the path you want is 'out.md'" in message, message
    assert not root.exists()  # 拒就是拒，一个字节都不落盘


def test_the_correct_path_without_the_prefix_is_written(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _workspace(root, write_region=("out.md",))
    written = workspace.write("output/out.md", "ok")
    assert written["path"] == "output/out.md"
    assert (root / "output" / "out.md").read_text(encoding="utf-8") == "ok"


# --------------------------------------------------------------------------- #
# ③ 根名不是 workspace 的部署：规则照跑，且不误伤「后面才出现 workspace」的路径
# --------------------------------------------------------------------------- #


def test_the_rule_is_computed_from_this_deployments_own_basename(tmp_path: Path) -> None:
    root = tmp_path / "run-7c3a"  # 目录名与 "workspace" 毫无关系
    workspace = _workspace(root, write_region=("out.md",))
    with pytest.raises(WriteOutsideRegion) as error:
        workspace.write("run-7c3a/out.md", "x")
    message = str(error.value)
    assert "block may not write 'run-7c3a/out.md'" in message, message
    assert "the path you want is 'out.md'" in message, message
    # 反例：`workspace` 只是路径中间/后面的一段 ⇒ 与目录名无关，不得误伤
    workspace.write("notes/workspace/x.md", "fine")
    assert (root / "notes" / "workspace" / "x.md").is_file()


def test_a_declared_path_that_starts_with_the_basename_is_not_misjudged(tmp_path: Path) -> None:
    """★ 误伤边界：根目录恰好叫 `output`，而交付物就是 `output/x.csv`（已声明）⇒ 放行。"""
    root = tmp_path / "output"
    workspace = _workspace(root, write_region=("output/x.csv",))
    workspace.write("output/x.csv", "a,b\n")
    assert (root / "output" / "x.csv").read_text(encoding="utf-8") == "a,b\n"


def test_a_frozen_path_keeps_its_own_refusal(tmp_path: Path) -> None:
    """★ frozen 优先：既是 frozen 又带前缀时，报的是写区那条（规则没被这条盖掉）。"""
    root = tmp_path / "workspace"
    workspace = _workspace(root, write_region=("out.md",), frozen_paths=("workspace/other.md",))
    with pytest.raises(WriteOutsideRegion) as error:
        workspace.write("workspace/other.md", "x")
    message = str(error.value)
    assert "its write region is" in message, message
    assert "you are already inside" not in message, message


# --------------------------------------------------------------------------- #
# ④ ★ 反向：关掉护栏 ⇒ ①必须红
# --------------------------------------------------------------------------- #


def test_turning_the_guard_off_lets_the_prefixed_path_through(tmp_path: Path) -> None:
    """★ 反向：`reject_redundant_prefix=False` ⇒ ① 那条拒收不再发生（同一条路径能写下去）。

    写区为空 + frozen 已声明 ⇒ `may_write` 对 `workspace/out.md` 本来是放行的，
    唯一拦住它的是这道前缀护栏。
    """
    root = tmp_path / "workspace"
    workspace = BoundWorkspace(
        root=root, write_region=(), frozen_paths=(), reject_redundant_prefix=False
    )
    workspace.write("workspace/out.md", "x")
    assert (root / "workspace" / "out.md").read_text(encoding="utf-8") == "x"


# --------------------------------------------------------------------------- #
# ⑤ 读侧同样生效（具名 ReadOutsideRegion，不是写的那只）
# --------------------------------------------------------------------------- #


def test_the_read_side_refuses_the_same_prefix_and_keeps_its_own_name(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace = _workspace(root, write_region=("out.md",))
    workspace.write("output/out.md", "content")
    with pytest.raises(ReadOutsideRegion) as error:
        workspace.read("workspace/output/out.md")
    message = str(error.value)
    assert "block may not read 'workspace/output/out.md'" in message, message
    assert "the path you want is 'output/out.md'" in message, message
    assert not isinstance(error.value, WriteOutsideRegion)


def test_shell_workdir_repeats_the_basename_is_refused_the_same_way(tmp_path: Path) -> None:
    """★ `shell.run` 的 cwd 也在工作区里：workdir 再写一次目录名 = 同一条迷路。"""
    root = tmp_path / "workspace"
    workspace = _workspace(root, write_region=("out.md",))
    with pytest.raises(WorkspaceError) as error:
        workspace.run("pwd", workdir="workspace/output")
    assert "the path you want is 'output'" in str(error.value), str(error.value)


# --------------------------------------------------------------------------- #
# ⑥ 提示里的负向例子用的是现算的目录名
# --------------------------------------------------------------------------- #


class _LineCase:
    """驱动 `_workspace_line` 的最小替身：它只读 `workspace`。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def _workspace_value(self) -> str:
        return LoopIOImpl._workspace_value(self)

    def _workspace_line(self) -> str:
        return LoopIOImpl._workspace_line(self)


def test_the_negative_example_names_this_deployments_own_directory(tmp_path: Path) -> None:
    line = _LineCase(tmp_path / "workspace")._workspace_line()
    assert "不要写成 `workspace/output/xxx`" in line, line
    assert "正确是 `output/xxx`" in line, line
    assert "`workspace/` 那层是多余的" in line, line

    other = _LineCase(tmp_path / "srv-output")._workspace_line()
    assert "不要写成 `srv-output/output/xxx`" in other, other
    assert "workspace" not in other, other  # 不写死那个字符串
