"""把【工作目录】与【本块写区】告诉模型 —— 实测暴露的壳的账。

证据（`.bobanana6/ab/archives/qwen4b-A-c1.jsonl`，43 次调用、零产物）：
T-01 把 `filing_resolution.csv` 写到了 `<repo>/13f-crowding/output/`，
而判定器读的是 `<repo>/13f-crowding/_run/workspace/output/`；它随后自己
`mkdir -p <repo>/13f-crowding/output`，一路重试到预算耗尽。

三处修复，全部是【告知】——判定一处没动（拦截仍在 `services/workspace.py::may_write`
与 `produce.py::_reconcile_workspace`）：

  ① system 的恒定前缀里有**绝对**工作目录 + "brief 里的相对路径以它为基准" + "不许猜绝对路径"
  ② brief 里有本块的写区（`you_may_write` / `never_write` / `rule`），与 `frozen_paths` 对齐
  ③ `shell.run` / `code.run` 的返回带**绝对** cwd（原来 shell 是 `"."`、code 干脆没有这个字段）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl.produce import LoopIOImpl
from bobanana6.core.model import ModelResponse
from bobanana6.services.workspace import BoundWorkspace
from tests.test_convert_three import (
    _block_of,
    _briefing,
    _ConstantCase,
    _subtask,
    _task,
    _tool_call,
)

# --------------------------------------------------------------------------- #
# 跑一道真题，把每个块收到的 system 与 brief 原样抓下来
# --------------------------------------------------------------------------- #


def _capture(tmp_path: Path, name: str):
    task = _task(tmp_path, name=name)
    systems: dict[str, str] = {}
    briefs: dict[str, dict] = {}
    turns: dict[str, int] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages) or "?"
            system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
            systems.setdefault(block, system)
            briefs.setdefault(block, _briefing(messages))
            turn = turns.get(block, 0)
            turns[block] = turn + 1
            # 先真读一次来源（不然会被"读了来源才准答"那条前置门挡住），再交答案。
            if block == "T-01" and turn == 0:
                return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}],
                        "rows": [{"obligor": "A-1"}],
                        "done": True,
                    }
                )
            )

    run = run_task(task, client=_Client(), env={}, workspace=True)
    return run, systems, briefs


# --------------------------------------------------------------------------- #
# ① system：绝对工作目录 + 口径 + 边界（全 DAG 恒定）
# --------------------------------------------------------------------------- #


def test_every_block_system_has_the_absolute_workspace_and_the_basis_sentence(tmp_path: Path) -> None:
    """★ 反证主体：工作目录**绝对路径**进了每个块的 system，且口径/边界句都在。

    改前：`_workspace_line` 只丢一个路径字符串 —— 实测那一轮模型照旧按题面里
    写死的 `/app/data/` 与 `find` 出来的绝对路径走，最后猜了一个输出目录。
    """
    run, systems, _ = _capture(tmp_path, "ws-in-system")
    assert run.workspace is not None and systems, "no block was ever asked"
    root = str(Path(run.workspace).resolve())
    for block, system in systems.items():
        assert "WORKSPACE PATH:" in system, block
        assert root in system, (block, root)
        # ★ 口径：相对路径以它为基准（不能只给一个路径字符串）
        assert "brief 里所有相对路径都以它为基准" in system, block
        # ★ 边界：不许自己推断/猜测绝对路径
        assert "不许自己推断或猜测绝对路径" in system, block
        # ★ 路径是绝对路径，不是 cwd 那种 "."
        assert Path(root).is_absolute()


def test_the_workspace_prefix_is_dag_constant(tmp_path: Path) -> None:
    """★ 恒定判据：工作区根是整跑级的 ⇒ 它在**全 DAG** 上同一个值，进 system 前缀。"""
    run, systems, _ = _capture(tmp_path, "ws-constant")
    assert run.workspace is not None
    root = str(Path(run.workspace).resolve())
    prefixes = {block: system.split("YOUR TASK:")[0] for block, system in systems.items()}
    for block, prefix in prefixes.items():
        workspace_index = prefix.find("WORKSPACE PATH:")
        assert workspace_index != -1 and root in prefix[workspace_index:], block
    # 恒定前缀在块之间逐字节相同（工作区行在里面，不会让缓存从中间断开）
    assert len(set(prefixes.values())) == 1, "the workspace line is not DAG-constant"


# --------------------------------------------------------------------------- #
# ④ 工作区行的**出现**由 §4 恒定判据决定（不许无条件手工塞进 system）
# --------------------------------------------------------------------------- #


class _PrefixCase:
    """驱动 `_system_prefix` 的最小替身：它只要 `toolset.task.contract().goal`。"""

    def __init__(self, workspace: Path) -> None:
        class _Task:
            @staticmethod
            def contract():  # type: ignore[no-untyped-def]
                from types import SimpleNamespace

                return SimpleNamespace(goal="g")

        from types import SimpleNamespace

        self.workspace = workspace
        self.toolset = SimpleNamespace(task=_Task())

    # ★ 在**调用时**才取 `LoopIOImpl` 的方法（不是类体赋值）：这样在"旧代码"那一臂上，
    #   本题是一条普通的 **失败**（AttributeError 落在测试体内），而不是让整个文件收集失败
    #   —— 两臂逐条对比要的是"同一条测试 red/green"，不是 collection error。
    def _workspace_value(self) -> str:
        return LoopIOImpl._workspace_value(self)

    def _workspace_line(self) -> str:
        return LoopIOImpl._workspace_line(self)


def test_the_workspace_line_appears_only_when_the_constant_check_says_so(tmp_path: Path) -> None:
    """★★ ④：工作区行**不是**无条件直插 system —— 判据说恒定才出现。

    反证：拿不到全 DAG（`_constant_brief_entries` 返回 `{}`）⇒ 一个字都不加。
    这正是"不许手工把它塞进 system"的可执行形式。
    """
    case = _PrefixCase(tmp_path / "ws")
    without = LoopIOImpl._system_prefix(case, {})
    assert "WORKSPACE PATH:" not in without
    with_it = LoopIOImpl._system_prefix(case, {"workspace": str((tmp_path / "ws").resolve())})
    assert "WORKSPACE PATH:" in with_it
    assert str((tmp_path / "ws").resolve()) in with_it


def test_the_workspace_value_is_a_dag_constant_candidate(tmp_path: Path) -> None:
    """★ 工作区根进了候选表，且真的过全 DAG 比较（不是白名单）。"""
    blocks = tuple(_subtask(f"T-0{index}") for index in range(1, 4))
    root = str((tmp_path / "ws").resolve())
    constant = LoopIOImpl._constant_brief_entries(_ConstantCase({"workspace": lambda _s: root}, blocks))
    assert constant.get("workspace") == root

    # 空工作区 ⇒ 空值不算恒定（第一条 fail-closed）
    empty = LoopIOImpl._constant_brief_entries(_ConstantCase({"workspace": lambda _s: ""}, blocks))
    assert "workspace" not in empty


# --------------------------------------------------------------------------- #
# ② brief：本块写区（you_may_write / never_write / rule）
# --------------------------------------------------------------------------- #


def test_every_block_is_told_its_write_region(tmp_path: Path) -> None:
    """★ 写区进了 brief：知道"能写哪"（`you_may_write`）与"哪碰不得"（`never_write`）。"""
    _, _, briefs = _capture(tmp_path, "region")
    assert briefs, "no block was ever asked"
    for block, brief in briefs.items():
        region = brief.get("write_region")
        assert isinstance(region, dict), (block, sorted(brief))
        assert isinstance(region["you_may_write"], list), block
        assert isinstance(region["never_write"], list), block
        assert "相对工作目录" in region["rule"], block
        assert "工作目录【之外】" in region["rule"], block
        # ★ 与 `may_write` 同优先级：写区优先 ⇒ 同一个路径不许同时出现在两边
        assert not (set(region["you_may_write"]) & set(region["never_write"])), block


def test_the_blocks_own_output_is_in_its_write_region(tmp_path: Path) -> None:
    """★ 本块自己的产物是**永远可以写**的那一个（写区按块变）。"""
    _, _, briefs = _capture(tmp_path, "own-output")
    assert briefs
    for block, brief in briefs.items():
        output = brief["subtask"]["output"]
        assert output in brief["write_region"]["you_may_write"], (block, output, brief["write_region"])


class _RegionCase:
    """驱动 `_write_region_entry` 的最小替身：它只读这三个属性。"""

    def __init__(self, workspace, write_region, frozen_paths):  # type: ignore[no-untyped-def]
        self.workspace = workspace
        self.write_region = write_region
        self.frozen_paths = frozen_paths


def test_a_frozen_path_that_is_my_own_region_is_not_listed_as_forbidden() -> None:
    """★ 与 `may_write` 同序：**写区优先** —— 自己的产物不列进 `never_write`。

    一个块要改的那份来源（`fix` 类）会同时出现在交付物与来源里；若两边都点名，
    模型会读到"能写 X"和"不许写 X"并存 ⇒ 正是 harness 说的与做的不一致。
    """
    entry = LoopIOImpl._write_region_entry(_RegionCase(Path("/ws"), ("a.csv",), ("a.csv", "b.csv")))
    assert entry["you_may_write"] == ["a.csv"]
    assert entry["never_write"] == ["b.csv"]


def test_an_empty_write_region_is_described_by_the_real_rule() -> None:
    """★ 空区不许写成"没有写文件的权限"—— 那是**文案与执行语义矛盾**。

    `may_write(path, (), frozen_paths=('a.csv',))` 对不在 frozen 里的路径返回 **True**
    （写区之外的"草稿纸"可以写）。所以 frozen 已声明时，空区只能说"没有文件形式的交付区"，
    不能说"不许写任何文件"；只有 `frozen_paths is None`（退回旧规则）时才是真的不许写。
    """
    loose = LoopIOImpl._write_region_entry(_RegionCase(Path("/ws"), (), ("a.csv",)))
    assert loose["you_may_write"] == []
    assert "没有文件形式的交付区" in loose["rule"]
    assert "不许写任何文件" not in loose["rule"]

    strict = LoopIOImpl._write_region_entry(_RegionCase(Path("/ws"), (), None))
    assert "不许写任何文件" in strict["rule"]


def test_an_empty_path_in_the_region_is_not_advertised() -> None:
    """★ `partition="deliverable:"` ⇒ `write_region()` 返回 `("",)`：坏声明，但别再发给模型。

    空串不是一条路径；把它列成 `you_may_write` 等于给模型一个幻觉地址。
    （底层那句"声明不合法"该怎么具名报，是另一条账，本轮不动判定。）
    """
    entry = LoopIOImpl._write_region_entry(_RegionCase(Path("/ws"), ("",), ("a.csv",)))
    assert entry["you_may_write"] == []
    assert "" not in entry["never_write"]


def test_the_rule_never_claims_more_than_may_write_enforces() -> None:
    """★ 一致性：文案里"可以写"的那一类，`may_write` 必须真的放行（反之亦然）。"""
    from bobanana6.services.workspace import may_write

    entry = LoopIOImpl._write_region_entry(_RegionCase(Path("/ws"), ("mine.csv",), ("other.csv",)))
    assert entry["you_may_write"] == ["mine.csv"]
    assert entry["never_write"] == ["other.csv"]
    # 文案宣称：写区永远可写 + frozen 之外可以写临时文件
    assert may_write("mine.csv", ("mine.csv",), ("other.csv",)) is True
    assert may_write("scratch.tmp", ("mine.csv",), ("other.csv",)) is True
    assert "其它路径可以写" in entry["rule"]
    # 文案宣称：frozen 一个字节都不许改
    assert may_write("other.csv", ("mine.csv",), ("other.csv",)) is False


def test_an_undeclared_frozen_set_makes_the_rule_stricter_not_looser() -> None:
    """★ `frozen_paths is None`（调用方没声明别人的东西）⇒ 规则退回旧口径，不许放宽。"""
    entry = LoopIOImpl._write_region_entry(_RegionCase(Path("/ws"), ("a.csv",), None))
    assert "之外一律不许写" in entry["rule"]
    assert "临时文件" not in entry["rule"]


def test_without_a_workspace_there_is_no_write_region_entry() -> None:
    """部署没有工作区 ⇒ 文件工具根本不广告，不必（也不该）凭空宣布一个写区。"""
    assert LoopIOImpl._write_region_entry(_RegionCase(None, ("a.csv",), ("b.csv",))) == {}


# --------------------------------------------------------------------------- #
# ③ 工具返回：绝对 cwd（每轮一次校准）
# --------------------------------------------------------------------------- #


def test_shell_run_reports_an_absolute_cwd(tmp_path: Path) -> None:
    """★ 改前返回 `"cwd": "."`；改后是绝对路径，前台与后台两条路都改。"""
    root = (tmp_path / "ws").resolve()
    workspace = BoundWorkspace(root=root, write_region=("out.csv",), frozen_paths=("other.csv",))
    both = [
        workspace.run("echo hi"),
        workspace.run("echo hi", background=True),
    ]
    workspace.close()
    for result in both:
        assert result["cwd"] == str(root), result
        assert Path(result["cwd"]).is_absolute()
        assert result["cwd"] != "."


def test_run_code_reports_an_absolute_cwd(tmp_path: Path) -> None:
    """★ `code.run` 以前**没有** cwd 字段 ⇒ 27 轮代码之后模型只能自己猜输出目录。"""
    root = (tmp_path / "ws").resolve()
    workspace = BoundWorkspace(root=root, write_region=("out.csv",), frozen_paths=("other.csv",))
    result = workspace.run_code("1 + 1")
    workspace.close()
    assert result["cwd"] == str(root), result
    assert Path(result["cwd"]).is_absolute()
    assert result["value"] == "2"  # 既有字段一字未动
