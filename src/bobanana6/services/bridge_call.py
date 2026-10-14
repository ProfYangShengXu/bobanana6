#!/usr/bin/env python3
"""桥的**一次性调用器** —— 在 **Windows 侧**跑（`python.exe`），只用标准库。

★ 为什么要有这个文件：bb6 跑在 WSL 里，而桥绑的是 Windows 的 `127.0.0.1`
  （故意只回环、没有鉴权）。WSL 的 127.0.0.1 不是 Windows 的 127.0.0.1 ⇒ 直连不通。
  ⭐ 实测有效的路子是**进程互操作**：WSL 直接执行 Windows 的 exe，
  流量根本没走网络，落回宿主自己的回环 —— 不用改绑定地址、不用开防火墙、不用端口转发。

★ 送达形状是**实测**出来的，不是从文档抄的（见 `agent-interop-protocols` skill）：
  · 方法名 `message/send`；参数是**扁平**的 `{agent, text, messageId}`
    （不是 A2A 规范那套嵌套 `{"message": {"parts": [...]}}`）
  · 响应形状是 `result.task.taskId`，**不是** `result.taskId`
  · taskId 是 **16 位十六进制**；小整数是 JSON-RPC 的 `id` ⇒ 请求其实报错了
  · 答复在 `tasks/get` 的 `artifacts[].parts[].text`；正文也可能在 `history` 里

★ 阻塞口径（用户定）：**不设答复超时**。对面是只会被动应答的一方，
  一超时就等于把答复扔了。只划两条边界：
    ① 连不上 / 桥直接报错 ⇒ **立刻**返回（不傻等）
    ② 桥收到了、任务在跑 ⇒ 一直轮询到终态

用法：python bridge_call.py <请求文本文件> [agent]
输出：一行 JSON —— `{"answered": bool, "advice": str, "detail": str}`
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# blame: whitelist — 环境定位符（默认指向本机安装的桥）；送不达时返回"没拿到"⇒ 落回降级
ENDPOINT = os.environ.get("BB6_BRIDGE_ENDPOINT", "http://127.0.0.1:8791/")
#: 轮询间隔。★ 与"答复超时"是两件事：这个只是**多久问一次**，不是多久放弃。
# blame: whitelist — 只是轮询节奏，调大调小都不改变"答不上来"的结果
POLL_S = float(os.environ.get("BB6_BRIDGE_POLL_S", "10"))
#: 单次网络往返的上限（连不上要立刻失败，而不是挂死在这里）。
# blame: fail-closed — 单次往返超时 ⇒ 立刻报"没拿到"，不傻等
CALL_TIMEOUT_S = float(os.environ.get("BB6_BRIDGE_CALL_TIMEOUT_S", "30"))
#: 总轮询上限（秒）。0 = 不限。★ 这是防"对面永远不动"的兜底，不是"等答复的超时"。
# blame: fail-open — 理由：用户口径"不设答复超时"（对面只会被动应答，超时就等于把答复扔了）；反证读数：桥那头若卡死，不加此闸这一跑会一直挂住 ⇒ 所以它**可**被显式收紧，默认留 0
MAX_WAIT_S = float(os.environ.get("BB6_BRIDGE_MAX_WAIT_S", "0"))

TERMINAL = ("completed", "failed", "canceled")


def rpc(method: str, params: dict, timeout: float) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def say(answered: bool, advice: str = "", detail: str = "", task_id: str = "", pending: str = "") -> None:  # blame: fail-closed — 空串 = 这一项没内容可报；永不缺省为"成功"，answered 必须显式给
    """把结论写成**一行** JSON —— 父进程（`services/escalation.py`）按行读回来。

    ★ 一行是有意的：日志/流式输出会混进来，逐行找最后一个 `{...}` 比整段解析稳。
    ★★ `task_id` 与 `pending` **一定要带**：任务是异步的，只有拿到 id 才能
      `contactor answer <id> "<回答>"` 把它续下去（见本文件顶部的"两条出路"）。
    """
    payload = {"answered": answered, "advice": advice, "detail": detail}
    if task_id:
        payload["task_id"] = task_id
    if pending:
        payload["pending"] = pending
    print(json.dumps(payload, ensure_ascii=False))


def _texts(node) -> list[str]:
    """从 task 里把**交付物**的文本捞出来。

    ★ 只认 `artifacts`，**不拼 `history`**：实测同一条答复会以两种形态同时到 ——
      artifacts 里是拼好的整句，history 里是流式分片的碎片
      （`收到\n，\n失败\n身份\n是\n overload\ned\n。`）。
      两个都拼就会把同一句话灌进去两遍、还带一堆碎片。
    ★ `artifacts` 缺席（对端不讲 Artifact）时才退回 history —— 那是**兜底**，不是并列。
    ★ `append` 语义：同一个 `artifactId` 可以多次到达，续在已有内容后面（不是覆盖）。
    """
    if not isinstance(node, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for artifact in node.get("artifacts") or ():
        if not isinstance(artifact, dict):
            continue
        for part in artifact.get("parts") or ():
            if isinstance(part, dict) and part.get("text"):
                text = str(part["text"])
                if text not in seen:
                    seen.add(text)
                    out.append(text)
    if out:
        return out
    for message in node.get("history") or ():
        if isinstance(message, dict) and str(message.get("role")) == "agent":
            for part in message.get("parts") or ():
                if isinstance(part, dict) and part.get("text") and not part.get("append"):
                    out.append(str(part["text"]))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"answered": False, "advice": "", "detail": "no request file given"}))
        return 2
    with open(sys.argv[1], encoding="utf-8") as handle:
        text = handle.read()
    agent = sys.argv[2] if len(sys.argv) > 2 else "hermes"

    # ── ① 送达（连不上就立刻回，不傻等）────────────────────────────────
    try:
        sent = rpc(
            "message/send",
            {"agent": agent, "text": text, "messageId": f"bb6-{int(time.time() * 1000)}"},
            CALL_TIMEOUT_S,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        say(False, detail=f"bridge unreachable: {str(exc)[:200]}")
        return 0

    task = (sent.get("result") or {}).get("task") or {}
    task_id = task.get("taskId")
    # ★ 判据：16 位十六进制。小整数 = 那是 JSON-RPC 的 id ⇒ 请求没发出去。
    if not task_id or len(str(task_id)) != 16 or not all(c in "0123456789abcdef" for c in str(task_id)):
        say(False, detail=f"no task was created: {str(sent)[:300]}")
        return 0

    # ── ② 等终态（★ 不设答复超时，只防"永远不动"）──────────────────────
    started = time.time()
    while True:
        try:
            got = rpc("tasks/get", {"taskId": task_id}, CALL_TIMEOUT_S)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            say(False, detail=f"lost the bridge while waiting: {str(exc)[:200]}")
            return 0
        current = (got.get("result") or {}).get("task") or {}
        state = str(current.get("state") or "")

        if state in TERMINAL:
            texts = _texts(current)
            if state == "completed" and texts:
                say(True, advice="\n".join(texts), detail="state=completed", task_id=str(task_id))
            else:
                error = str(current.get("error") or "")[:200]
                say(False, detail=f"state={state} texts={len(texts)} {error}", task_id=str(task_id))
            return 0

        if state == "input-required":
            # ★★ 对面反过来要人拍板（`pending.kind == "permission"` 就是放行请求）。
            #   **块答不了**（它是模型驱动的），所以据实说"没拿到"，并把问题与 taskId
            #   原样带回 —— 交给上层/人去处理，绝不假装拿到了答复。
            #   ★ 拿到 taskId 才能续：`contactor answer <taskId> "<回答>"`。
            pending = json.dumps(current.get("pending") or {}, ensure_ascii=False)[:600]
            say(False, detail="the outside side is asking for input", task_id=str(task_id), pending=pending)
            return 0

        if MAX_WAIT_S and (time.time() - started) > MAX_WAIT_S:
            # ★★ 到点了，任务**还活着** —— 这不是失败，是"还没好"。
            #   把 taskId 带回去，外面记下来接着等（或交给人去接）。
            say(False, detail=f"still working after {MAX_WAIT_S:.0f}s (state={state})", task_id=str(task_id))
            return 0
        time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
