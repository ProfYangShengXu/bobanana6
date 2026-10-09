"""工具面收束（核心层 + 按需层）。

证据（五轮 4B 实测）：模型把"重写一段脚本"当默认动作（一轮 18~41 次），
而 `files.patch` **五轮全是 0** —— 因为"重写"在工具面里**是默认**，而"改上一版"**不存在**。
本轮把它收成：**核心层**（干活的最小集）+ **按需层**（满足条件才亮）。

三条按需判据都要能从**运行时状态现算**，拿不到就不给（fail-closed）：

```
semantics.*   契约里真有语义声明吗      （任一交付物字段声明了类型以外的东西）
check.*       本块有交付物吗            （subtask.output ∈ contract.deliverables）
files.patch   本块已经写过至少一个文件吗（WriteBudget.paths 非空）
```
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl.produce import tool_schemas
from bobanana6.core.model import (
    DeliverableField,
    DeliverableSpec,
    ModelResponse,
    TaskContract,
)
from bobanana6.services.tools import declares_semantics
from bobanana6.services.workspace import BoundWorkspace
from tests.fakes import contract as bare_contract
from tests.fakes import toolset_for
from tests.test_convert_three import _block_of, _task, _tool_call
from tests.test_services import _full_toolset

CORE = ("shell.run", "files.write", "task.mysubtask", "task.contract")
SEMANTICS = {"semantics.lookup", "semantics.map", "semantics.check"}
CHECKS = {"check.local", "check.provenance", "check.assemble_dryrun"}


def _names(toolset) -> set[str]:  # type: ignore[no-untyped-def]
    return {spec.name for spec in toolset.specs()}


def _has(names: set[str], prefix: str) -> set[str]:
    return {name for name in names if name.startswith(prefix)}


# --------------------------------------------------------------------------- #
# ① 工具面读数：核心层一直在；按需层满足条件才加
# --------------------------------------------------------------------------- #


def test_the_core_layer_is_always_advertised(tmp_path: Path) -> None:
    """核心层：`shell.run` / `files.write` / `task.*` / `sources.*`（按授权）。"""
    workspace = BoundWorkspace(root=tmp_path / "ws", write_region=("out.csv",), frozen_paths=())
    toolset = replace(
        toolset_for("T-1", sources=True),
        workspace=workspace,
        semantics_declared=False,
        has_deliverable=True,
    )
    names = _names(toolset)
    assert set(CORE) <= names, sorted(names)
    assert {"sources.read", "sources.grep", "sources.snapshot"} <= names
    assert "files.patch" not in names  # 还没写过文件
    assert _has(names, "semantics.") == set()


def test_a_file_written_in_turn_one_brings_files_patch_in_turn_two(tmp_path: Path) -> None:
    """★ ②b + ①：本块写过文件 ⇒ `files.patch` **下一轮就出现**（而且是现算的）。"""
    workspace = BoundWorkspace(root=tmp_path / "ws", write_region=("out.csv",), frozen_paths=())
    toolset = replace(toolset_for("T-1"), workspace=workspace, has_deliverable=True)
    assert "files.patch" not in _names(toolset)
    workspace.write("out.csv", "obligor\nA-1\n")  # ← 块写了一个文件
    assert "files.patch" in _names(toolset)


def test_files_patch_needs_a_workspace_and_a_prior_write(tmp_path: Path) -> None:
    """★ ②a + fail-closed：没 workspace、或 workspace 里还没写过 ⇒ **不给**。"""
    without = replace(toolset_for("T-1"), has_deliverable=True)  # workspace=None
    assert "files.patch" not in _names(without)
    empty = replace(
        toolset_for("T-1"),
        workspace=BoundWorkspace(root=tmp_path / "ws", write_region=("out.csv",), frozen_paths=()),
        has_deliverable=True,
    )
    assert "files.patch" not in _names(empty)


def test_semantics_hides_unless_the_contract_declares_something() -> None:
    """★ ②c + 一条正面：裸类型契约 ⇒ 不给 `semantics.*`；声明了 domain ⇒ 给。"""
    bare = bare_contract()
    assert declares_semantics(bare) is False
    bare_toolset = replace(toolset_for("T-1"), semantics_declared=declares_semantics(bare))
    assert _has(_names(bare_toolset), "semantics.") == set()

    declared = TaskContract(
        task_id="task-1",
        goal="g",
        deliverables=(
            DeliverableSpec(
                name="out.csv",
                schema={"amount": DeliverableField(name="amount", type="float", domain="money")},
            ),
        ),
    )
    assert declares_semantics(declared) is True
    typed = replace(toolset_for("T-1"), semantics_declared=declares_semantics(declared))
    assert _has(_names(typed), "semantics.") == SEMANTICS


def test_check_hides_without_a_deliverable() -> None:
    """按需层：不产出交付物的块不该被塞一个"自查这一块过没过"。"""
    assert _has(_names(replace(toolset_for("T-1"), has_deliverable=False)), "check.") == set()
    assert _has(_names(replace(toolset_for("T-1"), has_deliverable=True)), "check.") == CHECKS


# --------------------------------------------------------------------------- #
# ②b 的接线面：菜单后出现的工具，**注册表必须派发得到**
# --------------------------------------------------------------------------- #


def test_the_registry_serves_more_than_it_advertises(tmp_path: Path) -> None:
    """★ 注册表按 `servable_specs()`（`specs()` 的超集）注册。

    否则：菜单后来出现了 `files.patch`，派发却答 "unknown tool" —— 模型照菜单调用却吃闭门羹。
    """
    tools, registry = _full_toolset(tmp_path)
    assert "files.patch" not in _names(tools)  # 此刻**不广告**
    assert "files.patch" in registry.names()  # 但**派发得到**
    assert {spec.name for spec in tools.specs()} <= set(registry.names())


def test_a_tool_that_appears_later_can_actually_be_called(tmp_path: Path) -> None:
    """端到端：先写、`files.patch` 出现、调用它**真的生效**（不是 unknown_tool）。"""
    _tools, registry = _full_toolset(tmp_path)
    root = tmp_path / "ws"
    (root / "out.csv").write_text("obligor\nA-1\nB-2\n", encoding="utf-8")
    raw = {
        "id": "c-patch",
        "type": "function",
        "function": {
            "name": "files.patch",
            "arguments": json.dumps(
                {"path": "out.csv", "edits": [{"oldText": "B-2", "newText": "C-3"}]}
            ),
        },
    }
    result = registry.execute((registry.calls([raw])[0],))[0]
    assert result.ok is True, result.content
    assert (root / "out.csv").read_text(encoding="utf-8") == "obligor\nA-1\nC-3\n"


# --------------------------------------------------------------------------- #
# ① 端到端读数：真跑里开局那份菜单就是收束后的样子
# --------------------------------------------------------------------------- #


def test_the_opening_menu_of_a_real_run_is_the_tightened_one(tmp_path: Path) -> None:
    """★ 开局：核心层 + 交付物带的 check；**没有** patch、**没有** semantics。"""
    task = _task(tmp_path, name="tightened")
    menus: dict[str, list[list[str]]] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages) or "?"
            names = sorted((entry.get("function") or {}).get("name") for entry in tools)
            menus.setdefault(block, []).append(names)
            turn = len(menus[block]) - 1
            if block == "T-01" and turn == 0:
                return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
            if block == "T-01" and turn == 1:
                return ModelResponse(
                    tool_calls=(_tool_call("w1", "files.write", {"path": "a.csv", "content": "obligor\nA-1\n"}),)
                )
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

    run_task(task, client=_Client(), env={}, workspace=True)
    assert menus, "no block was ever asked"
    opened = set(menus["T-01"][0])
    assert set(CORE) <= opened, sorted(opened)
    assert "files.patch" not in opened  # 开局：还没写过
    assert _has(opened, "semantics.") == set()  # 裸类型契约
    assert _has(opened, "check.") == CHECKS  # 本块有交付物
    assert _has(opened, "code.") == set()  # ★ 本轮把 `code.*` 删了（能力交给 shell.run）
    assert "traces.read" not in opened  # 按需：不是每个块都有轨迹可读

    # ★ 写过之后同一块里 `files.patch` 出现（收束不是"一次性削掉"）
    if len(menus["T-01"]) >= 3:
        assert "files.patch" in set(menus["T-01"][-1]), menus["T-01"]


def test_the_advertised_menu_can_grow_within_a_block(tmp_path: Path) -> None:
    """★ `files.write` 之后 `files.patch` 就在 schema 里了 —— 同一个对象，两次结果不同（现算）。"""
    workspace = BoundWorkspace(root=tmp_path / "ws", write_region=("out.csv",), frozen_paths=())
    toolset = replace(toolset_for("T-1"), workspace=workspace, has_deliverable=True)
    before = {entry["function"]["name"] for entry in tool_schemas(toolset)}
    workspace.write("out.csv", "x\n")
    after = {entry["function"]["name"] for entry in tool_schemas(toolset)}
    assert "files.patch" not in before and "files.patch" in after
    assert before <= after  # 只增（收束不靠"中途删"）


def test_the_advertised_menu_is_always_a_subset_of_what_the_registry_serves(tmp_path: Path) -> None:
    """★ 不变量：`specs()`（每轮广告）⊆ `servable_specs()`（注册表一次建成）。

    收束最危险的破法就在这里：某个门**依赖轮内会变的状态**，却没有走 `dynamic` 那一路
    ⇒ 注册表在块开始时把它丢掉了，菜单后来却亮了 ⇒ "unknown tool"。
    """
    workspace = BoundWorkspace(root=tmp_path / "ws", write_region=("out.csv",), frozen_paths=())
    configurations = (
        toolset_for("T-1"),
        toolset_for("T-1", sources=True),
        replace(toolset_for("T-1"), traces=object()),
        replace(toolset_for("T-1"), workspace=workspace),
        replace(toolset_for("T-1"), workspace=workspace, semantics_declared=True),
        replace(toolset_for("T-1"), workspace=workspace, semantics_declared=True, has_deliverable=True),
    )
    for toolset in configurations:
        served = {spec.name for spec in toolset.servable_specs()}
        advertised = _names(toolset)
        assert advertised <= served, sorted(advertised - served)
    # ★ 动门那一档的正面读数：写之前"注册面有、菜单没有"，写之后菜单才追上
    fresh = replace(
        toolset_for("T-1"),
        workspace=BoundWorkspace(root=tmp_path / "ws2", write_region=("out.csv",), frozen_paths=()),
    )
    assert "files.patch" in {spec.name for spec in fresh.servable_specs()}
    assert "files.patch" not in _names(fresh)
    fresh.workspace.write("out.csv", "x\n")  # type: ignore[union-attr]
    assert "files.patch" in _names(fresh)
