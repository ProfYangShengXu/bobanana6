"""求助出口的实作 —— 把一块的"做不到 + 原因 + 证据"送到本机的 agent 桥。

★ 边界（与 `core/ports.py::EscalationPort` 那一侧约定好的）：这个文件只负责**送达**。
  `core/` 一个字都不认识"桥 / agent / 回环"这些词，也不该认识。

★ 阻塞口径（用户定）：**不设答复超时** —— 对面是只会被动应答的一方（人 / 另一个 agent），
  一超时就等于把答复扔了。只留一条边界：**连不上 ⇒ 立刻返回"没拿到"**，
  因为那不是"等一等就好"，那是"这条路今天不通"。
  整跑的墙钟仍是总闸（`Budget.max_wall_s`），那是另一本账。

★ ★ 为什么走 `python.exe` 而不是直接发请求：bb6 跑在 WSL，桥绑的是 Windows 的
  `127.0.0.1`（故意只回环、无鉴权）—— 两边的回环不是同一个。实测有效的路子是
  **进程互操作**：WSL 直接执行 Windows 的 exe，流量根本没走网络（见
  `agent-interop-protocols` skill 第五节）。送达的报文形状全部在 `bridge_call.py` 里。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..core.model import EscalationOutcome, EscalationRequest

__all__ = ["ContactorEscalation", "compose_request"]

#: Windows 侧的 python（进程互操作的落地器）。★ env 可覆盖，别改代码。
DEFAULT_WINDOWS_PYTHON = os.environ.get(
    "BB6_WINDOWS_PYTHON",
    # blame: whitelist — 环境定位符（默认指向本机装了 python 的那个路径）；找不到就报"没拿到"⇒ 落回降级
    r"C:\Users\45140\AppData\Local\Programs\Python\Python312\python.exe",
)

#: 桥里接这件活的 agent（在 `%LOCALAPPDATA%\contactor\config.yaml` 里注册）。
# blame: whitelist — 环境定位符；桥里没有这个 agent 时送达会失败 ⇒ 同样落回降级
DEFAULT_AGENT = os.environ.get("BB6_ESCALATION_AGENT", "hermes")


def compose_request(request: EscalationRequest) -> str:
    """把求助写成一段**自包含**的话。

    ★ 三条纪律（来自 `contactor` skill，它们不是措辞偏好）：
      · **自包含**：对面没有你的上下文 —— 别写"上次那个"、别用指代
      · **给事实不给步骤**：说清楚"我在找什么"，不规定它怎么找
      · **说清用途**：让它知道答复会被交给谁、用来干什么（决定它怎么取舍）
    ★ 不许把"你该怎么做"写进去 —— 写死步骤等于花着跨进程的成本做同进程的事。
    """
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
        "仓库在桌面「项目/bobanana6」（WSL 里是 /mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6）。\n"
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
    #: Windows 侧的 python：进程互操作的落地器。
    # blame: whitelist — 同上，构造时有值；起不来时 escalate 返回"没拿到"
    windows_python: str = DEFAULT_WINDOWS_PYTHON
    #: Windows 侧那一段送达代码（`bridge_call.py`）。
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
                    [self.windows_python, str(self.helper), str(ask), self.agent],
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
