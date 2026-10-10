"""`shell.run` 的能力回归 —— 输出换页 / 全量落盘 / 超时收树 / 同等记账。

★ 这里考的四条，是上一轮删掉那个"跑代码"工具时**如实退回的真损失**的补法：

```
① 输出不丢   超过预算 ⇒ 正文留【头 + 尾】两段，并说清省了多少；全量落 full_output_path
② 没截不动   没超预算 ⇒ 一个字符不动、**一个文件都不落**（反向）
③ 超时收树   到点不只杀 shell，连孙进程一起收（审计 #32/#68 的对照面）
④ 同等记账   命令落下的**产物**与 files.write 走同一本账；被截落下的**全量文件不算产物**
```

★ 与 `bobanana6_flash` 的取舍差异（尾截 vs 头尾 / 默认超时 / killpg / 落盘位置）见
  `shell.run补四条-20261009.md` —— 这里只做断言，不复述理由。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.agentloop.compress import truncate_tool_results
from bobanana6.core import ports
from bobanana6.services.workspace import BoundWorkspace, WorkspaceLedger
from tests.test_services import _full_toolset


def _workspace(tmp_path: Path) -> BoundWorkspace:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    ledger = WorkspaceLedger(root=root, region=("out.csv",))
    return BoundWorkspace(root=root, write_region=("out.csv",), frozen_paths=(), ledger=ledger)


def _running(pid: int) -> bool:
    """The process is still running (a zombie counts as dead -- it holds no work)."""
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False
    try:
        state = stat.read_text(encoding="utf-8").split(") ", 1)[1].split(" ", 1)[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


# --------------------------------------------------------------------------- #
# ① 换页：头 + 尾 + 全文落盘
# --------------------------------------------------------------------------- #


def test_a_long_output_keeps_head_and_tail_and_spills_the_full_output(tmp_path: Path) -> None:
    """★ 5000 行 ⇒ 正文头尾都在、明说省了多少，全量在 `full_output_path` 里读得回来。"""
    workspace = _workspace(tmp_path)
    result = workspace.run("seq 1 5000")
    workspace.close()

    assert result["exit_code"] == 0
    assert result["truncated"] is True
    body = result["stdout"].splitlines()
    assert body[0] == "1"  # 头在（表头/前几行）
    assert body[-1] == "5000"  # ★ 尾也在（旧行为只留头，尾部被静默丢掉）
    assert any("output truncated" in line and "omitted" in line for line in body)

    assert result["full_output_path"].startswith("_shell/")
    full = (tmp_path / "ws" / result["full_output_path"]).read_text(encoding="utf-8")
    assert len(full.splitlines()) == 5000  # ★ 全量一行不少
    assert full.splitlines()[0] == "1" and full.splitlines()[-1] == "5000"
    assert result["full_output_lines"] == 5000
    assert result["full_output_bytes"] == len(full.encode("utf-8"))


def test_both_streams_are_paged_and_the_spill_carries_both(tmp_path: Path) -> None:
    """stdout 与 stderr 各自换页；落盘那份两股流都在，且有一条分界。"""
    workspace = _workspace(tmp_path)
    result = workspace.run("seq 1 5000; seq 1 5000 >&2")
    workspace.close()

    assert result["truncated"] is True
    assert result["stdout"].splitlines()[0] == "1" and result["stdout"].splitlines()[-1] == "5000"
    assert result["stderr"].splitlines()[0] == "1" and result["stderr"].splitlines()[-1] == "5000"

    full = (tmp_path / "ws" / result["full_output_path"]).read_text(encoding="utf-8")
    assert "----- stderr -----" in full
    assert result["full_output_bytes"] > 20_000


def test_a_short_output_is_untouched_and_spills_nothing(tmp_path: Path) -> None:
    """★ 反证：没超预算 ⇒ 一个字符不动、一个文件不落、没有那几个键。"""
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    ledger = WorkspaceLedger(root=root, region=("out.csv",))
    workspace = BoundWorkspace(root=root, write_region=("out.csv",), frozen_paths=(), ledger=ledger)

    result = workspace.run("printf 'small'")
    workspace.close()

    assert result["truncated"] is False
    assert result["stdout"] == "small"
    # ★ 反向：连**键序**都与改前一致（"没截断 = 原样"要能逐字对得上）
    assert list(result) == ["background", "exit_code", "cwd", "stdout", "stderr", "truncated"]
    assert "full_output_path" not in result
    assert "full_output_lines" not in result and "full_output_bytes" not in result
    assert sorted(path.name for path in root.iterdir()) == []  # ★ 没有落盘文件
    assert ledger.changed() == ()


def test_two_blocks_spilling_do_not_collide(tmp_path: Path) -> None:
    """★ 两块共用一个工作区 ⇒ 落盘文件名不许撞车（各自的内容都得留着）。"""
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    first = BoundWorkspace(
        root=root, write_region=("a.csv",), frozen_paths=(),
        ledger=WorkspaceLedger(root=root, region=("a.csv",)),
    )
    second = BoundWorkspace(
        root=root, write_region=("b.csv",), frozen_paths=(),
        ledger=WorkspaceLedger(root=root, region=("b.csv",)),
    )
    left = first.run("seq 1 5000")
    right = second.run("seq 1 6000")
    first.close()
    second.close()

    assert left["full_output_path"] != right["full_output_path"]
    assert len((root / left["full_output_path"]).read_text().splitlines()) == 5000
    assert len((root / right["full_output_path"]).read_text().splitlines()) == 6000


# --------------------------------------------------------------------------- #
# ② 超时：到点真掐 + 收整棵树（审计 #32/#68 的对照面）
# --------------------------------------------------------------------------- #


def test_the_timeout_parameter_reaches_the_shell_and_actually_bites(tmp_path: Path) -> None:
    """★ `timeout` 参数真的会结束进程；不是「设了但没人看」。"""
    _tools, registry = _full_toolset(tmp_path)
    raw = {
        "id": "c-sleep",
        "type": "function",
        "function": {
            "name": "shell.run",
            "arguments": json.dumps({"command": "exec sleep 30", "timeout": 1}),
        },
    }
    started = time.monotonic()
    result = registry.execute((registry.calls([raw])[0],))[0]
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.error_type == "TimeoutExpired"  # 失败是**可见**的，不静默
    assert elapsed < 20, elapsed  # 真的掐了，不是等满 30 秒


def test_a_timeout_does_not_hang_on_a_grandchild_that_escaped_the_group(tmp_path: Path) -> None:
    """★ P0：`setsid` 的孙进程脱离进程组、又握着管道 ⇒ 只 `killpg` 不收尸会**挂死整块**。

    触发条件：超时后台命令 `setsid … &`（daemonize 一类）——`killpg` 收不到它，
    而它还握着 stdout/stderr 的写端；裸 `communicate()` 会一直等 EOF。这里断言 1s 内抛
    `TimeoutExpired`，不是等到逃逸进程自己睡完。
    """
    workspace = _workspace(tmp_path)
    root = tmp_path / "ws"
    escaped = root / "escaped.pid"
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            workspace.run("setsid sh -c 'echo $$ > escaped.pid; sleep 30' & sleep 30", timeout=1)
        elapsed = time.monotonic() - started
    finally:
        workspace.close()
        # 清掉逃逸的那个孤儿，别让它活过测试
        if escaped.exists():
            try:
                os.kill(int(escaped.read_text(encoding="utf-8")), signal.SIGKILL)
            except (OSError, ValueError):
                pass
    assert elapsed < 10, elapsed


def test_a_spill_failure_does_not_destroy_the_output(tmp_path: Path) -> None:
    """★ 落盘失败 ⇒ **不销毁**：宁可把全量给它，也不留一份"截了却取不回"的输出。"""
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "_shell").write_text("not a directory", encoding="utf-8")  # 建目录必失败
    workspace = BoundWorkspace(
        root=root, write_region=("out.csv",), frozen_paths=(),
        ledger=WorkspaceLedger(root=root, region=("out.csv",)),
    )
    result = workspace.run("seq 1 5000")
    workspace.close()

    assert "full_output_path" not in result
    assert "full_output_error" in result and "Error" in result["full_output_error"]
    assert result["truncated"] is False  # 没换成页 ⇒ 不截
    assert len(result["stdout"].splitlines()) == 5000  # ★ 中间段一行没少


def test_a_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    """★ `subprocess.run(timeout=…)` 只杀 shell ⇒ 孙进程留下；本仓必须收整棵树。"""
    workspace = _workspace(tmp_path)
    root = tmp_path / "ws"
    child = root / "child.py"
    child.write_text(
        "import os, time\n"
        "open('grandchild.pid', 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    # 复合命令：外层 sh 必须留着（不能把自己 exec 掉）⇒ 孙进程才真的挂在它下面。
    with pytest.raises(subprocess.TimeoutExpired):
        workspace.run(f'"{sys.executable}" child.py ; echo done', timeout=1)

    pid_file = root / "grandchild.pid"
    for _ in range(100):
        if pid_file.exists():
            break
        time.sleep(0.02)
    assert pid_file.exists(), "the grandchild never started"
    pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(100):
        if not _running(pid):
            break
        time.sleep(0.02)
    workspace.close()
    assert not _running(pid), f"grandchild {pid} survived the timeout"


# --------------------------------------------------------------------------- #
# ③ 记账：产物进 ledger；全量文件**不进**（照 `_code/` 的先例）
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


def test_the_spill_file_is_not_a_deliverable(tmp_path: Path) -> None:
    """★ 截断发生了，但产物清单**不变**：`_shell/` 被单点排除在 ledger 之外。"""
    workspace = _workspace(tmp_path)
    assert workspace.ledger is not None

    result = workspace.run("seq 1 5000")
    workspace.close()

    assert (tmp_path / "ws" / result["full_output_path"]).is_file()
    assert workspace.ledger.changed() == ()  # ★ 一个字节的落盘都不算产物


# --------------------------------------------------------------------------- #
# ④ 可观测性：元数据键必须排在正文之前（压缩按头部截 2000 字符）
# --------------------------------------------------------------------------- #


def test_the_paging_metadata_survives_head_truncation(tmp_path: Path) -> None:
    """★ 长会话里压缩会把 tool 结果按**头部**截 2000 字符 ⇒ 路径键必须在前面。

    用**长行**输出（5000 行 × 500 字符）把预览顶到 ~12KB，才会真的撞上 2000 的压缩尺子。
    """
    _tools, registry = _full_toolset(tmp_path)
    raw = {
        "id": "c-big",
        "type": "function",
        "function": {
            "name": "shell.run",
            "arguments": json.dumps({"command": "seq -f '%0500g' 1 5000"}),
        },
    }
    result = registry.execute((registry.calls([raw])[0],))[0]
    assert result.ok is True
    assert len(result.content) > 2000

    capped, retained = truncate_tool_results([{"role": "tool", "content": result.content}], 2000)
    assert retained, "the payload was expected to be over the 2000-char cap"
    head = capped[0]["content"][:2000]
    assert '"full_output_path"' in head and '"truncated"' in head


# --------------------------------------------------------------------------- #
# ⑤ 描述：模型必须知道默认超时（否则不知道只有 60 秒）
# --------------------------------------------------------------------------- #


def test_the_shell_description_states_the_default_and_max_timeout() -> None:
    spec = next(item for item in ports._BASE_TOOL_SPECS if item.name == "shell.run")
    assert "默认 60" in spec.description
    assert "上限 600" in spec.description
