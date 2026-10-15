"""求助出口（`EscalationPort`）—— 重试与反思都用完之后的**最后一步**。

证据（`.bobanana6/ab/archives/qwen4b14-A-c1.jsonl`）：三个块连挂三跑，
失败读数**逐字相同** —— 壳手里再没有新东西可换。重试换的是"再打一次"，
反思换的是"把事实再说一遍"，两个都用完时，真正缺的是**换一个脑子看一遍**，
那是壳自己产不出来的东西，只能从外面要。

四条纪律（每条都有对应的用例）：

① **拿不到答复 = 正常返回，不是失败**（`answered=False`）——
   交不出去不该让这一块更糟，它本来就快做不成了 ⇒ 落回原来的降级出口。
② **端口抛异常不许杀掉整跑** —— 与 `_publish_disk_artifact` 同一条纪律：
   兜底路径上的 IO 风险要就地收口（那一处它一抛，杀掉的不是这一块而是整个 run）。
③ **求助只给一次**（`max_escalations=1`）—— 它是贵动作：等的是外面，不是自己。
④ **求助不吃回合预算** —— 与重试同类：花的是"再试一次"的额度。

★ 这里测的是**规则**，不看任何 13f 的数字；B 臂（`EscalationPort` 省略）的既有读数一字不变。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bobanana6.adapters.openai_compat import LLMError
from bobanana6.agentloop.gate import GateVerdict
from bobanana6.agentloop.loop import LoopConfig, TurnProposal, WorkerLoop
from bobanana6.agentloop.recovery import RecoveryAction, decide_recovery
from bobanana6.core.model import Candidate, EscalationOutcome, EscalationRequest


class _IO:
    """最小 IO：先失败 `fail_times` 次，之后交一个能过的候选。"""

    # blame: whitelist — 测试替身的默认值；省略即"不失败、不自带判据"
    def __init__(self, *, fail_times: int = 1, record: list | None = None) -> None:
        self.fail_times = fail_times
        self.produces = 0
        self.record = record if record is not None else []

    def produce(self, state):  # type: ignore[no-untyped-def]
        self.produces += 1
        if self.produces <= self.fail_times:
            raise LLMError("model call failed with HTTP 503: System is too busy now", status_code=503)
        self.record.append(tuple(state.messages))
        return TurnProposal(candidate=Candidate(subtask_id="T-01", source_id="s", hint="a"))

    def execute(self, calls):  # type: ignore[no-untyped-def]
        return ()

    def verify(self, _candidate):  # type: ignore[no-untyped-def]
        return GateVerdict("passed", "full")


class _Port:
    """假出口：记下收到的求助，按剧本回一个答复。"""

    # blame: whitelist — 测试替身；省略即"不答复"（测的就是拿不到时的那条路）
    def __init__(self, *, answered: bool = True, advice: str = "先看 X 再看 Y", explode: bool = False) -> None:
        self.answered = answered
        self.advice = advice
        self.explode = explode
        self.seen: list[EscalationRequest] = []

    def escalate(self, request: EscalationRequest) -> EscalationOutcome:
        self.seen.append(request)
        if self.explode:
            raise RuntimeError("the port itself is broken")
        return EscalationOutcome(answered=self.answered, advice=self.advice if self.answered else "", detail="test")


def _run(io: _IO, port=None, **overrides):  # type: ignore[no-untyped-def]
    """★ `max_retries=1` / `max_reflections=0`：一次失败就到底 —— 好让"到头了"这一步立刻发生。"""
    config = LoopConfig(**{"max_turns": 6, "retry_backoff_s": 0.0, "max_retries": 1, "max_reflections": 0, **overrides})
    return WorkerLoop(io, config=config, escalation=port).run(task_id="t", worker_id="w")


# --------------------------------------------------------------------------- #
# ⓪ 前提：没有出口时，行为与改前逐字相同（B 臂的读数不能被动到）
# --------------------------------------------------------------------------- #


def test_without_a_port_nothing_changes() -> None:
    """★ 看门狗：不注入端口 ⇒ 一次失败即降级，**绝不**凭空"求助"。"""
    io = _IO(fail_times=99)
    result = WorkerLoop(io, config=LoopConfig(max_turns=6, retry_backoff_s=0.0, max_retries=1, max_reflections=0)).run(
        task_id="t", worker_id="w"
    )
    assert io.produces == 1, io.produces
    assert result.exit_reason in {"degrade_single_agent", "declined", "failed"}, result.exit_reason


def test_recovery_only_escalates_when_there_is_an_exit() -> None:
    """★ 规则在 `decide_recovery` 里，不在循环里：同一组输入，有出口才出 ESCALATE。"""
    from bobanana6.agentloop.errors import classify_error

    action = classify_error(LLMError("model call failed with HTTP 503: System is too busy now", status_code=503)).action

    def call(can: bool):  # type: ignore[no-untyped-def]
        # ★ `attempts=5 = max_retries(4) + max_reflections(1)`：这正是"重试与反思都用完"那一步。
        return decide_recovery(action, attempts=5, max_retries=4, max_reflections=1, can_escalate=can).action

    assert call(True) is RecoveryAction.ESCALATE
    assert call(False) is RecoveryAction.DEGRADE_SINGLE_AGENT


def test_a_server_side_failure_never_reaches_reflection() -> None:
    """★★ 回归闸：503 在两个 REFLECT 出口上都要被挡住。

    实测（本文件，2026-10-14）：只堵了第一个出口（带 `detail` 的那支）时，
    `attempts=4, max_retries=4, max_reflections=1` ⇒ 503 落到**第二个**出口
    （"重试用完了 ⇒ 反思一下"），还是"注入原因 + 不退避" —— 与用户口径
    （**等一分钟再打**）正好相反，而且把没用的报错文本白灌进上下文。

    ★ `failure=` 必须按**真实调用点**那样传（`loop.py` 传的是 `classified.reason`）——
      这个入参是"服务端类"的唯一判据，忘了传闸门就静默失效（本文件第一次就是这么写的）。
    ★ 这里扫一遍 attempts 全区间：只要有一个落进 REFLECT 就挂 —— 出口以后再加也不会漏。
    """
    from bobanana6.agentloop.errors import classify_error

    classified = classify_error(LLMError("model call failed with HTTP 503: System is too busy now", status_code=503))
    for attempts in range(8):
        for detail in ("", "HTTP 503 System is too busy now"):
            decision = decide_recovery(
                classified.action,
                attempts=attempts,
                max_retries=4,
                max_reflections=1,
                failure=classified.reason,  # ★ 与 loop.py:974 同一个形状
                detail=detail,
            )
            assert decision.action is not RecoveryAction.REFLECT, (attempts, detail, decision.reason)


def test_a_server_side_failure_gets_no_reflection_in_the_real_loop() -> None:
    """★★ 端到端闸（打的是**真实接线点**，不是规则本身）。

    上面那条测的是规则；这条确保**接线**也把身份传对了 ——
    规则再对，接线点漏传 `failure` 就等于没有（"机制在但没起作用"那一族）。
    """
    io = _IO(fail_times=1)
    _run(io, max_retries=4, max_reflections=1)  # 不注入端口 ⇒ 走原来的路
    assert io.produces == 2, io.produces  # 第一次失败后重试、第二次成功
    blob = " ".join(str(m) for m in io.record[-1])
    assert "do not repeat the same input unchanged" not in blob, blob[-400:]


def test_a_fixable_failure_still_gets_reflected() -> None:
    """★ 反向对照：模型**自己能改**的失败，该反思的还是要反思（别把闸门焊死）。"""
    from bobanana6.agentloop.errors import classify_error

    classified = classify_error(RuntimeError("the answer could not be assembled"))
    decision = decide_recovery(
        classified.action, attempts=4, max_retries=4, max_reflections=1, failure=classified.reason, detail="列名对不上"
    )
    assert decision.action is RecoveryAction.REFLECT, decision.reason


# --------------------------------------------------------------------------- #
# ① 拿到答复 ⇒ 注入 + 再跑一轮
# --------------------------------------------------------------------------- #


def test_an_answered_escalation_is_fed_back_to_the_block() -> None:
    """★ 拿到答复 ⇒ 作为一条消息交回给块，它据此再试一次（用户原话："你直接帮他解决"）。"""
    io = _IO(fail_times=1)
    port = _Port(answered=True, advice="判据声明它约束 output/x.csv，先把该文件的列名对齐")
    result = _run(io, port=port, max_escalations=1)

    assert len(port.seen) == 1, port.seen
    assert result.exit_reason == "passed", result.exit_reason
    assert io.produces == 2, io.produces
    # ★ 答复真的进了模型看得到的上下文（不是记在日志里）
    assert any("先把该文件的列名对齐" in str(m) for m in io.record[-1]), io.record[-1]


def test_the_request_carries_the_evidence() -> None:
    """★ 送出去的是"做不到 + 原因 + 证据"，不是一句"帮帮我"。"""
    io = _IO(fail_times=1)
    port = _Port()
    _run(io, port=port)
    request = port.seen[0]
    assert request.task_id == "t"
    assert request.block == "w"
    assert request.reason == "overloaded", request.reason
    assert request.attempts >= 1, request.attempts


def test_asking_the_outside_does_not_spend_a_turn() -> None:
    """★ 与重试同类：求助那一轮**还回去** —— 求助挤掉的不能是正式回合的额度。"""
    io = _IO(fail_times=1)
    _run(io, port=_Port(), max_turns=1)
    assert io.produces == 2, io.produces


# --------------------------------------------------------------------------- #
# ② 拿不到答复 ⇒ 落回降级（不许把"没拿到"当"拿到了"）
# --------------------------------------------------------------------------- #


def test_an_unanswered_escalation_falls_back_to_degrade() -> None:
    """★ 交不出去 = 正常返回，不是失败，更不是成功 —— 落回原来那条降级出口。"""
    io = _IO(fail_times=99)
    result = _run(io, port=_Port(answered=False))
    assert result.exit_reason != "passed", result.exit_reason
    assert io.produces == 1, io.produces


def test_a_broken_port_does_not_kill_the_run() -> None:
    """★ 端口是实现方的代码，它抛异常不该杀整跑（就地收口 ⇒ 当作没拿到）。"""
    io = _IO(fail_times=99)
    result = _run(io, port=_Port(explode=True))
    assert result.exit_reason != "passed", result.exit_reason


def test_the_outside_is_only_asked_once() -> None:
    """★ 上限 1：答复再不对也不无限打扰外面（求助是贵动作）。"""
    io = _IO(fail_times=99)
    port = _Port()
    _run(io, port=port, max_escalations=1)
    assert len(port.seen) == 1, port.seen


# --------------------------------------------------------------------------- #
# ③ 接线：env 默认关（既有读数一字不变）
# --------------------------------------------------------------------------- #


def test_the_exit_is_off_unless_the_entry_point_opens_it() -> None:
    """★ 默认关 —— 单测/离线路径一个字都不变；真跑由入口显式打开。"""
    from bobanana6.assemble_impl.runtime import escalation_from_env

    assert escalation_from_env({}) is None
    for off in ("", "0", "false", "no", "off", "OFF"):
        assert escalation_from_env({"BB6_ESCALATE": off}) is None, off
    opened = escalation_from_env({"BB6_ESCALATE": "1"})
    assert opened is not None
    assert opened.agent  # 桥里接这件活的那一方（写死在实现里，不在 core）


# --------------------------------------------------------------------------- #
# ④ 求助文本：自包含（对面没有你的上下文）
# --------------------------------------------------------------------------- #


def test_the_ask_is_self_contained() -> None:
    """★ 三条纪律的机械可查部分：带全关键事实、说明用途、不写死步骤。"""
    from bobanana6.services.escalation import compose_request

    text = compose_request(
        EscalationRequest(task_id="13f", block="T-03", reason="overloaded", attempts=4, detail="HTTP 503")
    )
    for must in ("13f", "T-03", "overloaded", "HTTP 503"):
        assert must in text, must
    assert "答复" in text  # 说清了"你给的东西会被交给谁"（决定它怎么取舍）


def test_a_broken_answer_reads_as_nothing() -> None:
    """★ 读不回来 ⇒ 当作"没拿到"，绝不猜一封答复出来。"""
    from bobanana6.services.escalation import parse_lines

    assert parse_lines(["noise", "more noise"]).answered is False
    assert parse_lines(['{"answered": true, "advice": "ok"}']).answered is True
    assert parse_lines(['{"answered": "yes-please"}']).answered is True
    assert parse_lines([]).answered is False


# --------------------------------------------------------------------------- #
# ⑤ 送达方式可换：不许把某个平台/某台机器的解释器写死
# --------------------------------------------------------------------------- #


def _reload_with(monkeypatch, **env: str):
    """按给定的环境变量重新加载模块 —— 探测发生在 import 期，只有这样才测得到。"""
    import importlib

    import bobanana6.services.escalation as module

    for name in ("BB6_BRIDGE_PYTHON", "BB6_WINDOWS_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return importlib.reload(module)


def test_without_any_setting_it_falls_back_to_the_running_interpreter(monkeypatch) -> None:
    """★ 核心断言：**没有配任何东西**时不许依赖某个平台的绝对路径。

    换到纯 Linux、或桥与本进程同栈时，这一步就该能用 ——
    以前这里默认指向一个 Windows 的 `python.exe` 绝对路径，
    等于"换个环境必断"，而且断的方式是启动失败，不是具名结果。
    """
    module = _reload_with(monkeypatch)
    assert module.DEFAULT_HELPER_PYTHON == sys.executable
    assert "python.exe" not in module.DEFAULT_HELPER_PYTHON


def test_the_generic_name_wins_over_the_legacy_one(monkeypatch) -> None:
    module = _reload_with(monkeypatch, BB6_BRIDGE_PYTHON="/opt/py/bin/python3",
                          BB6_WINDOWS_PYTHON=r"C:\other\python.exe")
    assert module.DEFAULT_HELPER_PYTHON == "/opt/py/bin/python3"


def test_the_legacy_name_still_works(monkeypatch) -> None:
    """★ 已有部署写的是老名字，不能因为改名就废掉它们。"""
    module = _reload_with(monkeypatch, BB6_WINDOWS_PYTHON=r"C:\legacy\python.exe")
    assert module.DEFAULT_HELPER_PYTHON == r"C:\legacy\python.exe"


def test_blank_values_are_ignored_not_obeyed(monkeypatch) -> None:
    """★ 空串 = 没配。不许把空串当解释器去起进程（那会报一个看不懂的错）。"""
    module = _reload_with(monkeypatch, BB6_BRIDGE_PYTHON="   ")
    assert module.DEFAULT_HELPER_PYTHON == sys.executable


def test_the_chosen_interpreter_is_what_actually_gets_run(monkeypatch) -> None:
    """★ 断言的是**行为**不是常量：runner 真收到那个解释器。

    ★ 顺序要紧：必须**先 reload、后取类**。探测发生在 import 期，
      所以 reload 之前 import 进来的那个类是带着旧默认值的 ——
      这条测试第一次跑就栽在这儿（拿到的还是旧的 `sys.executable`），
      正好说明它测的确实是行为，不是常量。
    """
    module = _reload_with(monkeypatch, BB6_BRIDGE_PYTHON="/custom/python")
    ContactorEscalation = module.ContactorEscalation
    seen: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = '{"answered": true, "advice": "ok"}'
        stderr = ""

    esc = ContactorEscalation(runner=lambda argv, **kw: (seen.append(argv), _Done())[1])
    outcome = esc.escalate(EscalationRequest(task_id="t", block="B", reason="r", attempts=1))
    assert seen and seen[0][0] == "/custom/python"
    assert seen[0][1].endswith("bridge_call.py")
    assert outcome.answered is True
