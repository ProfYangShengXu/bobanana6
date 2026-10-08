#!/usr/bin/env python
"""传导率 A/B：同一份信息，【内联进 brief】vs【放在工具背后】的传递读数差。

**这是一个度量工具，不是产品行为改动**（规格 §五③）：它只跑测、只读数，不接产品路径。

```
A 臂 = 当前形态：壳把这份信息直接内联进块的第一条消息（brief）
B 臂 = 同一份信息改由工具交付：块要**自己发起调用**才拿到
差值 = 传导率损失的一部分（另一个部分是"模型到底用没用"，那要真模型，见 model_reading）
```

★ 只用**确定性**部分就能量出一部分：

```
· 这份信息在不在块看到的第一条消息里（in_first_message_bytes）
· 拿到它需要几次调用（fetch_calls）
· 拿到它的载荷有多大（tool_payload_bytes）
```

那些"模型有没有真的用上"的读数一律标 `待跑`（§五④：不许编真模型读数）。

用法：

    .venv/bin/python tools/conduction_ab.py            # 传导率基线表（JSON）
    .venv/bin/python tools/conduction_ab.py --markdown  # 同一张表，markdown
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

ACCEPTANCE = REPO / "acceptance"

#: 每个"点" = 一份信息 + 它在 A 臂的落点 + 它在 B 臂该用哪个工具取。
@dataclass(frozen=True)
class Point:
    name: str
    task: str
    block: str
    #: A 臂：brief 里的哪个键（空 = **没有内联**，A 臂读数为 0）。
    brief_key: str
    #: B 臂：用哪个工具取（空 = 没有专属工具）。
    tool: str
    tool_arguments: dict[str, Any]
    note: str


POINTS: tuple[Point, ...] = (
    Point(
        name="statement",
        task="ab/13f-crowding",
        block="T-01",
        brief_key="statement",
        tool="sources.snapshot",
        tool_arguments={"ref": "instruction"},
        note="题面全文：`_statement_entry` 内联进每个块的 brief（produce.py 的 statement 键）",
    ),
    Point(
        name="upstream",
        task="main/panel-three-sources",
        block="T-04",
        brief_key="upstream",
        tool="task.upstream_all",
        tool_arguments={},
        note="上游交接件：合并块的 brief 里内联了每份输入的载荷 + 逐行溯源",
    ),
    Point(
        name="contract_deliverables",
        task="main/panel-three-sources",
        block="T-04",
        brief_key="",
        tool="task.contract",
        tool_arguments={},
        note="★ 未内联（审计①第 1 条）：交付物声明只能靠块自己调 task.contract 拿到",
    ),
    Point(
        name="ontology",
        task="main/panel-three-sources",
        block="T-04",
        brief_key="",
        tool="",
        tool_arguments={},
        note="★ 未内联（审计①第 2 条）且**没有专属工具** —— 块只能从 task.contract 间接看到交付物",
    ),
)


def _bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _briefing(messages: list[dict[str, Any]]) -> dict[str, Any]:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith("{"):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue
    return {}


def _block_of(messages: list[dict[str, Any]]) -> str:
    return str(_briefing(messages).get("subtask", {}).get("id", ""))


def measure(point: Point) -> dict[str, Any]:
    """跑一次这道题，量 A 臂（brief）与 B 臂（工具载荷）。零模型。"""
    from acceptance.harness import run_task  # 延迟 import：让 --help 不必加载产品代码
    from bobanana6.core.model import ModelResponse

    captured: dict[str, Any] = {"brief_key_bytes": 0, "tool_payload_bytes": 0, "fetch_calls": 0, "brief_measured": False}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            if _block_of(messages) != point.block:
                return ModelResponse(text="not json at all")
            brief = _briefing(messages)
            if point.brief_key and not captured["brief_measured"]:
                captured["brief_key_bytes"] = _bytes(brief.get(point.brief_key))
                captured["brief_measured"] = True
            tool_results = [
                json.loads(str(message["content"]))
                for message in messages
                if message.get("role") == "tool" and str(message.get("content", "")).startswith("{")
            ]
            if tool_results:
                captured["tool_payload_bytes"] = sum(_bytes(one) for one in tool_results)
                captured["fetch_calls"] = len(tool_results)
                return ModelResponse(text="not json at all")  # 量完就收工
            if point.tool:
                return ModelResponse(
                    tool_calls=(
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": point.tool, "arguments": json.dumps(point.tool_arguments)},
                        },
                    )
                )
            return ModelResponse(text="not json at all")

    run = run_task(ACCEPTANCE / point.task, client=_Client(), env={}, offline=False)
    inlined = captured["brief_key_bytes"]
    behind_tool = captured["tool_payload_bytes"]
    return {
        "point": point.name,
        "task": point.task,
        "block": point.block,
        "note": point.note,
        "inline": {
            "in_first_message_bytes": inlined,
            "fetch_calls": 0,
            "reachable": bool(point.brief_key) and inlined > 0,
        },
        "behind_tool": {
            "in_first_message_bytes": 0,
            "tool": point.tool,
            "tool_payload_bytes": behind_tool,
            "fetch_calls": captured["fetch_calls"],
            "reachable": bool(point.tool) and behind_tool > 0,
        },
        "delta": {
            "first_message_bytes": inlined - 0,
            # ★ 传导率损失：内联的那一份在工具臂里【一次消息也没有】，
            #   要换 fetch_calls 次调用才拿到（fetched 越大越贵）。
            "extra_calls_to_obtain": captured["fetch_calls"],
            "payload_overhead_bytes": behind_tool - inlined,
        },
        "blocks_ran": tuple(run.blocks),
        # ★ 这部分只能靠真模型：没跑就写未跑，一个数字都不编（§五④）
        "model_reading": "未跑（需要真模型：模型到底有没有调用、调用后有没有用上）",
    }


def render(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| 点 | A 臂：内联进 brief (字节) | B 臂：工具载荷 (字节) | B 臂拿到要几次调用 | A 臂可达 | B 臂可达 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['point']} | {row['inline']['in_first_message_bytes']} | "
            f"{row['behind_tool']['tool_payload_bytes']} | {row['behind_tool']['fetch_calls']} | "
            f"{row['inline']['reachable']} | {row['behind_tool']['reachable']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markdown", action="store_true", help="同一张表，markdown")
    arguments = parser.parse_args(argv)

    rows = [measure(point) for point in POINTS]
    if arguments.markdown:
        print(render(rows))
        return 0
    print(
        json.dumps(
            {
                "schema": "bobanana6.conduction-ab/1",
                "model_reading": "未跑（本轮只用确定性部分；见每一行的 model_reading）",
                "points": rows,
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
