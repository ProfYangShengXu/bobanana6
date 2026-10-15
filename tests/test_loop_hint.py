"""★ 打转要有出口：同一动作对同一目标反复 N 次后，在工具返回里追加一条【提示】。

现场（`acceptance/ab/agent-jd-research/`，4B，痕迹 commit `3ab893a`，T-01）：

```
28 次 shell.run 里有 16 次（t45–t60）换着 grep -c / grep -E / awk / sed 数同一个
./interview-bank.md —— **全部 ok=True**，所以只统计失败调用的
GuardrailConfig.repeat_block_after=4 / same_tool_block_after=6 一次都没拦到。
```

本文件钉住：

```
① 正例：16 次换正则数同一文件 ⇒ 从第 4 次起工具结果里出现提示（含「第 N 次」与文件名）
② 反例：开头 10 次 sources.read 读不同窗口 / 不同 ref ⇒ 不得出现（正常取数）
③ 反例：同一文件读 2-3 次（正常复核）⇒ 不出现
④ 提示文案领域无关（用中性目标名断言，模板里一个本题的词都没有）
⑤ ★ 反向：关掉注入点 ⇒ ①必须红
```

★ 是【提示】不是【拦截】：调用照跑、结果照返回，只是正文后面多一段。
"""

from __future__ import annotations

import pytest

from bobanana6.agentloop.loop import LoopConfig, TurnProposal, WorkerLoop
from bobanana6.agentloop.tools import (
    GuardrailConfig,
    ToolCall,
    ToolCallGuardrailController,
    ToolResult,
    loop_keys,
    shell_paths,
)

# --------------------------------------------------------------------------- #
# 现场原样：3ab893a:T-01 的 t45–t60，16 条命令（换着正则数同一个文件的形状）
# --------------------------------------------------------------------------- #

T01_COUNTING_COMMANDS: tuple[str, ...] = (
    "wc -l ./interview-bank.md",
    'grep -c "^\\\\([0-9]\\\\|##\\\\|[0-9]\\\\*\\\\*\\\\*Q:" ./interview-bank.md',
    'grep -E "^\\\\d+\\\\.\\\\*\\\\*\\\\*Q:" ./interview-bank.md | wc -l',
    'grep -c "\\\\*\\\\*Q:" ./interview-bank.md',
    "stat ./jd-matrix.md ./interview-bank.md ./roadmap.md ./summary.json",
    'grep -E "^\\\\d+\\\\." ./interview-bank.md | wc -l',
    'grep -E "^\\\\d+\\\\.\\\\*" ./interview-bank.md | wc -l',
    "awk '/^\\\\d+\\\\./{count++}END{print count}' ./interview-bank.md",
    "awk 'BEGIN{count=0} /^\\\\d+\\\\./{count++}END{print count}' ./interview-bank.md",
    "cut -d: -f1 ./jd-matrix.md",
    "sed -n '/^\\\\d+\\\\.*/p' ./interview-bank.md | wc -l",
    'grep -c "^\\\\d*Q:" ./interview-bank.md',
    "head -60 ./interview-bank.md",
    'grep -E "^[0-9]+\\\\ \\\\*\\\\*Q:" ./interview-bank.md | wc -l',
    "sed -n '/Q: /{N;p}' ./interview-bank.md | grep -c \"^[0-9]\\\\+\\\\ \" ; wc -l ./interview-bank.md",
    "awk '/Q:/{c++}END{print c}' ./interview-bank.md",
)

DOMAIN_WORDS = (
    "JD",
    "面试",
    "题库",
    "薪资",
    "攻略",
    "招聘",
    "面经",
    "简历",
    "求职",
    "大厂",
    "腾讯",
    "阿里巴巴",
    "百度",
    "美团",
    "华为",
    "jd-matrix",
    "interview-bank",
    "roadmap",
)


# --------------------------------------------------------------------------- #
# 一个最小的 LoopIO：每个 turn 交一条调用，最后交 text 收尾
# --------------------------------------------------------------------------- #


class _ScriptedIO:
    def __init__(self, calls: tuple[ToolCall, ...]) -> None:
        self._calls = list(calls)
        self.states: list = []

    def produce(self, state):  # type: ignore[no-untyped-def]
        self.states.append(state)
        if self._calls:
            return TurnProposal(tool_calls=(self._calls.pop(0),))
        return TurnProposal(text="done")

    def execute(self, calls):  # type: ignore[no-untyped-def]
        return tuple(ToolResult(call_id=call.call_id, ok=True, content=f"r:{call.call_id}") for call in calls)

    def verify(self, _candidate):  # type: ignore[no-untyped-def]
        raise AssertionError("no candidate was ever handed over")


def _run(calls: tuple[ToolCall, ...]) -> list[str]:
    """Drive the real loop and return the content of every tool row, in order."""
    io = _ScriptedIO(calls)
    loop = WorkerLoop(io, config=LoopConfig(max_turns=len(calls) + 4, max_retries=1, max_reflections=0))
    result = loop.run(task_id="t", worker_id="w")
    return [str(row["content"]) for row in result.messages if row.get("role") == "tool"]


def _shell_call(call_id: str, command: str) -> ToolCall:
    return ToolCall(call_id=call_id, name="shell.run", arguments={"command": command}, effect="write")


def _counting_calls() -> tuple[ToolCall, ...]:
    return tuple(_shell_call(f"c{index}", command) for index, command in enumerate(T01_COUNTING_COMMANDS))


# --------------------------------------------------------------------------- #
# 取键：shell 从命令抽它触碰的文件（不能用 (tool, 完整 args)）
# --------------------------------------------------------------------------- #


def test_shell_paths_extracts_the_file_from_every_real_counting_command() -> None:
    """★ T-01 的 16 条命令，除 t54（`cut ... ./jd-matrix.md`）外都指向 `interview-bank.md`。"""
    hits = [shell_paths(command) for command in T01_COUNTING_COMMANDS]
    missing = [index for index, paths in enumerate(hits) if "interview-bank.md" not in paths]
    assert missing == [9], hits  # t54 本来就不碰面试文件
    assert sum(1 for paths in hits if paths == ("interview-bank.md",)) == 14


def test_shell_paths_ignores_commands_flags_and_absolute_paths() -> None:
    """反例：命令名 / 旗标 / 绝对路径不是"目标"，别把共享前缀制造出假循环。"""
    assert shell_paths("ls -la output/") == ()
    assert shell_paths("cd /mnt/c/Users/me/_run/workspace && pwd") == ()
    assert shell_paths("python3 -c 'import os; print(1)'") == ()
    assert shell_paths("curl https://example.com/x.json") == ()
    # 相对的多个文件照收；绝对路径与系统路径丢掉
    assert shell_paths("cd /srv/ws && grep -c x ./a.md ./b/c.csv") == ("a.md", "b/c.csv")


def test_loop_keys_is_per_file_for_shell_run_not_per_command_text() -> None:
    """★ 16 条命令文本各不相同，键必须都落在同一个文件上。"""
    keys = [loop_keys(_shell_call(f"c{index}", command)) for index, command in enumerate(T01_COUNTING_COMMANDS)]
    targets = [target for item in keys for _key, target in item]
    assert targets.count("interview-bank.md") == 15  # 15 条命令真的碰它（含 stat 那条多目标）
    counting_keys = {key for item in keys for key, target in item if target == "interview-bank.md"}
    assert len(counting_keys) == 1  # 全部同一个键 —— 换正则不会换键


def test_loop_keys_pages_a_declared_target_as_distinct_windows() -> None:
    """★ 声明了 target 的工具：读不同窗口 / 换 pattern 是【不同】动作（正常取数）。"""
    first = ToolCall(call_id="1", name="sources.read", arguments={"ref": "hunt:1", "offset": 0}, targets=("hunt:1",))
    second = ToolCall(call_id="2", name="sources.read", arguments={"ref": "hunt:1", "offset": 100}, targets=("hunt:1",))
    grep_one = ToolCall(call_id="3", name="sources.grep", arguments={"pattern": "a", "ref": "hunt:1"}, targets=("hunt:1",))
    grep_two = ToolCall(call_id="4", name="sources.grep", arguments={"pattern": "b", "ref": "hunt:1"}, targets=("hunt:1",))
    assert loop_keys(first) != loop_keys(second)  # 不同窗口 = 不同动作
    assert loop_keys(grep_one) != loop_keys(grep_two)  # 不同 pattern = 不同动作
    assert loop_keys(first) == loop_keys(
        ToolCall(call_id="5", name="sources.read", arguments={"ref": "hunt:1", "offset": 0}, targets=("hunt:1",))
    )  # 同一个窗口重复读 = 同一动作


# --------------------------------------------------------------------------- #
# ① 正例：16 次数同一个文件 ⇒ 从第 4 次起有提示
# --------------------------------------------------------------------------- #


def test_sixteen_regexes_over_one_file_get_the_hint_from_the_fourth_time() -> None:
    """★ 复现 T-01 形状：16 条命令全部 ok=True，旧的失败守卫一条都拦不到。"""
    rows = _run(_counting_calls())
    hinted = [row for row in rows if "第" in row and "次用 `shell.run`" in row]
    assert len(hinted) == 12, hinted  # 15 次碰它，第 4 次起 = 12 条带提示
    first = rows[3]  # 第 4 条命令（t48）
    assert "第 4 次" in first and "interview-bank.md" in first, first
    # 前三次不得有提示（不是看见可疑就报）
    assert all("边缘核对" not in row for row in rows[:3]), rows[:3]
    # 三件事都在：计数 / 定性 / 出口
    assert "第 4 次用 `shell.run` 对 `interview-bank.md`" in first
    assert "边缘核对" in first
    assert "换个办法" in first and "可有可无的核对" in first and "放弃它" in first
    assert "主线是把产物写出来交掉" in first
    # ★ 提示不是拦截：调用照跑、结果照返回
    assert all(row.startswith("r:") for row in rows)


def test_the_hint_never_blocks_or_changes_the_result() -> None:
    """★ 判定语义一个字没改：同一批里 `before_call` 全放行，结果原样在。"""
    controller = ToolCallGuardrailController()
    marks = []
    for index in range(6):
        call = _shell_call(f"c{index}", f"grep -c x{index} ./interview-bank.md")
        marks.append(controller.before_call(call))
    assert marks == [None] * 6 and controller.halted is False
    hinted = controller.loop_hint(_shell_call("c5", "grep -c x5 ./interview-bank.md"))
    assert hinted.startswith("\n★ 提示（不是拦截）")


# --------------------------------------------------------------------------- #
# ② 反例：开头 21 次 sources.read/grep 是正常取数，不得误报
# --------------------------------------------------------------------------- #


def test_reading_different_windows_and_refs_is_not_a_loop() -> None:
    """★ 反向：T-01 开头 10 次读，参数不同（不同 ref / 不同 offset）⇒ 零提示。"""
    calls = (
        ToolCall(call_id="r1", name="sources.read", arguments={"ref": "hunt", "offset": 0, "limit": 17}, targets=("hunt",)),
        ToolCall(call_id="r2", name="sources.read", arguments={"ref": "hunt:1", "offset": 0, "limit": 406}, targets=("hunt:1",)),
        ToolCall(call_id="r3", name="sources.grep", arguments={"pattern": "a", "ref": "hunt:1"}, targets=("hunt:1",)),
        ToolCall(call_id="r4", name="sources.grep", arguments={"pattern": "b", "ref": "hunt:1"}, targets=("hunt:1",)),
        ToolCall(call_id="r5", name="sources.grep", arguments={"pattern": "c", "ref": "hunt:1"}, targets=("hunt:1",)),
        ToolCall(call_id="r6", name="sources.read", arguments={"ref": "hunt:2", "offset": 0, "limit": 100}, targets=("hunt:2",)),
        ToolCall(call_id="r7", name="sources.read", arguments={"ref": "hunt:3", "offset": 0, "limit": 100}, targets=("hunt:3",)),
        ToolCall(call_id="r8", name="sources.grep", arguments={"pattern": "d", "ref": "hunt:1"}, targets=("hunt:1",)),
        ToolCall(call_id="r9", name="sources.read", arguments={"ref": "hunt:2", "offset": 45, "limit": 80}, targets=("hunt:2",)),
        ToolCall(call_id="r10", name="sources.read", arguments={"ref": "hunt:4", "offset": 0, "limit": 100}, targets=("hunt:4",)),
    )
    rows = _run(calls)
    assert all("边缘核对" not in row for row in rows), rows


def test_reading_the_same_file_two_or_three_times_is_not_yet_a_loop() -> None:
    """★ 反例：同一文件复核 2-3 次是正常动作，阈值以下不响。"""
    calls = (
        ToolCall(call_id="f1", name="files.read", arguments={"path": "output/draft.md", "offset": 0, "limit": 50}, targets=("output/draft.md",)),
        ToolCall(call_id="f2", name="files.read", arguments={"path": "output/draft.md", "offset": 50, "limit": 50}, targets=("output/draft.md",)),
        ToolCall(call_id="f3", name="files.read", arguments={"path": "output/draft.md", "offset": 100, "limit": 50}, targets=("output/draft.md",)),
        # 同一个窗口重复读三次也仍在阈值以下
        ToolCall(call_id="f4", name="files.read", arguments={"path": "output/draft.md", "offset": 0, "limit": 50}, targets=("output/draft.md",)),
        ToolCall(call_id="f5", name="files.read", arguments={"path": "output/draft.md", "offset": 0, "limit": 50}, targets=("output/draft.md",)),
    )
    rows = _run(calls)
    assert all("边缘核对" not in row for row in rows), rows


# --------------------------------------------------------------------------- #
# ④ 领域无关
# --------------------------------------------------------------------------- #


def test_the_hint_template_carries_no_task_domain_word() -> None:
    """★ 提示是恒定模板（本项目代码），不许写进任何一道题的词。

    目标名是**数据**（像 contract.goal 一样），所以用中性目标名断言模板本身；
    送达由 ① 的 `interview-bank.md` 断言保证。
    """
    controller = ToolCallGuardrailController()
    hint = ""
    for index in range(4):
        call = _shell_call(f"c{index}", f"grep -c x{index} ./counts.md")
        assert controller.before_call(call) is None
        hint = controller.loop_hint(call)
    assert "counts.md" in hint
    leaked = [word for word in DOMAIN_WORDS if word in hint]
    assert leaked == [], f"the loop hint leaked task-domain words: {leaked}"


# --------------------------------------------------------------------------- #
# ⑤ ★ 反向：关掉注入点 ⇒ ①必须红
# --------------------------------------------------------------------------- #


def test_turning_the_injection_point_off_turns_the_positive_test_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 反向：把 `_deliver_tool_result` 里的提示追加拿掉 ⇒ 16 条命令一条提示都没有。

    证明 ① 钉的是**这个注入点**，不是"正文里恰好还有别的同义句"。
    """

    def _page_only(self, turn, call, result):  # type: ignore[no-untyped-def]
        return self._page_tool_result(turn, result, call.name if call is not None else "")

    monkeypatch.setattr(WorkerLoop, "_deliver_tool_result", _page_only)
    rows = _run(_counting_calls())
    assert all("边缘核对" not in row for row in rows), rows


def test_the_hint_can_be_disabled_by_config() -> None:
    """★ 阈值可配：`repeat_hint_after=0` 关掉（`before_call` 的拦截语义不动）。"""
    controller = ToolCallGuardrailController(GuardrailConfig(repeat_hint_after=0))
    for index in range(6):
        call = _shell_call(f"c{index}", f"grep -c x{index} ./counts.md")
        assert controller.before_call(call) is None
        assert controller.loop_hint(call) == ""
