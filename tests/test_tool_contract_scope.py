"""★ 缺口①：工具契约的硬边界必须出现在**模型看得见的那句话**里。

反例（改前实测）：`limit` 的唯一权威是 `services/textops.py:MAX_LIMIT=2000`，
执行点是 `_integer(..., maximum=MAX_LIMIT)`，但 `ToolSpec.description` 只写
"limit = 读几行"，`tool_schemas()` 又把参数渲染成 `{"type": "string"}`（没有
`maximum`）⇒ 4B 只能靠一次 `TextArgumentError: limit must be <= 2000, got 35000`
去发现边界（T-06 的首错）。写区/读区同理：真实清单只在 brief 里，工具描述是抽象说法。

本文件钉住三件事：

1. 数值上限（`≤ 2000`、默认 `200`、单次写 `200000` 字节）出现在对应描述里；
2. 写区 / 授权来源的**实际值**（本块现算）出现在对应描述里；
3. ★ 判定逻辑**一行没动**：越界 `limit` 仍然被 `TextArgumentError` 拒，
   `may_write` 的语义与改前一致 —— 这次只改"说给模型听的那句话"。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bobanana6.assemble_impl.produce import tool_schemas
from bobanana6.services.textops import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    TextArgumentError,
    line_window,
)
from bobanana6.services.workspace import BoundWorkspace, may_write
from tests.test_services import _full_toolset


def _descriptions(toolset) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {entry["function"]["name"]: entry["function"]["description"] for entry in tool_schemas(toolset)}


def test_the_read_tools_state_the_real_limit_and_the_authorized_refs(tmp_path: Path) -> None:
    toolset, _registry = _full_toolset(tmp_path)
    descriptions = _descriptions(toolset)

    for name in ("sources.read", "files.read"):
        assert f"最多 {MAX_LIMIT} 行" in descriptions[name], (name, descriptions[name])
        assert f"默认 {DEFAULT_LIMIT} 行" in descriptions[name], (name, descriptions[name])
    # context 也被同一个上限卡（textops.py:157），描述不能漏
    assert f"context 最多 {MAX_LIMIT} 行" in descriptions["sources.grep"]
    # 授权来源的实际清单：ref 只能取这些
    assert "src-1" in descriptions["sources.read"]
    assert "src-1" in descriptions["sources.grep"]
    assert "src-1" in descriptions["sources.snapshot"]


def test_the_write_tools_state_the_real_region_and_the_bytes_cap(tmp_path: Path) -> None:
    toolset, _registry = _full_toolset(tmp_path)
    # `files.patch` 是轮内按需出现的（本块真的写过文件之后），先写一次再取菜单。
    toolset.workspace.write("out.csv", "x\n")  # type: ignore[union-attr]
    descriptions = _descriptions(toolset)

    for name in ("files.write", "files.read", "files.patch"):
        assert "out.csv" in descriptions[name], (name, descriptions[name])
    assert "you_may_write" in descriptions["files.write"]
    assert "200000 字节" in descriptions["files.write"]
    assert "200000 字节" in descriptions["files.patch"]
    # 没有冻结路径时不许声称有一条 never_write（描述要照执行语义写）
    assert "never_write=" not in descriptions["files.write"]


def test_an_empty_write_region_is_stated_as_no_file_scope(tmp_path: Path) -> None:
    toolset, _registry = _full_toolset(tmp_path)
    blocked = replace(
        toolset,
        workspace=BoundWorkspace(root=tmp_path / "empty", write_region=(), frozen_paths=()),
    )
    description = _descriptions(blocked)["files.write"]
    assert "没有文件写区" in description
    assert "you_may_write=[]" not in description


def test_the_description_says_the_limit_but_the_judgement_is_unchanged() -> None:
    """★ 只补描述、不新增第二处校验：越界必须仍然被**同一个**错误拒。"""
    with pytest.raises(TextArgumentError, match=f"limit must be <= {MAX_LIMIT}"):
        line_window("a\n" * 10, limit=35000)
    # 边界本身仍被接受（不是靠描述"放宽"出来的）
    assert line_window("a\n" * 10, limit=MAX_LIMIT)["limit"] == MAX_LIMIT
    # 写区判定语义不变：写区内可写；frozen_paths=None 时区外不可写
    assert may_write("out.csv", ("out.csv",), None) is True
    assert may_write("other.csv", ("out.csv",), None) is False


def test_the_write_description_no_longer_points_at_the_wrong_read_tool() -> None:
    """★ 审计 #773：`files.write` 曾让模型"写完用 sources.read 回读" —— 那是授权来源，不是回读入口。"""
    from bobanana6.core.ports import _TOOL_DESCRIPTIONS

    text = _TOOL_DESCRIPTIONS["files.write"]
    assert "files.read" in text
    assert "sources.read 回读" not in text
    # 而 sources.read 仍然只服务授权来源（不是回读入口）
    assert "sources.read" in _TOOL_DESCRIPTIONS["files.read"]


def test_no_sources_means_no_ref_list_is_invented(tmp_path: Path) -> None:
    toolset, _registry = _full_toolset(tmp_path)
    without = replace(toolset, sources=None)
    schemas = tool_schemas(without)
    names = {entry["function"]["name"] for entry in schemas}
    assert "sources.read" not in names  # capability gate unchanged
