#!/usr/bin/env python
"""端到端验收：跑 bb6 两遍，比产物 —— 「同一个来源第二次跑不许再抓」。

在 **WSL 里**跑（假服务器和 bb6 在同一侧，127.0.0.1 才是同一个回环）：

    .venv/bin/python acceptance/ab/research-freeze/run_acceptance.py

与 `tests/test_snapshot_freeze.py` 的分工：
  · 那个在【取数层】证机制（3 秒、零模型、确定性）
  · 这个在【整条流水线】证后果 —— 真的跑模型两遍，比产物逐字节
  它闭合 b50 报告里的 `[未验①]`（原来"产物会不同"是从源码推的，不是跑出来的）。

★ 假服务器每次被访问就换一个数值 —— 这是"网页/API 会变"的最小模型。
  于是：两次跑若各自去抓 ⇒ 拿到两份不同材料 ⇒ 产物不同（改前）
        接上台账 ⇒ 第二次根本不抓 ⇒ 拿到第一次那份（改后）
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path("/mnt/c/Users/45140/OneDrive/Desktop/项目/bobanana6")
TASK = REPO / "acceptance" / "ab" / "research-freeze"
PORT = 8799
RUN = TASK / "_run"
PRODUCT = "output/findings.csv"


class Feed(BaseHTTPRequestHandler):
    """情报源：**每次访问换一个数值**。"""

    hits = 0

    def do_GET(self) -> None:  # noqa: N802
        type(self).hits += 1
        n = type(self).hits
        body = f"as_of,value\n2026-10-{10 + n:02d},{n}\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


def digest(path: Path) -> str:
    if not path.is_file():
        return "<没产出>"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    sys.path[:0] = [str(REPO), str(REPO / "src")]
    from acceptance.harness import run_task  # noqa: E402
    from bobanana6.adapters import LLMSettings, OpenAICompatibleClient, TokenRateLimiter  # noqa: E402

    # ★ 本机开着 VPN ⇒ 别让 127.0.0.1 被代理走（否则假服务器收不到请求，读数全假）
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Feed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"假服务器 127.0.0.1:{PORT} 已起（每次访问换数值）")

    settings = LLMSettings.from_env(os.environ)
    print(f"模型 = {settings.model}")

    # 第一次跑：**清掉**快照目录（干净起点 —— 没有台账、没有冻结件）
    if (RUN / "snapshots").exists():
        import shutil

        shutil.rmtree(RUN / "snapshots", ignore_errors=True)
    print(f"[起点] snapshots 目录已清空；服务器 hits = {Feed.hits}")

    readings: list[dict] = []
    for cycle in (1, 2):
        hits_before = Feed.hits
        started = time.time()
        client = OpenAICompatibleClient(
            settings, limiter=TokenRateLimiter(tpm_limit=settings.tpm_limit, rpm_limit=settings.rpm_limit)
        )
        run = run_task(TASK, client=client, env={}, workspace=True, strategy="single", max_calls=40)
        seconds = time.time() - started

        # ★ `run_task` 每次都会重建 workspace ⇒ 产物必须**当场抄出来**再跑第二轮
        live = RUN / "workspace" / PRODUCT
        snapshot_copy = RUN / f"cycle{cycle}-{Path(PRODUCT).name}"
        snapshot_copy.write_bytes(live.read_bytes()) if live.is_file() else None

        verdicts = {
            str(block): bool(v.ok) for block, v in (run.verdicts or {}).items()
        }
        readings.append(
            {
                "cycle": cycle,
                "hits_delta": Feed.hits - hits_before,
                "digest": digest(snapshot_copy),
                "bytes": snapshot_copy.stat().st_size if snapshot_copy.is_file() else 0,
                "verdicts": verdicts,
                "seconds": round(seconds, 1),
            }
        )
        print(
            f"[第 {cycle} 轮] 抓了 {readings[-1]['hits_delta']} 次 · "
            f"产物 {readings[-1]['digest']}（{readings[-1]['bytes']}B）· "
            f"{readings[-1]['seconds']}s · 块级 {verdicts}"
        )

    a, b = readings
    print()
    print("=" * 62)
    print(f"① 网络被抓次数     第1轮 {a['hits_delta']} · 第2轮 {b['hits_delta']}   "
          f"（要的是 1 / 0）")
    print(f"② 产物 sha256      第1轮 {a['digest']} · 第2轮 {b['digest']}")
    same = a["digest"] == b["digest"] and a["digest"] != "<没产出>"
    print(f"③ 两次产物一致     {'✓ 是' if same else '✗ 否'}")
    frozen = sorted(p.name for p in (RUN / "snapshots").glob("*") if p.is_file())
    print(f"④ 冻结仓内容       {frozen}")
    print("=" * 62)
    ok = same and b["hits_delta"] == 0
    print("验收结论:", "✓ 通过 —— 同一个来源第二次没再抓，产物可复现" if ok else "✗ 不通过")
    (RUN / "acceptance-readings.json").write_text(
        json.dumps(readings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    server.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
