"""Text-form tool calls: declare the format, detect leaks, never execute them.

The measured failure (§〇): a model writes the call as text in the reply body, the shell
recognises nothing, the call never runs, and the model is never told -- so it repeats.
These tests pin the two halves: the detector (dialects + no false positives) and the
loop behaviour (no execution, a notice the model sees next turn).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.agentloop.events import EventLog
from bobanana6.agentloop.loop import LoopConfig, TurnProposal, WorkerLoop
from bobanana6.agentloop.metrics import text_tool_call_metrics
from bobanana6.agentloop.text_tool_calls import (
    DIALECT_FUNCTION_PARAMETER,
    DIALECT_TOOL_CALL_PARAM_KEY,
    TEXT_TOOL_CALL_NOTICE,
    TextToolCall,
    detect_text_tool_call,
)
from tests.test_agentloop_loop import FakeIO, candidate

#: 结构逐字取自归档 `qwen4b6-A-c1.jsonl` rec65（本地绝对路径换成中性值）。
DIALECT_A = (
    "<function=shell_run>\n<parameter=command>\nls -la input/\n</parameter>\n"
    "<parameter=timeout>\n10\n</parameter>\n</function>\n</tool_call>"
)
#: 同 rec120 的形态：被 max_tokens 截断，**没有任何闭合标签**。
DIALECT_A_TRUNCATED = "<function=shell_run>\n<parameter=command>\necho \"import pandas as pd\n"
#: 结构逐字取自归档 `xing29b-A-c1.jsonl` rec7（路径换成中性值）。
DIALECT_B = "<tool_call>write<param_key>path</param_key><param_value>process.py</param_value>"
#: 真归档里那条 24,597 字符记录的**逐字开头**（自包含回退；有归档时用全量）。
ARCHIVE = REPO / ".bobanana6" / "ab" / "archives" / "xing29b-A-c1.jsonl"
XING_PREFIX = "<tool_call>write<param_key>path</param_key><param_value>/mnt/data/process.py</param_value>"


# --------------------------------------------------------------------------- #
# ① 两种方言都能认出来（含被截断的那种）
# --------------------------------------------------------------------------- #


def test_dialect_a_function_parameter_is_detected() -> None:
    hit = detect_text_tool_call(DIALECT_A)
    assert hit is not None
    assert hit.dialect == DIALECT_FUNCTION_PARAMETER
    assert hit.names == ("shell_run", "command")
    assert hit.chars == len(DIALECT_A)


def test_a_truncated_dialect_a_is_still_detected() -> None:
    """★ rec120 就是这种：21,072 字符、没有闭合标签 —— 要求闭合就会漏掉最贵的那种。"""
    hit = detect_text_tool_call(DIALECT_A_TRUNCATED)
    assert hit is not None and hit.dialect == DIALECT_FUNCTION_PARAMETER


def test_dialect_b_tool_call_param_key_is_detected() -> None:
    hit = detect_text_tool_call(DIALECT_B)
    assert hit is not None
    assert hit.dialect == DIALECT_TOOL_CALL_PARAM_KEY
    assert hit.names == ("write", "path")
    assert hit.chars == len(DIALECT_B)


# --------------------------------------------------------------------------- #
# ② 反向：正常正文 0 误报，正文逐字节不变
# --------------------------------------------------------------------------- #

NORMAL_TEXTS = (
    '{"ref": "seed", "rows": [{"a": 1}], "tool_calls": []}',
    "math: a < b > c and 3 <= 4",
    "a bare <function> tag and a bare <parameter> with no =name",
    'an example: `<tool_call>{"name": "x"}</tool_call>` (JSON dialect, no param_key)',
    "closing junk: </parameter> </function> </tool_call>",
    "<tool>shell.run</tool><invoke>whatever</invoke>",
    "prose with an empty parameter: <function=shell_run><parameter=command></parameter>",
)


def test_normal_text_is_never_flagged() -> None:
    """★ 宁可漏认，不许错认：误报会污染整个循环。正常正文一个都不许命中。"""
    for text in NORMAL_TEXTS:
        assert detect_text_tool_call(text) is None, text


def test_an_example_inside_a_code_fence_is_not_a_call() -> None:
    """★ 归档里三次泄漏**都不在**代码块里；代码块里的示例不是调用。"""
    fenced = f"用法示例：\n\n```\n{DIALECT_A}\n```\n"
    assert detect_text_tool_call(fenced) is None
    assert fenced == f"用法示例：\n\n```\n{DIALECT_A}\n```\n"  # 正文逐字节未动


def test_an_unknown_dialect_is_left_alone() -> None:
    assert detect_text_tool_call("<invoke name='shell'><argument>x</argument></invoke>") is None


def test_a_dialect_b_with_an_empty_value_is_not_a_call() -> None:
    """★ 只读审计 #135：`<param_value></param_value>`（空值）不是调用 —— 与方言 A 同尺。"""
    assert detect_text_tool_call("<tool_call>write<param_key>path</param_key><param_value></param_value>") is None


def test_an_inline_code_example_is_not_a_call() -> None:
    """★ 只读审计 #135：行内反引号里的示例与代码围栏同一条取舍。"""
    inline = "写成这样不行：`<function=shell_run><parameter=command>ls</parameter></function>`"
    assert detect_text_tool_call(inline) is None


# --------------------------------------------------------------------------- #
# ③ 发现即告知：不执行、下一轮一定看得到、正文原样保留、记一笔
# --------------------------------------------------------------------------- #


def test_a_leaked_call_is_not_executed_and_the_notice_reaches_the_next_turn() -> None:
    io = FakeIO([TurnProposal(text=DIALECT_A, notice=TEXT_TOOL_CALL_NOTICE), TurnProposal(candidate=candidate())])
    result = WorkerLoop(io, config=LoopConfig(max_turns=3)).run(task_id="t", worker_id="w")

    assert result.exit_reason == "passed"
    assert io.executed == []  # ★ 绝不执行
    assert len(io.states) >= 2, "the model must be called again"

    second = list(io.states[1].messages)
    # ① 告知是一条 `user` 行（不是 tool 返回），模型下一轮一定看得到
    notices = [row for row in second if row.get("content") == TEXT_TOOL_CALL_NOTICE]
    assert notices == [{"role": "user", "content": TEXT_TOOL_CALL_NOTICE}]
    # ② 模型自己的正文**原样**留了一行（不截断、不改写）
    assert {"role": "assistant", "content": DIALECT_A} in second


def test_a_normal_text_finish_is_unchanged() -> None:
    """★ 反向：没有泄漏的普通文本轮次，行为与改前一致 —— 直接 `text_finish`，不注入。"""
    io = FakeIO([TurnProposal(text="a normal answer")])
    result = WorkerLoop(io, config=LoopConfig(max_turns=2)).run(task_id="t", worker_id="w")
    assert result.exit_reason == "text_finish"
    assert io.executed == []


# --------------------------------------------------------------------------- #
# ③b 审计修的三条：withheld 时告知不许丢、合法答案不许改道、下一轮不许锁 schema
# --------------------------------------------------------------------------- #


def test_the_notice_survives_a_withheld_candidate() -> None:
    """★ 只读审计 #133：`_retry` 排在 `_text_finish` 之前，withheld 非空时告知会被吞。

    触发：某轮候选被门拒（withheld 置位）之后，再出现一次文本形式调用。
    """
    from bobanana6.agentloop.gate import GateVerdict

    io = FakeIO(
        [
            TurnProposal(candidate=candidate()),
            TurnProposal(text=DIALECT_A, notice=TEXT_TOOL_CALL_NOTICE),
            TurnProposal(candidate=candidate()),
        ],
        gates=[GateVerdict("failed", "full")],
    )
    WorkerLoop(io, config=LoopConfig(max_turns=5)).run(task_id="t", worker_id="w")
    reached = any(
        any(row.get("content") == TEXT_TOOL_CALL_NOTICE for row in state.messages)
        for state in io.states[1:]
    )
    assert reached, "the notice was swallowed by the retry phase"


def test_produce_detects_the_leak_and_does_not_lock_the_next_schema(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """★ 只读审计 #134：命中后 `last_had_tool_calls=True` ⇒ 下一轮不挂 schema，工具通道才发得出。"""
    from tests.test_services import _merge_io, _ScriptedAnswers, _state

    io = _merge_io(tmp_path, DIALECT_A, None, client=_ScriptedAnswers([DIALECT_A]))
    proposal = io.produce(_state())  # type: ignore[arg-type]
    assert proposal.notice == TEXT_TOOL_CALL_NOTICE
    assert proposal.candidate is None
    assert io.last_had_tool_calls is True


def test_a_legitimate_answer_containing_the_text_is_not_redirected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """★ 只读审计 #136：合法答案里恰好引用了方言样文本 ⇒ 绝不被改道（先解析，成功就交付）。"""
    from tests.test_services import _merge_io, _ScriptedAnswers, _state

    answer = (
        '{"ref": "panel-c", "rows": [{"obligor": "C-001", "amount": 90000.0}], '
        '"summary": "<function=shell_run><parameter=command>ls</parameter></function>"}'
    )
    io = _merge_io(tmp_path, answer, None, client=_ScriptedAnswers([answer]))
    proposal = io.produce(_state())  # type: ignore[arg-type]
    assert proposal.candidate is not None
    assert proposal.notice == ""


# --------------------------------------------------------------------------- #
# ④ 记账：单独一格，不并进 tool.calls
# --------------------------------------------------------------------------- #


def test_the_leak_is_counted_on_its_own_metric() -> None:
    log = EventLog()
    log.record(
        kind="text_tool_call",
        task_id="t",
        worker_id="T-01",
        at=0.0,
        ok=False,
        detail={"dialect": DIALECT_TOOL_CALL_PARAM_KEY, "chars": 24_597},
    )
    metrics = text_tool_call_metrics(log.events)
    assert (metrics.calls, metrics.chars) == (1, 24_597)


def test_the_runner_metrics_expose_the_leak_counts() -> None:
    from bobanana6.assemble_impl.runner import Runner

    log = EventLog()
    log.record(
        kind="text_tool_call", task_id="t", worker_id="T-01", at=0.0, ok=False,
        detail={"dialect": DIALECT_FUNCTION_PARAMETER, "chars": 21_072},
    )
    runner = object.__new__(Runner)  # `_metrics` only reads `event_log`
    runner.event_log = log  # type: ignore[attr-defined]
    metrics = runner._metrics()  # type: ignore[attr-defined]
    assert metrics["text.tool_calls"] == 1
    assert metrics["text.tool_call_chars"] == 21_072
    assert metrics["tool.calls"] == 0  # ★ 不并进工具调用数


def test_note_text_tool_call_emits_the_event() -> None:
    from types import SimpleNamespace

    from bobanana6.assemble_impl.produce import LoopIOImpl

    io = object.__new__(LoopIOImpl)  # the method only reads log/task_id/subtask/epoch
    io.log = EventLog()
    io.task_id = "t"
    io.subtask = SimpleNamespace(id="T-01")
    io.epoch = lambda: 0.0
    io._note_text_tool_call(TextToolCall(DIALECT_FUNCTION_PARAMETER, 227, ("shell_run", "command"), DIALECT_A[:40]))
    event = io.log.events[0]
    assert (event.kind, event.ok) == ("text_tool_call", False)
    assert event.detail["dialect"] == "function_parameter" and event.detail["chars"] == 227


# --------------------------------------------------------------------------- #
# ⑤ 真实归档回放（§三⑥）：认出那条 24,597 字符的记录，且绝不执行
# --------------------------------------------------------------------------- #


def _archived_xing_text() -> tuple[str, int | None]:
    if not ARCHIVE.is_file():
        return XING_PREFIX, None
    for line in ARCHIVE.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        for value in _strings(record):
            if "<tool_call>" in value:
                return value, len(value)
    return XING_PREFIX, None


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_the_archived_xing29b_leak_is_recognised_and_never_executed() -> None:
    """★ 有归档时用真记录（24,597 字符）；没有时用它的逐字开头，测试仍自包含。"""
    text, real_chars = _archived_xing_text()
    hit = detect_text_tool_call(text)
    assert hit is not None
    assert hit.dialect == DIALECT_TOOL_CALL_PARAM_KEY
    assert hit.names == ("write", "path")
    if real_chars is not None:
        assert hit.chars == real_chars == 24_597
    # ★ 检测器是纯函数：它没有任何执行面（不 import 任何执行/注册表模块）
    import bobanana6.agentloop.text_tool_calls as module

    assert not hasattr(module, "execute")
