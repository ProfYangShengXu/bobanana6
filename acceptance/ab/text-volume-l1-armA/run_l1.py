"""L1 最小验证的跑测入口（带完整记录）。

与 run_research.py 同一套跑法（同一个 harness.run_task、同一个 client），
只是指到 L1 那道小题。

★ 记什么：判定的三层信号（run / 块 / 计划）+ 每次尝试的 summary + 产物落点。
  一开始只用 TaskRun 的属性名（`exit_reason` 是 RunResult 的，不是它的）——
  那次崩在最后一行，白跑 8 分钟。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6")
TASK = Path(__file__).resolve().parent   # ★ 按脚本自身位置推（复制到别的臂就跟着走）
TREE = TASK / "_run" / "artifacts" / "tree"
PY = sys.executable


def block_attempts() -> list[dict]:
    """痕迹树里这一块的每一次尝试（重试会在同一棵树里留多笔）。"""
    out = subprocess.run(
        ["git", "-C", str(TREE), "log", "--format=%h|%ci|%s", "-n", "12"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        h, when, subject = parts
        cj = subprocess.run(
            ["git", "-C", str(TREE), "show", f"{h}:T-01/commit.json"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        summary = ""
        m = __import__("re").search(r'"summary":\s*"([^"]*)"', cj)
        if m:
            summary = m.group(1)
        rows.append({"commit": h, "at": when, "subject": subject, "summary": summary})
    return rows


def main() -> int:
    sys.path[:0] = [str(REPO), str(REPO / "src")]

    from acceptance.harness import run_task  # noqa: E402
    from bobanana6.adapters import (  # noqa: E402
        LLMSettings,
        OpenAICompatibleClient,
        TokenRateLimiter,
    )

    settings = LLMSettings.from_env(os.environ)
    print(f"模型 = {settings.model}", flush=True)

    started = time.time()
    client = OpenAICompatibleClient(
        settings,
        limiter=TokenRateLimiter(tpm_limit=settings.tpm_limit, rpm_limit=settings.rpm_limit),
    )
    run = run_task(
        TASK,
        client=client,
        env={},
        workspace=True,
        strategy="source",
        max_calls=int(os.environ.get("BB6_MAX_CALLS", "150")),
    )
    seconds = time.time() - started

    print(f"耗时 = {seconds:.1f}s", flush=True)
    # ★ 只打印 TaskRun 真的有的字段（上一版在这里崩过）
    print(f"refused = {run.refused} {run.refused_codes}", flush=True)
    print(f"plan_shape = {run.plan_shape}  blocks = {list(run.blocks)}", flush=True)
    rep = run.report
    print(f"coverage = {getattr(rep, 'coverage', None)}", flush=True)
    print(f"assembly_passed = {getattr(rep, 'assembly_passed', None)}", flush=True)
    print(f"block_pass_rate = {getattr(rep, 'block_pass_rate', None)}", flush=True)
    for block, verdict in sorted(run.verdicts.items()):
        print(f"  {block}: ok={getattr(verdict, 'ok', None)} failed={getattr(verdict, 'failed', None)}", flush=True)

    print("attempts:", flush=True)
    for row in block_attempts():
        print(f"  {row['commit']} {row['at']} [{row['subject']}] {row['summary']}", flush=True)

    brief = TASK / "_run" / "workspace" / "output" / "brief.md"
    print(f"deliverable = {brief} exists={brief.is_file()} bytes={brief.stat().st_size if brief.is_file() else 0}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
