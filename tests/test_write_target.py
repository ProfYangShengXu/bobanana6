"""写块的产出目标（与读块对称）：契约声明 `min_chars` ⇒ 写块收到固定产出指令。

改动前的结构性不对称（可复核）：读块有 `_read_output_instruction`（告诉它把材料压成
「可引用要点 + 行号、一条一行、≤N 字符」），写块**什么产出指令都没有**，
`Subtask` 上也没有写侧的字段。后果就是文字产物写不厚、或者用脚本循环填模板。

本文件钉住补上的那一半：

```
① 声明了 min_chars ⇒ 写块的 write_instruction 非空，且含那个数字
② 没声明 ⇒ write_instruction == ""（逐字回到改动前的行为）
③ 措辞领域无关（不许写进本题的词）
④ 指令点名的工具本块真有；输入为空（没有 parts）时也不写出不成立的前提
⑤ ★ 反向：把 `_merge_blocks` 里那行 write_instruction=... 的产出拿掉 ⇒ ①必须红
```

★ 这条只加【指令】，**不加判定**：产物短了不拒收（要不要拒是下一步的事）。
★ `read_instruction` / `write_instruction` 是两个字段：那个的语义是读块的，不复用。
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from bobanana6.assemble_impl.produce import tool_schemas
from bobanana6.contract import parse_contract
from bobanana6.core.model import DeliverableSpec
from bobanana6.decompose import BlockBudget, Decomposer, split_by_source, strategies
from bobanana6.services.workspace import BoundWorkspace
from tests.fakes import toolset_for
from tests.test_decompose import _band_lines, ontology, research_contract, research_recon

#: 一道最小题：两个来源 ⇒ `split_by_source` 会切出读块 + 合并（写）块，
#: 合并块正是 `_merge_blocks` 造出来的那一个。
_BASE = """
task_id: write-target-1
goal: produce the artifact
deliverables:
  - {name: artifact.md, schema: {text: str}}
sources:
  - {ref: src-a}
  - {ref: src-b}
"""

#: 同一道题，只是多声明了一个体量下界。
_DECLARED = _BASE.replace("schema: {text: str}}", "schema: {text: str}, min_chars: 5000}")

#: 本题 / 该 vocabulary 式的领域词。写块指令是**恒定模板**，一个都不许出现
#: （否则就是把这道题过拟合进壳里）。
DOMAIN_WORDS = (
    "JD",
    "薪资",
    "攻略",
    "面试",
    "招聘",
    "面经",
    "简历",
    "求职",
    "字节",
    "腾讯",
    "阿里",
    "jd-matrix",
    "interview-bank",
    "roadmap",
)


def _contract(text: str):
    return parse_contract(text).contract


def _write_blocks(plan):
    return [block for block in plan.subtasks if block.mode == "write"]


# --------------------------------------------------------------------------- #
# ① 声明了体量目标 ⇒ 写块收到指令（含那个数字）
# --------------------------------------------------------------------------- #


def test_a_declared_floor_reaches_every_write_block() -> None:
    """★ 声明了 `min_chars` ⇒ 写块的产出指令非空，且把那体量目标写进指令。"""
    plan = split_by_source(_contract(_DECLARED), ontology())
    writes = _write_blocks(plan)
    assert writes
    for block in writes:
        assert block.write_instruction, block.id
        assert "WRITE BLOCK OUTPUT RULES." in block.write_instruction
        assert "5000" in block.write_instruction
        # 产出的是交付物【本身】，名字在场
        assert "artifact.md" in block.write_instruction


# --------------------------------------------------------------------------- #
# ② 没声明 ⇒ 逐字回到改动前（fail-closed）
# --------------------------------------------------------------------------- #


def test_an_undeclared_floor_hands_down_no_write_instruction() -> None:
    """★ 没声明 `min_chars` ⇒ 整条指令不下发，`Subtask` 上是一个空串。"""
    plan = split_by_source(_contract(_BASE), ontology())
    writes = _write_blocks(plan)
    assert writes
    assert all(block.write_instruction == "" for block in writes)
    # 两个字段分开：写块不占用读块那个字段
    assert all(block.read_instruction == "" for block in writes)
    # 缺省值就是 0 = 未声明
    assert DeliverableSpec(name="x").min_chars == 0
    # 直接问那个函数：0 不是"下发一条空指令"，是"不下发"
    assert strategies._write_output_instruction(deliverable_name="x", min_chars=0, has_parts=True) == ""
    assert strategies._write_output_instruction(deliverable_name="x", min_chars=-1, has_parts=True) == ""


def test_no_strategy_hands_down_a_write_instruction_without_a_declared_floor() -> None:
    """★ 覆盖所有写块构造点：没声明体量目标 ⇒ 任何策略的写块都拿空串。

    ★ 特别是 `split_read_then_product` —— 唯一给读块下发产出指令的那条路径
    （`research` 源展开 ≥2）；写块必须与它对称，否则"读块被告知写多少、写块没人管"。
    """
    plans = [
        Decomposer(strategy="source").split(_contract(_BASE), ontology()),
        Decomposer(strategy="field").split(_contract(_BASE), ontology()),
        Decomposer(strategy="single").split(_contract(_BASE), ontology()),
        Decomposer(strategy="source", block_budget=BlockBudget(max_chars=1000, min_chars=750)).split(
            research_contract(), ontology(), research_recon(_band_lines())
        ),
    ]
    for plan in plans:
        assert all(block.write_instruction == "" for block in _write_blocks(plan)), plan.shape


def test_the_volume_line_only_exists_when_a_floor_is_declared() -> None:
    """★ 只声明了体量目标才有"至少 N 字符"那一行；它是**体量**不是判定。"""
    text = strategies._write_output_instruction(deliverable_name="artifact.md", min_chars=5000, has_parts=True)
    assert "至少 5000 字符" in text
    # 不做上限、不做拒收 —— 这次只加指令，不加判定
    assert "至多" not in text
    assert "拒收" not in text and "会被判回来" not in text


# --------------------------------------------------------------------------- #
# ③ 措辞领域无关
# --------------------------------------------------------------------------- #


def test_the_write_instruction_carries_no_task_domain_word() -> None:
    """★ 恒定模板里不许出现本题的词；出现即过拟合。"""
    text = strategies._write_output_instruction(deliverable_name="artifact.md", min_chars=5000, has_parts=True)
    hits = [word for word in DOMAIN_WORDS if word in text]
    assert hits == [], f"write instruction leaked task-domain words: {hits}"
    # 也不点名任何一份具体来源
    assert "src-a" not in text and "src-b" not in text


# --------------------------------------------------------------------------- #
# ④ 不越权：点名的工具真有；空输入下指令也成立
# --------------------------------------------------------------------------- #


def test_the_named_tools_are_the_ones_this_block_really_has(tmp_path: Path) -> None:
    """★ 指令里点名的每一个工具 / 参数都必须是本块工具面里真的有的。"""
    text = strategies._write_output_instruction(deliverable_name="artifact.md", min_chars=5000, has_parts=True)
    assert "`files.write`" in text
    assert "`files.read`" in text
    assert "`append=true`" in text

    workspace = BoundWorkspace(root=tmp_path / "ws", write_region=("artifact.md",), frozen_paths=())
    toolset = replace(toolset_for("T-1"), workspace=workspace, has_deliverable=True)
    schemas = {entry["function"]["name"]: entry["function"] for entry in tool_schemas(toolset)}  # type: ignore[attr-defined]
    assert {"files.write", "files.read"} <= set(schemas), sorted(schemas)
    assert "append" in schemas["files.write"]["parameters"]["properties"]
    assert "offset" in schemas["files.read"]["parameters"]["properties"]


def test_the_instruction_holds_when_no_parts_were_handed_over() -> None:
    """★ 输入为空时不许写出不成立的前提（"你收到的输入里带着的行号"）。"""
    without = strategies._write_output_instruction(deliverable_name="artifact.md", min_chars=5000, has_parts=False)
    assert without
    assert "你收到的输入" not in without  # 没有 parts 就不许声称输入里有行号
    assert "来源" in without  # 换成不依赖 parts 的坐标说法
    # 其余每一条仍在：空输入不是"不下发指令"的借口
    for needle in ("`files.write`", "`files.read`", "至少 5000 字符", "脚本", "坐标"):
        assert needle in without, needle


# --------------------------------------------------------------------------- #
# ⑤ ★ 反向：删掉 `_merge_blocks` 里那行 ⇒ ①必须红
# --------------------------------------------------------------------------- #


def test_removing_the_merge_blocks_write_instruction_turns_the_floor_check_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 反向：`_merge_blocks` 里那行 `write_instruction=...` 是①的唯一来源。

    * 源码层面：那行真的在 `_merge_blocks` 里（删掉它 ⇒ 本断言红）；
    * 行为层面：把它的产出拿掉（helper 返回空串，等价于那行不下发）⇒ 写块全空。
    """
    source = inspect.getsource(strategies._merge_blocks)
    assert "write_instruction=_write_output_instruction(" in source
    plan = split_by_source(_contract(_DECLARED), ontology())
    assert any(block.write_instruction for block in _write_blocks(plan))
    monkeypatch.setattr(strategies, "_write_output_instruction", lambda **_: "")
    again = split_by_source(_contract(_DECLARED), ontology())
    assert all(block.write_instruction == "" for block in _write_blocks(again))
