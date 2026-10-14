"""重试：退避 · 回灌原因 · **不吃回合预算** —— 实测暴露的壳的账。

证据（`.bobanana6/ab/archives/qwen4b14-A-c1.jsonl`）：
T-01 / T-02 被 `HTTP 503 System is too busy now` 打挂。分类与动作**都对**
（`errors.py:338` 认 503 ⇒ `OVERLOADED`；`errors.py:87` 映射 `RETRY_LOCAL`），
但 `loop.py` 的重试那一支是**直接 continue**：

    RETRY   → cont("retry_after_error")   ← 不等待、也不告诉模型刚才为什么失败
    REFLECT → 注入一条 user 消息（原因）   ← 只有这一支才"说"

三条账：

① **不退避**：503 是"服务太忙"，立刻重发等于往火上浇油 —— 两次瞬间用完，块被判死。
   （429 有分钟级退避，但那是 adapter 层的，503 走不到那儿。）
② **不说**：31.8MB 归档里失败原因一次都没进过模型收到的上下文 ⇒ 它只会原样再交一次。
   ★ 但"说"要分两半：服务端忙说了没用（它重发的是一模一样的东西），**只该退避**。
③ **吃回合预算**：`run()` 的 `while state.turn < max_turns` 每轮 +1，而重试走的正是"再来一轮"，
   可 `runtime.py` 把 `max_turns` 接到了 `budget.max_calls`（BB6_MAX_CALLS 那个总闸）
   ⇒ 重试挤掉的是**正式回合**的额度。用户口径：重试的闸是它自己的计数器。

★ 这里测的是**规则**（退避发生了吗 / 原因进上下文了吗 / 回合数还回去了吗），
  不看任何 13f 的数字。下面几条读数都用实测标定过（见各用例的读数注释）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.adapters.openai_compat import LLMError
from bobanana6.agentloop.errors import classify_error
from bobanana6.agentloop.gate import GateVerdict
from bobanana6.agentloop.loop import LoopConfig, TurnProposal, WorkerLoop
from bobanana6.core.model import Candidate


class _IO:
    """最小 IO：前 `fail_times` 次产出抛 `error`，之后交一个能过的候选。"""

    def __init__(self, *, fail_times: int = 1, error: BaseException | None = None, gates=None) -> None:
        self.fail_times = fail_times
        self.error = error or RuntimeError("boom")
        self._gates = list(gates or [])
        self.seen: list[tuple[dict, ...]] = []
        self.produces = 0
        self.verify_calls = 0

    def produce(self, state):  # type: ignore[no-untyped-def]
        self.produces += 1
        if self.produces <= self.fail_times:
            raise self.error
        self.seen.append(tuple(state.messages))
        return TurnProposal(candidate=Candidate(subtask_id="T-01", source_id="s", hint="a"))

    def execute(self, calls):  # type: ignore[no-untyped-def]
        return ()

    def verify(self, _candidate):  # type: ignore[no-untyped-def]
        self.verify_calls += 1
        return self._gates.pop(0) if self._gates else GateVerdict("passed", "full")


def _busy() -> LLMError:
    """服务端忙：真实报文就是这一句（`code:50508`）。"""
    return LLMError("model call failed with HTTP 503: System is too busy now", status_code=503)


def _fixable() -> RuntimeError:
    """不是服务端类的失败（分类不出来 ⇒ `reason='unknown'` ⇒ `_UNKNOWN_ACTION = RETRY_LOCAL`）。"""
    return RuntimeError("the answer could not be assembled")


def _run(io: _IO, **overrides):  # type: ignore[no-untyped-def]
    config = LoopConfig(**{"max_turns": 6, "retry_backoff_s": 0.0, **overrides})
    return WorkerLoop(io, config=config).run(task_id="t", worker_id="w")


# --------------------------------------------------------------------------- #
# ⓪ 前提：这两种错误真的落在不同的分类上（不然下面几条测的就不是它们）
# --------------------------------------------------------------------------- #


def test_the_two_fixtures_are_in_fact_classified_differently() -> None:
    """★ 前提断言：503 是**服务端类**，`unknown` 不是 —— 否则下面几条测错了对象。"""
    assert str(classify_error(_busy()).reason) == "overloaded"
    assert str(classify_error(_fixable()).reason) == "unknown"


# --------------------------------------------------------------------------- #
# ① 退避：服务端类等一等，别的立刻重试
# --------------------------------------------------------------------------- #


def test_a_server_side_failure_waits_before_retrying() -> None:
    """★ 503 这类：**退避发生**（等一分钟再打），而不是立刻重发。

    读数（实测标定）：失败 1 次后成功 ⇒ 恰好退避一次、出口 `passed`。
    """
    slept: list[float] = []
    io = _IO(fail_times=1, error=_busy())
    result = _run(io, retry_backoff_s=60.0, sleeper=slept.append)
    assert slept == [60.0], slept
    assert io.verify_calls == 1, io.verify_calls
    assert result.exit_reason == "passed", result.exit_reason


def test_a_fixable_failure_does_not_wait() -> None:
    """★ 模型自己能改的失败：**不等**（等也不会变好）—— 它该做的是被告知原因。"""
    slept: list[float] = []
    io = _IO(fail_times=1, error=_fixable())
    _run(io, retry_backoff_s=60.0, sleeper=slept.append)
    assert slept == [], slept


def test_the_default_is_no_sleep() -> None:
    """★ 默认 0（单测/离线不真睡）：真跑由入口显式打开（`run_bb6.py` 设 60）。

    这条是"既有读数一字不变"的看门狗 —— 默认一旦变成正数，
    每一个驱动服务端失败的用例都会真睡，跑测时间会被静默拖长。
    """
    slept: list[float] = []
    io = _IO(fail_times=1, error=_busy())
    _run(io, sleeper=slept.append)
    assert slept == [], slept
    assert LoopConfig().retry_backoff_s == 0.0


# --------------------------------------------------------------------------- #
# ② 回灌原因：模型可改的说，服务端类的不说
# --------------------------------------------------------------------------- #


def test_a_fixable_failure_puts_something_new_in_front_of_the_model() -> None:
    """★ 不说，它只会原样再交一次 —— 实测代价就是这个（归档里一次都没说）。

    判据用**差分**（服务端类 vs 可改类），不依赖注入文案的措辞：
    可改类的下一轮上下文里**多出**一条消息，服务端类的不多。
    """
    fixable = _IO(fail_times=1, error=_fixable())
    _run(fixable)
    server = _IO(fail_times=1, error=_busy())
    _run(server)

    assert fixable.seen and server.seen, "两者都该被再问一次"
    assert len(fixable.seen[-1]) > len(server.seen[-1]), (
        "可改的失败没有把原因告诉模型",
        len(fixable.seen[-1]),
        len(server.seen[-1]),
    )


def test_a_server_side_failure_does_not_add_a_message() -> None:
    """★ 服务端忙**不告诉模型**：它重发的是一模一样的东西，说了只是白占上下文。"""
    io = _IO(fail_times=1, error=_busy())
    _run(io, retry_backoff_s=60.0, sleeper=lambda _s: None)
    assert io.seen, "模型该被再问一次"
    # 只有最初那一轮的空上下文，没有多出来的 user 消息
    assert len(io.seen[-1]) == 0, io.seen[-1]


# --------------------------------------------------------------------------- #
# ③ 重试不吃回合预算
# --------------------------------------------------------------------------- #


def test_a_retry_does_not_spend_a_turn() -> None:
    """★★ 反证主体：重试的那一轮**还回去**。

    读数（实测标定）：`max_turns=1` 且一直失败 ⇒ **仍然跑出 3 次产出**
    （1 正式 + 2 次重试，受 `max_retries=3` 卡住）。
    · 退还 ⇒ 回合数被还回去，循环条件一直成立 ⇒ 产出次数 > 1
    · 不退还 ⇒ `max_turns=1` 用完就退 ⇒ 产出次数 == 1
    两者给出**不同**的读数，所以这条能真的分辨改没改。
    """
    io = _IO(fail_times=99, error=_busy())
    result = _run(io, max_turns=1, max_retries=3, max_reflections=0)
    assert io.produces > 1, ("重试吃掉了回合预算", io.produces)
    assert io.produces == 3, io.produces
    assert result.turns <= 1, ("退还之后不该再涨回合数", result.turns)


def test_the_turn_cap_no_longer_decides_how_many_retries_happen() -> None:
    """★ 同一个事实的另一面：把 `max_turns` 调大**不改变**重试次数。

    （改前：重试走的就是回合 ⇒ `max_turns` 越大重试越多，两者耦合。）
    """
    readings = set()
    for max_turns in (1, 2, 6):
        io = _IO(fail_times=99, error=_busy())
        _run(io, max_turns=max_turns, max_retries=3, max_reflections=0)
        readings.add(io.produces)
    assert readings == {3}, readings


def test_retries_are_still_bounded_by_their_own_counter() -> None:
    """★ 退还**不会**失控：`max_retries` 仍是硬闸（`attempts` 只增不减）。"""
    io = _IO(fail_times=99, error=_busy())
    result = _run(io, max_turns=10, max_retries=2, max_reflections=0)
    assert result.exit_reason not in {"passed", "retry", "retry_after_error"}, result.exit_reason
    assert io.produces == 2, io.produces  # 1 正式 + 1 重试，到顶就停


def test_the_default_gives_one_shot_plus_three_retries() -> None:
    """★ 用户口径："重试失败三次后向外部求助" ⇒ 1 次正式 + **3 次重试**。

    ★ 读数口径：闸是 `attempts < max_retries`，而 `attempts` 数的是**总失败次数**
    （含第一次）⇒ 默认值必须是 **4**，不是 3。
    """
    assert LoopConfig().max_retries == 4
    io = _IO(fail_times=99, error=_busy())
    _run(io, max_turns=1, max_reflections=0)
    assert io.produces == 4, ("默认没给到 1 正式 + 3 重试", io.produces)
