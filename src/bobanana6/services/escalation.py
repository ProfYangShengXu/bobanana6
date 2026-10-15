"""求助出口的实作 —— 把一块的"做不到 + 原因 + 证据"送到一个外部的 agent。

★ 边界（与 `core/ports.py::EscalationPort` 那一侧约定好的）：这个文件只负责**送达**。
  `core/` 一个字都不认识"桥 / agent / 回环"这些词，也不该认识。

★ 阻塞口径（用户定）：**不设答复超时** —— 对面是只会被动应答的一方（人 / 另一个 agent），
  一超时就等于把答复扔了。只留一条边界：**连不上 ⇒ 立刻返回"没拿到"**，
  因为那不是"等一等就好"，那是"这条路今天不通"。
  整跑的墙钟仍是总闸（`Budget.max_wall_s`），那是另一本账。

★ ★ 送达方式**与机器拓扑无关**，两件事分开看：

    ① **桥在哪** —— 由 `bridge_call.py` 里的 `BB6_BRIDGE_ENDPOINT` 决定。
       默认 `http://127.0.0.1:8791/`（本地桥）。远端桥就改这个地址。

    ② **怎么把请求递过去** —— 就是这里 `helper_python` 的事。
       同侧部署（桥跟 bb6 跑在同一栈）⇒ 用本进程的解释器直接跑 `bridge_call.py` 即可。
       双栈机器（bb6 在 WSL、桥在 Windows）⇒ 用 **Windows 侧的解释器**跑，
       流量会落回宿主自己的回环 —— 这是**实测有效的一种送达方式**，不是必须的形态
       （两个 `127.0.0.1` 不是同一个，直连不通；见 `agent-interop-protocols` skill 第五节）。

    ⇒ 所以「WSL 跑 Windows 的 python.exe」是**默认拓扑下的落地点**，不是接口定义。
      换环境时通常只需要动环境变量，不用改代码：`BB6_BRIDGE_ENDPOINT` 换地址、
      `BB6_BRIDGE_PYTHON` 换解释器。报文形状全部在 `bridge_call.py` 里。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..core.model import EscalationOutcome, EscalationRequest

__all__ = ["ContactorEscalation", "compose_request"]

def _default_helper_python() -> str:
    """跑 `bridge_call.py` 用的解释器 —— **送达方式**，不是接口本身。

    ★ 为什么可配（而不是把某个绝对路径写死）：`bridge_call.py` 只做一件事 ——
      往 `BB6_BRIDGE_ENDPOINT` 发 HTTP。**桥在哪一边由端点决定，跟解释器无关。**
      「WSL 执行 Windows 的 python.exe」只是**其中一种送达方式**：在双栈机器上
      它让流量落回宿主自己的回环，省掉改绑定/开防火墙/端口转发。
      但换到纯 Linux、或桥本来就跟进程同侧时，同一个解释器直接跑就行。

    ★ 探测顺序（显式配置一律优先，绝不被猜出来）：
        ① `BB6_BRIDGE_PYTHON`           —— 通用名，跨平台都用它
        ② `BB6_WINDOWS_PYTHON`         —— 历史名，保留（已有部署写了这个，不能废）
        ③ `sys.executable`             —— 跑着自己的解释器
        ④ `python3`
    ★ 全都不成立时也**不抛异常**：起不来会在 `escalate()` 里落成
      "could not reach the bridge" 的具名结果（`EscalationOutcome(answered=False)`），
      与"桥连不上"走同一条降级路 —— 出口只有一条，不在入口分叉。
    """
    for name in ("BB6_BRIDGE_PYTHON", "BB6_WINDOWS_PYTHON"):
        # blame: whitelist — 环境定位符；配了就用，没配才往下探测
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    # blame: whitelist — 本进程的解释器；同侧部署（桥与本进程同栈）时它一定可用
    return sys.executable or "python3"


#: 跑 `bridge_call.py` 的解释器。★ env 可覆盖，别改代码。
# blame: whitelist — 环境定位符；探测不到时退回本进程解释器；真起不来 ⇒ escalate 报"没拿到"
DEFAULT_HELPER_PYTHON = _default_helper_python()

#: 桥里接这件活的 agent（在 `%LOCALAPPDATA%\contactor\config.yaml` 里注册）。
# blame: whitelist — 环境定位符；桥里没有这个 agent 时送达会失败 ⇒ 同样落回降级
DEFAULT_AGENT = os.environ.get("BB6_ESCALATION_AGENT", "hermes")


def _repo_hint() -> str:
    """求助文本里要说的「仓库在哪」—— **从本文件的位置推**，不写死某台机器的路径。

    ★ 为什么要推而不是配：这句话是给对面（另一个 agent / 人）看的导航信息，
      指错地方比不给更坏。而 `escalation.py` 自己的位置就是最可靠的锚：
      `<repo>/src/bobanana6/services/escalation.py` ⇒ 上溯三层就是仓库根。
    ★ 推不出来（比如被打包成 zip 运行）就退回一个不指路的说法 ——
      **宁可不指路，也不指到一个别人机器上不存在的路径**。
    """
    try:
        root = Path(__file__).resolve().parents[3]
        if root.is_dir():
            return str(root)
    except (OSError, IndexError):  # pragma: no cover — 位置异常时才走到
        pass
    return ""


def compose_request(request: EscalationRequest) -> str:
    """把求助写成一段**自包含**的话。

    ★ 三条纪律（来自 `contactor` skill，它们不是措辞偏好）：
      · **自包含**：对面没有你的上下文 —— 别写"上次那个"、别用指代
      · **给事实不给步骤**：说清楚"我在找什么"，不规定它怎么找
      · **说清用途**：让它知道答复会被交给谁、用来干什么（决定它怎么取舍）
    ★ 不许把"你该怎么做"写进去 —— 写死步骤等于花着跨进程的成本做同进程的事。
    """
    where = _repo_hint()
    where_line = f"仓库在 {where}。\n" if where else ""
    return (
        "【求助】bobanana6 的一个块反复做不成，需要你接手判断。\n"
        "\n"
        f"- 任务: {request.task_id}\n"
        f"- 块: {request.block}\n"
        f"- 失败身份: {request.reason}\n"
        f"- 已尝试次数: {request.attempts}（重试与反思都用完了）\n"
        f"- 分类器给的细节: {request.detail or '（无）'}\n"
        "\n"
        "我在找什么：不是让你替它把活算完，而是判断**这块为什么做不成** ——\n"
        "是**规格没给到它**（它看不到某条要求）、是**壳的缺陷**（机制在但没起作用），\n"
        "还是确实超出了它的能力；并给出**可执行的下一步**。\n"
        f"{where_line}"
        "\n"
        "你给的答复会作为**一条消息直接交回给这个块**，它会据此再试一次。\n"
        "所以请写成给那个块看的话，而不是给我看的汇报。\n"
    )


def _parse(stdout: str) -> EscalationOutcome:
    """读 `bridge_call.py` 吐的那一行 JSON。★ 读不回来 ⇒ 当作"没拿到"。"""
    line = ""
    for candidate in reversed((stdout or "").strip().splitlines()):
        if candidate.strip().startswith("{"):
            line = candidate.strip()
            break
    if not line:
        return EscalationOutcome(answered=False, detail="the bridge caller printed nothing usable")
    try:
        data = json.loads(line)
    except ValueError:
        return EscalationOutcome(answered=False, detail=f"unreadable answer: {line[:200]}")
    return EscalationOutcome(
        # blame: fail-closed — 缺字段即 False ⇒ 当作"没拿到答复"，绝不凭空当成功
        answered=bool(data.get("answered")),
        # blame: fail-closed — 缺 advice 即空串 ⇒ loop 那侧也会拒收，两道闸同向
        advice=str(data.get("advice") or ""),
        # blame: whitelist — 缺说明只影响留痕，不影响"拿没拿到"的判定
        detail=str(data.get("detail") or ""),
    )


@dataclass
class ContactorEscalation:
    """把 `EscalationPort` 落到本机的 agent 桥上。"""

    #: 桥里接这件活的 agent 名（写在这里 = 只有这一处知道"对面是谁"）。
    # blame: whitelist — 实例字段；构造时总能从模块默认拿到值（上面那个环境定位符）
    agent: str = DEFAULT_AGENT
    #: 跑 `bridge_call.py` 的解释器 —— **送达方式**，与"桥在哪"无关。
    #: ★ 默认由 `_default_helper_python()` 按 环境变量 → 本进程解释器 的顺序探测，
    #:   所以双栈机器（WSL + Windows）和纯 Linux 机器都不用改代码。
    # blame: whitelist — 同上，构造时有值；起不来时 escalate 返回"没拿到"
    helper_python: str = DEFAULT_HELPER_PYTHON
    #: 送达代码本体（`bridge_call.py`）。
    # blame: whitelist — 与实现文件同目录，随包走；找不到时起不来 ⇒ 返回"没拿到"
    helper: Path = field(default_factory=lambda: Path(__file__).with_name("bridge_call.py"))
    #: 子进程总上限（秒）。`0` = 不限 —— ★ 与"答复超时"是两回事：
    #: 这里限的是**本机这段子进程**，不是"等了多久就放弃答复"。
    # blame: fail-open — 理由：用户口径"不设答复超时"；反证读数：本机子进程理论上可挂死，但它的输出被父进程的整跑墙钟兜底（Budget.max_wall_s），不是无界
    timeout_s: float = 0.0
    #: 可注入（测试用假 runner，不真起进程）。
    # blame: whitelist — 只为可测注入（假 runner），生产走默认的 subprocess.run
    runner: Callable[..., subprocess.CompletedProcess[str]] = field(
        default=subprocess.run,
    )

    def escalate(self, request: EscalationRequest) -> EscalationOutcome:
        text = compose_request(request)
        with tempfile.TemporaryDirectory(prefix="bb6-ask-") as tmp:
            ask = Path(tmp) / "ask.txt"
            ask.write_text(text, encoding="utf-8")
            try:
                done = self.runner(
                    [self.helper_python, str(self.helper), str(ask), self.agent],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_s or None,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                # ★ 起不来 = 这条路不通 ⇒ 立刻说"没拿到"，不把它当异常往上抛。
                return EscalationOutcome(
                    answered=False, detail=f"could not reach the bridge: {type(exc).__name__}: {exc}"
                )
            if done.returncode != 0:
                return EscalationOutcome(
                    answered=False,
                    detail=f"the bridge caller exited {done.returncode}: {(done.stderr or '')[:200]}",
                )
        return _parse(done.stdout or "")


def parse_lines(lines: Sequence[str]) -> EscalationOutcome:
    """给测试用的小门面：把若干行当作子进程输出解析。"""
    return _parse("\n".join(lines))
