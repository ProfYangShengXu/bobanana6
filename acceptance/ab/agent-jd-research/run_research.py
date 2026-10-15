#!/usr/bin/env python
"""跑「大厂 AI Agent 岗研究」这道真实开放题。

在 **WSL 里**跑（研究器直连网络，跟 bb6 同侧）：

    .venv/bin/python acceptance/ab/agent-jd-research/run_research.py

★ 这是**第一道真用上研究类取数**的题：契约里声明了 `kind: research` 的来源，
  bb6 自己去网上把材料抓回来冻住，然后按四份产物做归纳。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path("/mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6")
TASK = REPO / "acceptance" / "ab" / "agent-jd-research"
OUTPUT = TASK / "_run" / "workspace" / "output"


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

    print()
    print("=" * 66)
    print(f"耗时 {seconds:.0f}s")
    for block, verdict in sorted((run.verdicts or {}).items()):
        evidence = dict(verdict.evidence or {})
        print(
            f"  {block}  ok={bool(verdict.ok)}  failed={sorted(str(c) for c in verdict.failed)}  "
            f"exit={evidence.get('exit')}  errors={str(evidence.get('errors'))[:160]}"
        )
    print("=" * 66)
    print()
    print("产物：")
    for path in sorted(OUTPUT.glob("*")) if OUTPUT.is_dir() else []:
        print(f"  {path.name:24s} {path.stat().st_size:8d} 字节")

    # ★ 快照表：看得到"走了哪条后端、降级没有"（web-use 纪律②的观测面）
    summary = TASK / "_run" / "reports" / "summary.json"
    if summary.is_file():
        report = json.loads(summary.read_text(encoding="utf-8"))
        snaps = report.get("snapshots") or []
        print()
        print(f"冻结材料 {len(snaps)} 份：")
        for snap in snaps:
            flag = "  ★降级" if snap.get("degraded") else ""
            print(f"  {snap['ref']:22s} backend={snap.get('backend') or '-':9s}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
