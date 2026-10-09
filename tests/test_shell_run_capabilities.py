"""`shell.run` 的能力回归 —— 那个跑代码的工具删掉之后，能跑代码的只剩它。

本文件是上一个回合那份「先落盘再跑」测试里**能力级**断言的迁移落点。那个工具有的
三条能力必须由 `shell.run` 接住：

```
① 输出不丢   长输出正文截断，但全量可以取回（截断是换页，不是销毁）
② 超时真的掐 到点真的结束，不把整跑挂住（对照只读审计 #68：原工具没有超时）
③ 同等记账   跑命令落下的文件与 `files.write` 走同一本账（ledger 对账得到）
```

★ 迁移的是**能力**，不是**形态**。原工具是「正文留末尾 + `full_output_path` 落全量」；
  `shell.run` 是「正文留**开头** 4000 字符 + 全量靠命令自己重定向到文件」。两者不等价，
  差异（尾部截断 + 无 `full_output_path` 键 + 默认 60s 超时）如实写在删除报告里，
  这里不做粉饰：断言的是「输出不丢、可取回」，不是「键名还在」。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.services.workspace import BoundWorkspace, WorkspaceLedger
from tests.test_services import _full_toolset


def _workspace(tmp_path: Path) -> BoundWorkspace:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    ledger = WorkspaceLedger(root=root, region=("out.csv",))
    return BoundWorkspace(root=root, write_region=("out.csv",), ledger=ledger)


# --------------------------------------------------------------------------- #
# ① 输出不丢：正文截断、全量可取回
# --------------------------------------------------------------------------- #


def test_a_long_output_is_truncated_but_the_full_output_is_recoverable(tmp_path: Path) -> None:
    """★ 5000 行输出 ⇒ 正文被截、`truncated` 为真，全量在盘上读得回来。"""
    workspace = _workspace(tmp_path)
    result = workspace.run("seq 1 5000 | tee full.txt")
    workspace.close()

    assert result["exit_code"] == 0
    assert result["truncated"] is True
    assert len(result["stdout"]) <= 4000  # 正文有上限
    # 正文留的是**开头**（shell 的现状）；末尾那几行只在盘上的全量里
    assert result["stdout"].splitlines()[0] == "1"
    assert "5000" not in result["stdout"].splitlines()

    full = tmp_path / "ws" / "full.txt"
    assert full.is_file()
    lines = full.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5000  # ★ 全量一行不少
    assert lines[-1] == "5000"  # ★ 被正文丢掉的那一段，从这里拿得回来


def test_a_small_output_is_untouched(tmp_path: Path) -> None:
    """反证：没到上限就一个字符都不动。"""
    workspace = _workspace(tmp_path)
    result = workspace.run("printf 'small'")
    workspace.close()

    assert result["truncated"] is False
    assert result["stdout"] == "small"


# --------------------------------------------------------------------------- #
# ② 超时：到点真的掐（审计 #68 的对照面）
# --------------------------------------------------------------------------- #


def test_the_timeout_parameter_reaches_the_shell_and_actually_bites(tmp_path: Path) -> None:
    """★ `timeout` 参数真的会结束进程；不是「设了但没人看」。"""
    _tools, registry = _full_toolset(tmp_path)
    raw = {
        "id": "c-sleep",
        "type": "function",
        "function": {
            "name": "shell.run",
            # `exec` 让 sleep 成为直接子进程：杀它就能收盘，不留孤儿（审计 #32）。
            "arguments": json.dumps({"command": "exec sleep 30", "timeout": 1}),
        },
    }
    started = time.monotonic()
    result = registry.execute((registry.calls([raw])[0],))[0]
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.error_type == "TimeoutExpired"  # 失败是**可见**的，不静默
    assert elapsed < 20, elapsed  # 真的掐了，不是等满 30 秒


# --------------------------------------------------------------------------- #
# ③ 同等记账：命令的副作用进 ledger（与 files.write 同一本账）
# --------------------------------------------------------------------------- #


def test_a_shell_side_effect_is_accounted_like_a_file_write(tmp_path: Path) -> None:
    """★ `> out.csv` 落下的文件，`ledger.changed()` 看得见 —— 与 `files.write` 同一条路。"""
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    ledger = WorkspaceLedger(root=root, region=("out.csv",))
    workspace = BoundWorkspace(root=root, write_region=("out.csv",), frozen_paths=(), ledger=ledger)
    assert ledger.used is False and ledger.changed() == ()  # 没用过通道 2 时零开销

    workspace.run("echo obligor > out.csv && echo A-1 >> out.csv")
    workspace.close()
    assert ledger.used is True
    assert ledger.changed() == ("out.csv",)

    # 真产物与命令副作用在同一张清单里（判定器/溯源看的是同一份）
    workspace.write("notes.txt", "x\n")
    assert set(ledger.changed()) == {"out.csv", "notes.txt"}
