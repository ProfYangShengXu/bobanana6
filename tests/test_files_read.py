"""`files.read`：块能读回【自己有权写】的东西（首要就是它自己的交付物）。

现场（`_run/artifacts/tree` commit `9937275`，4B 那轮 T-01）：它把 7 份材料全读到了，
却在 turn12 拿 `sources.read` 读 `output/jd-matrix.md`（**它自己的交付物**）被
`PermissionError` 拒；turn13 起只能 `shell.run cat` 硬读 —— 33 次 shell.run 大半在
回读自己写的文件。根因是工具面里没有"读回自己写区"的路，这是壳的账。

六条断言，逐条对应任务书：
  ① 能读回自己写的（内容 + 行号）；
  ② 读不到冻结路径，且异常名字**不是** `WriteOutsideRegion`；
  ③ 读不到写区之外（`frozen_paths is None` 的部署）；
  ④ `files.read` **不算**"读过来源"（read-before-produce 的判据一个字没改）；
  ⑤ 分页边界（超界 / 空文件 / 最后一行）；
  ⑥ 反向：既有的 `sources.read` / `files.write` 读数不变。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import toolset_for

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.core.model import Budget, Snapshot, Subtask
from bobanana6.core.ports import _BASE_TOOL_SPECS
from bobanana6.services import BoundWorkspace, ReadOutsideRegion, WriteOutsideRegion, build_registry
from bobanana6.services.registry import TOOL_NAMES
from tests.test_services import _full_toolset


# --------------------------------------------------------------------------- #
# 调用脚手架
# --------------------------------------------------------------------------- #


def _workspace(root: Path, *, region: tuple[str, ...], frozen: tuple[str, ...] | None = ()) -> BoundWorkspace:
    return BoundWorkspace(root=root, write_region=region, frozen_paths=frozen)


def _workspace_registry(workspace: BoundWorkspace):
    """一个只装了工作区的最小注册表（走真的 handler，不是打桩）。"""
    toolset = replace(toolset_for("T-01"), workspace=workspace, has_deliverable=True)
    return toolset, build_registry(toolset, lambda snapshot: b"")


def _call(registry, name: str, **arguments):
    raw = {
        "id": f"c-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return registry.execute((registry.calls([raw])[0],))[0]


# --------------------------------------------------------------------------- #
# ① 能读回自己写的
# --------------------------------------------------------------------------- #


def test_a_block_reads_back_what_it_wrote(tmp_path: Path) -> None:
    """写一个文件再 `files.read` 它 —— 拿到内容与**带行号**的行窗。"""
    workspace = _workspace(tmp_path / "ws", region=("out.csv",))
    written = workspace.write("out.csv", "obligor,amount\nA-1,10\nB-2,20\n")
    page = workspace.read("out.csv")

    assert page["path"] == "out.csv"
    assert page["bytes"] == written["bytes"]
    assert page["sha256"] == written["sha256"]
    assert page["total_lines"] == 3
    assert [row["line"] for row in page["lines"]] == [1, 2, 3]
    assert page["lines"][0]["text"] == "obligor,amount"
    assert page["lines"][2]["text"] == "B-2,20"
    assert page["truncated"] is False and page["next_offset"] is None
    assert page["sha256"] == hashlib.sha256(b"obligor,amount\nA-1,10\nB-2,20\n").hexdigest()

    # ★ 走**真的注册表**也读得到（证明它真的接进了工具面，不是只有方法在）。
    _toolset, registry = _workspace_registry(workspace)
    result = _call(registry, "files.read", path="out.csv")
    assert result.ok is True, result.content
    payload = json.loads(result.content)
    assert payload["lines"][1]["text"] == "A-1,10"


# --------------------------------------------------------------------------- #
# ② 读不到冻结路径 —— 具名拒，且不是 `WriteOutsideRegion`
# --------------------------------------------------------------------------- #


def test_reading_a_frozen_path_is_refused_by_name(tmp_path: Path) -> None:
    """别人的交付物 / 任务来源都在 frozen 里 ⇒ 读必拒，异常名字说清是"读"。"""
    root = tmp_path / "ws"
    workspace = _workspace(
        root,
        region=("out.csv",),
        frozen=("other/out.csv", "input/seed.txt"),
    )
    (root / "other").mkdir(parents=True, exist_ok=True)
    (root / "other" / "out.csv").write_text("someone else's deliverable\n", encoding="utf-8")

    for frozen in ("other/out.csv", "input/seed.txt"):
        with pytest.raises(ReadOutsideRegion) as error:
            workspace.read(frozen)
        # ★ 一个读失败绝不能报"你不能写"（那会让归因读错层）。
        assert not isinstance(error.value, WriteOutsideRegion)
        assert "may not read" in str(error.value)
        assert "write region" in str(error.value)

    # ★ 别名也不行：写区里种一个软链指到 frozen 的目标时，只按**名字**判权限会让
    #   `files.read('mine.csv')` 读到 `other/out.csv`。必须按 `_inside` 解析出的
    #   **真实相对路径**用同一条 `may_write` 再判一次（同 MCP `validatePath`）。
    try:
        os.symlink("other/out.csv", root / "mine.csv")
    except OSError:  # pragma: no cover - hosts without symlink support
        pass
    else:
        with pytest.raises(ReadOutsideRegion):
            workspace.read("mine.csv")

    # 注册表这一层也必须是 ReadOutsideRegion（模型看到的就是这个 error_type）。
    _toolset, registry = _workspace_registry(workspace)
    refused = _call(registry, "files.read", path="other/out.csv")
    assert refused.ok is False
    assert refused.error_type == "ReadOutsideRegion", refused
    assert "may not read" in refused.content
    # ★ 反向：同一个路径上，**写**仍报 WriteOutsideRegion（两臂没有混成一条）。
    writing = _call(registry, "files.write", path="other/out.csv", content="stolen")
    assert writing.ok is False and writing.error_type == "WriteOutsideRegion", writing


# --------------------------------------------------------------------------- #
# ③ 读不到写区之外（fail-closed 的部署）
# --------------------------------------------------------------------------- #


def test_reading_outside_the_write_region_is_refused_without_a_frozen_declaration(
    tmp_path: Path,
) -> None:
    """`frozen_paths=None`（没声明别人的东西是哪些）⇒ 退回旧规则：写区之外必拒。"""
    workspace = BoundWorkspace(root=tmp_path / "ws", write_region=("out.csv",))  # frozen_paths=None
    with pytest.raises(ReadOutsideRegion):
        workspace.read("_tmp_scratch.json")
    # 写区的正面读数：能写的那个读得到（读区 ⊆ 写区，不是"整个部署都读不了"）。
    workspace.write("out.csv", "x\n")
    assert workspace.read("out.csv")["lines"][0]["text"] == "x"


# --------------------------------------------------------------------------- #
# ④ 不算"读过来源"（约束 ③ 钉成断言）
# --------------------------------------------------------------------------- #


class _OneSource:
    """一块只被授权读一份来源的 sources 端口（`refs` 非空 ⇒ 有来源可取）。"""

    def refs(self) -> tuple[str, ...]:
        return ("snap-1",)

    def snapshot(self, ref: str) -> Snapshot:  # pragma: no cover - 本用例不会取数
        return Snapshot(ref=ref, body_path=Path("gone.csv"), captured_at="T0")


def test_files_read_does_not_count_as_reading_sources(tmp_path: Path) -> None:
    """★ read-before-produce 只认**成功的 `sources.*`**：读自己的草稿不是取数。"""
    from bobanana6.agentloop.tools import ToolCall
    from bobanana6.assemble_impl.produce import LoopIOImpl

    workspace = _workspace(tmp_path / "ws", region=("draft.md",))
    workspace.write("draft.md", "first draft\n")
    toolset = replace(
        toolset_for("T-01", sources=True),
        sources=_OneSource(),
        workspace=workspace,
    )
    io = LoopIOImpl(
        client=None,
        toolset=toolset,
        registry=build_registry(toolset, lambda snapshot: b""),
        judge=None,
        subtask=Subtask(
            id="T-01",
            goal="read sources",
            inputs=(),
            output="draft.md",
            contract_ref="t",
            axis="source",
            mode="read",
        ),
        budget=Budget(max_calls=4, max_wall_s=10, max_output=128),
    )
    # 前置：这一块**必须**读来源（只有来源一条取数路，且此刻一次都没读）。
    assert io._must_read_sources() is True
    assert io.read_sources is False

    call = ToolCall(
        call_id="c1",
        name="files.read",
        arguments={"path": "draft.md"},
        raw_arguments=json.dumps({"path": "draft.md"}),
    )
    results = io.execute((call,))
    assert results[0].ok is True, results[0].content

    # ★ 读回自己的草稿**不置真** —— 约束 ③ 的核心断言。
    assert io.read_sources is False, "files.read must not satisfy read-before-produce"


# --------------------------------------------------------------------------- #
# ⑤ 分页边界
# --------------------------------------------------------------------------- #


def test_reading_windows_lines_and_handles_the_boundaries(tmp_path: Path) -> None:
    """`offset` / `limit`（复用 `line_window`）：中间窗、最后一行、超界、空文件。"""
    workspace = _workspace(tmp_path / "ws", region=("f.txt",))
    workspace.write("f.txt", "a\nb\nc\n")

    middle = workspace.read("f.txt", offset=1, limit=1)
    assert [row["line"] for row in middle["lines"]] == [2]
    assert middle["lines"][0]["text"] == "b"
    assert middle["next_offset"] == 2 and middle["truncated"] is True

    last = workspace.read("f.txt", offset=2)
    assert [row["line"] for row in last["lines"]] == [3]
    assert last["next_offset"] is None and last["truncated"] is False

    beyond = workspace.read("f.txt", offset=99, limit=5)
    assert beyond["lines"] == [] and beyond["returned_lines"] == 0
    assert beyond["next_offset"] is None and beyond["truncated"] is False

    empty = _workspace(tmp_path / "ws2", region=("empty.txt",))
    empty.write("empty.txt", "")
    page = empty.read("empty.txt")
    assert page["total_lines"] == 0 and page["lines"] == []
    assert page["next_offset"] is None and page["truncated"] is False


# --------------------------------------------------------------------------- #
# ⑥ 反向：既有的写 / 读来源两臂读数不变
# --------------------------------------------------------------------------- #


def test_the_existing_write_and_source_read_arms_are_unchanged(tmp_path: Path) -> None:
    """加了 `files.read` 之后，`files.write` 与 `sources.read` 的**返回形状**逐字不变。"""
    _tools, registry = _full_toolset(tmp_path)

    written = json.loads(_call(registry, "files.write", path="out.csv", content="a,b\n1,2\n").content)
    assert set(written) == {"path", "bytes", "sha256", "lines"}
    assert written["path"] == "out.csv" and written["bytes"] == 8 and written["lines"] == 2
    assert written["sha256"] == hashlib.sha256(b"a,b\n1,2\n").hexdigest()

    page = json.loads(_call(registry, "sources.read", ref="src-1", offset="0", limit="1").content)
    assert page["ref"] == "src-1" and page["total_lines"] == 2 and page["returned_lines"] == 1
    assert page["lines"][0] == {"line": 1, "text": "a"} and page["truncated"] is True

    # ★ 新增的工具在菜单/基表/注册面三处一致，且 registration 里 effect 是 read。
    assert "files.read" in TOOL_NAMES
    assert "files.read" in {spec.name for spec in _BASE_TOOL_SPECS}
    assert "files.read" in {spec.name for spec in _tools.specs()}
    assert registry.effect("files.read") == "read"
