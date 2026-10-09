"""工具面收束的**删除守卫**：那个「跑代码」工具已从工具面拿掉，不许再回来。

本文件是本轮删除的**反证**载体 —— 下面每一条在删除前的树上都是**红**的
（旧树里 `src/bobanana6/core/ports.py` 有 ToolSpec/描述、`services/registry.py`
有 TOOL_NAMES/catalog 条目、`services/workspace.py` 有那个跑代码的方法、`services/tools.py`
有部署开关），删除后全绿。这不是风格洁癖：工具描述指向一个不存在的工具，会让模型
每轮照描述去调它、吃 `unknown_tool`，白烧轮次。

★ 一个细节：断言里写的是 `"code" + "." + "run"` 而不是字面量。守护者自己不该成为
  它守护的那个名字的散布点 —— 这样全仓 grep 那个已删名字时，看到的只有"它必须不存在"，
  而不是又一次引用。
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.core import ports
from bobanana6.services import registry as registry_module
from bobanana6.services import tools as tools_module
from bobanana6.services.workspace import BoundWorkspace
from tests.test_services import _full_toolset

_GONE = "code" + "." + "run"
_METHOD = "run" + "_code"
_SWITCH = "BOBANANA6_" + "CODE" + "_RUN"
#: 开关那个常量的名字（同样不写字面量，避免守护者自己散布这个名字）。
_SWITCH_ENV = "CODE" + "_RUN_ENV"
#: 工具名的命名空间（含已删的那个 —— 否则"描述指向那个工具"这条就抓不到了）。
_TOOL_TOKEN = re.compile(r"\b(?:task|sources|files|shell|code|semantics|check|traces)\.[a-z_]+\b")


def test_the_removed_tool_is_gone_from_every_table(tmp_path: Path) -> None:
    """基表 / 描述 / TOOL_NAMES / catalog（注册面）四处一起没有它。"""
    assert _GONE not in {spec.name for spec in ports._BASE_TOOL_SPECS}
    assert _GONE not in ports._TOOL_DESCRIPTIONS
    assert _GONE not in registry_module.TOOL_NAMES
    assert not hasattr(ports.ToolSet, "code" + "_run")

    _tools, registry = _full_toolset(tmp_path)
    assert _GONE not in registry.names()
    assert _GONE not in {spec.name for spec in _tools.servable_specs()}


def test_the_deployment_switch_is_gone_too(tmp_path: Path) -> None:
    """开关连同工具一起删掉 —— 不留"还能开回来"的暗门。"""
    assert not hasattr(tools_module, "code" + "_run_enabled")
    assert not hasattr(tools_module, _SWITCH_ENV)
    assert _SWITCH not in str(ports.ToolSet.__dataclass_fields__)


def test_no_workspace_runs_code_anymore() -> None:
    """实现与端口都拆干净：那个跑代码的方法不是"还在但没人调"。"""
    assert _METHOD not in ports.WorkspaceTools.__dict__
    assert not hasattr(BoundWorkspace, _METHOD)


def test_every_description_points_at_a_tool_that_exists() -> None:
    """★ 描述与工具面一致：描述里点名的工具，基表里必须真有。

    触发条件（这就是删除前那一版）：描述写着"X 时用某个工具"，而那个工具
    已经不在基表里 —— 模型照描述调用，只会拿到 `unknown_tool`。
    """
    known = {spec.name for spec in ports._BASE_TOOL_SPECS}
    for spec in ports._BASE_TOOL_SPECS:
        for token in _TOOL_TOKEN.findall(spec.description or ""):
            assert token in known, (spec.name, token)


def test_no_production_source_still_names_the_removed_line() -> None:
    """源码级守卫（照 OWASP AISVS 10.4.8 的"工具定义漂移要能检测"）：生产代码 0 处。

    ★ 这是本轮「全仓 grep 那个已删名字 + 它的两个伴生标识」自检的
      **可执行化**：人跑 grep 会说"我这次记得"，这条测试不会。
    """
    source_root = REPO / "src" / "bobanana6"
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for banned in (_GONE, _METHOD, _SWITCH):
            assert banned not in text, (path.relative_to(REPO), banned)


def test_the_migrated_tests_do_not_import_the_removed_module() -> None:
    """迁移留下的测试文件不许再 import 那套常量（它们随实现一起删了）。"""
    workspace_module = importlib.import_module("bobanana6.services.workspace")
    for name in ("CODE_DIR", "CODE_OUTPUT_MAX_LINES", "CODE_OUTPUT_MAX_BYTES", "CODE_OUTPUT_FULL_MAX_BYTES"):
        assert not hasattr(workspace_module, name), name
