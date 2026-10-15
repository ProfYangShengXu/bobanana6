#!/usr/bin/env python3
"""bobanana6 的**桥入口** —— 让别的 agent 能通过 contactor 派任务进来。

用法（由 contactor 的 `subprocess_cli` 后端调用）：

    python bb6_agent_entry.py "<prompt>"

`prompt` 里要含一个 **task.yaml 的路径**。本脚本：

    ① 从 prompt 里把那一个路径抠出来
    ② 交给 `acceptance.harness.run_task()` 跑（★ 系统级验收的**官方跑法**）
    ③ 把结果压成**一行 JSON** 打到 stdout

★ 为什么走 harness 而不是自己拼 CLI 参数：
  CLI 的参数是一组**会漏的开关** —— 实测第一版就漏了 `--workspace`，于是代码类交付物
  在真模型下被具名拒绝（`CandidateFormatError: this deployment has no workspace`），
  而报出来的东西看着像"模型不行"。
  `harness.run_task()` 把「怎么把一道题跑成一份 AcceptanceReport」这件事收在一处：
  workspace 播种、快照、并发、预算都按同一套口径来。入口只该负责**送达**，
  不该负责**重新实现一遍跑法**。

★ 为什么 stdout 只有一行 JSON：调用方（`services/bridge_call.py` 那一侧）按行读最后
  一个 `{...}`。别往 stdout 打别的东西 —— 日志一律走 stderr。

★ 为什么只认「路径」不认「自然语言」：契约本身就是这道题的规格。
  让 agent 现场把自然语言猜成契约，等于把"要什么"重新变成一次赌博。
  写契约的是调用方；bb6 只负责按契约把活干完、并如实报告干成了没有。

输出形状：

    {"ok": bool, "task_file": str, "exit_code": int, "duration_s": float,
     "reports": [str, ...], "summary": {...} | null, "detail": str}
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

#: 仓库根 —— 从本文件的位置推（`<repo>/tools/bb6_agent_entry.py`）。
REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "src")]

#: 跑一道题的墙钟上限（秒）。
# blame: whitelist — 本机这段子进程的上限，env 可覆盖；到点报"还没好"而不是"失败"
TIMEOUT_S = int(os.environ.get("BB6_ENTRY_TIMEOUT_S", "3600"))

#: prompt 里出现的 task.yaml 路径。
PATH_RE = re.compile(r"""["'`]?((?:/|[A-Za-z]:[\\/]|\.\.?/)[^\s"'`]+\.ya?ml)["'`]?""")


def say(payload: dict) -> None:
    """stdout 只出这一行。"""
    print(json.dumps(payload, ensure_ascii=False))


def find_task_file(prompt: str) -> str:
    """从 prompt 里抠出 task.yaml 的路径。找不到就给空串。"""
    candidates = [m.group(1) for m in PATH_RE.finditer(prompt or "")]
    if not candidates:
        return ""
    for c in candidates:
        if c.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", c):
            return c
    return candidates[-1]


def _fail(task_file: str, detail: str, **extra) -> int:
    payload = {
        "ok": False, "task_file": task_file, "exit_code": -1, "duration_s": 0.0,
        "reports": [], "summary": None, "detail": detail,
    }
    payload.update(extra)
    say(payload)
    return 0


def _brief(summary) -> dict | None:
    """只留关键字段 —— 完整版留在 `_run/reports/summary.json`。

    走桥回来的东西是给另一个 agent 或人读的；几十 KB 的 JSON 会把结论淹掉。
    """
    if not isinstance(summary, dict):
        return None
    task_block = summary.get("task") or {}
    assembly = summary.get("assembly") or {}
    blocks = summary.get("blocks_report") or []
    return {
        "task_id": task_block.get("task_id"),
        "goal": task_block.get("goal"),
        "exit_reason": summary.get("exit_reason"),
        "blocks": [
            {"subtask": b.get("subtask"), "ok": b.get("ok"), "failed": b.get("failed") or []}
            for b in blocks if isinstance(b, dict)
        ],
        "assembly": {
            "passed": assembly.get("passed"),
            "failed_blocks": assembly.get("failed_blocks") or [],
            "coverage": assembly.get("coverage"),
        },
    }


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    task_file = find_task_file(prompt)
    if not task_file:
        return _fail("", "no task.yaml path found in the prompt; "
                         "pass an absolute path to the contract (e.g. /path/to/task.yaml)")
    task_path = Path(task_file)
    if not task_path.is_file():
        return _fail(task_file, f"task file does not exist: {task_file}")

    from acceptance.harness import run_task
    from bobanana6.adapters import LLMSettings, OpenAICompatibleClient, TokenRateLimiter

    offline = os.environ.get("BB6_ENTRY_OFFLINE", "").strip() not in ("", "0", "false")
    client = None
    if not offline:
        try:
            settings = LLMSettings.from_env(os.environ)
        except Exception as exc:  # noqa: BLE001 — 配置缺失要**具名**说清，不抛给桥
            return _fail(str(task_path), f"no model endpoint configured: {exc}")
        client = OpenAICompatibleClient(
            settings,
            limiter=TokenRateLimiter(tpm_limit=settings.tpm_limit, rpm_limit=settings.rpm_limit),
        )

    started = time.time()
    try:
        run = run_task(
            task_path.resolve().parent,
            client=client,
            env=dict(os.environ),
            offline=offline,
            task_file=task_path.name,
            workspace=True,          # ★ 代码类产物的落盘目录；不给会被具名拒绝
        )
    except BaseException as exc:  # harness 自己会在"拒跑"时抛；这层只做**展现**
        return _fail(str(task_path), f"run_task raised {type(exc).__name__}: {str(exc)[:400]}")
    duration = time.time() - started

    out_dir = task_path.resolve().parent / "_run" / "reports"
    reports = sorted(p.name for p in out_dir.glob("*")) if out_dir.is_dir() else []
    summary = None
    summary_path = out_dir / "summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except ValueError:
            summary = None

    brief = _brief(summary)
    # ★ 成不成的判据是 verdicts（内核判完给的结果），不是"有没有产出文件"。
    verdicts = getattr(run, "verdicts", None) or {}
    ok = bool(verdicts) and all(bool(v.ok) for v in verdicts.values())
    failed = sorted({str(c) for v in verdicts.values() for c in (v.failed or ())})

    # ★ ★ 「拒跑」和「跑挂了」要分开说 —— 它们对外面的下一步完全不同：
    #   拒跑（notes 里有话说）= 这道题根本没开工，改的是题/环境；
    #   跑挂（有 verdicts 但不过）= 开工了没做成，改的是实现。
    notes = tuple(getattr(getattr(run, "report", None), "notes", ()) or ())
    if not verdicts and notes:
        detail = "refused before any worker started: " + "; ".join(str(n) for n in notes)[:300]
    elif ok:
        detail = "passed"
    else:
        detail = f"failed: {', '.join(failed) or 'no verdicts'}"
    if not reports:
        detail += "; no reports were written"

    say({
        "ok": ok, "task_file": str(task_path), "exit_code": 0 if ok else 1,
        "duration_s": round(duration, 1),
        "reports": reports, "summary": brief, "detail": detail,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
