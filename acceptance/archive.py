"""跑测侧的上下文归档（`小模型采访设计-找壳的调优点-20261004.md` 第五节）。

**为什么在跑测侧**：归档是【观测】不是改动 —— 本阶段不做调优，壳的代码保持干净；
包一层 client，壳自己完全不知道被记了。

**存什么（采访的 A1 要"逐字抄出来"，所以一条都不能少）**：

```
① 每次调用的完整 messages（system + user + 全部历史），原样，不许摘要/截断
② 模型的原始输出（未解析、未清洗）
③ 调用位置：块 / 轮 / 阶段
④ 判据的拒绝理由（由调用方在 judge 之后追加，见 record_rejection）
⑤ 请求参数：model / temperature / max_tokens / extra_body
⑥ token 用量   ⑦ 时间戳（同窗对照用）   ⑧ 工具菜单（也是"它看到的东西"）
```

格式：JSONL，一条一行；索引 `[block, turn, seq]`。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Archive", "ArchivingClient", "record_verdicts"]


def _block_and_turn(messages: list[dict[str, Any]]) -> tuple[str, int]:
    """Where this call sits: the block id from the briefing, the turn from the transcript."""
    block = ""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith("{"):
            try:
                block = str(json.loads(content).get("subtask", {}).get("id", "")) or block
            except json.JSONDecodeError:
                continue
            break
    turns = sum(1 for message in messages if message.get("role") == "assistant")
    return block, turns + 1


@dataclass
class Archive:
    """Append-only JSONL of every model call, plus the judged readings that follow it."""

    path: Path
    seq: int = 0
    calls: int = 0
    bytes_written: int = 0

    def record(
        self,
        *,
        messages: list[dict[str, Any]],
        output: str,
        tool_calls: tuple[Mapping[str, Any], ...] = (),
        tools: tuple[Mapping[str, Any], ...] = (),
        params: Mapping[str, Any],
        usage: Mapping[str, Any] | None = None,
        phase: str = "produce",
    ) -> dict[str, Any]:
        block, turn = _block_and_turn(messages)
        self.seq += 1
        self.calls += 1
        entry = {
            "seq": self.seq,
            "at": time.time(),
            "block": block,
            "turn": turn,
            "phase": phase,
            "params": dict(params),
            # ① 原样：不摘要、不截断
            "messages": [dict(message) for message in messages],
            # ② 原样：未解析未清洗
            "output": output,
            "tool_calls": [dict(call) for call in tool_calls],
            # ⑧ 壳当时给它的工具菜单：A1 问"它看到什么"，菜单也是看到的东西
            "tools": [dict(tool) for tool in tools],
            "usage": dict(usage or {}),
            # ④ 判据的拒绝理由由调用方在 judge 之后补（record_rejection）
            "rejections": [],
        }
        self._append(entry)
        return entry

    def record_rejection(self, *, block: str, constraint: str, reason: str) -> None:
        """④ 判据对它的拒绝理由：哪条约束、原话是什么（补到最后一条同块的记录上）。"""
        self._append({"seq": self.seq, "block": block, "phase": "rejection", "constraint": constraint, "reason": reason})

    def _append(self, entry: Mapping[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        self.bytes_written += len(line.encode("utf-8"))

    def report(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "calls": self.calls,
            "bytes": self.bytes_written,
            "megabytes": round(self.bytes_written / 1_048_576, 2),
        }


def record_verdicts(archive: Archive, verdicts: Mapping[str, Any]) -> int:
    """④ 判定跑完之后，把判据的拒绝理由灌进归档。

    只有判定器知道【是哪条约束】拒了它、【原话】是什么（`Verdict.failed` +
    `Verdict.evidence[cid]`）；归档从对话记录里重建不出来，所以由调用方交过来。
    返回写了多少条。
    """
    written = 0
    for block, verdict in verdicts.items():
        evidence = getattr(verdict, "evidence", None) or {}
        for constraint in getattr(verdict, "failed", ()) or ():
            fact = evidence.get(constraint) if hasattr(evidence, "get") else None
            archive.record_rejection(
                block=str(block), constraint=str(constraint), reason=str(fact if fact is not None else "")
            )
            written += 1
    return written


@dataclass
class ArchivingClient:
    """The real client with a recorder around it -- the shell never learns it exists."""

    client: Any
    archive: Archive
    params: Mapping[str, Any] = field(default_factory=dict)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        **kw: Any,
    ) -> Any:
        response = self.client.complete(messages, tools, max_tokens, **kw)
        merged = {**dict(self.params), "max_tokens": max_tokens, **{key: str(value) for key, value in kw.items()}}
        self.archive.record(
            messages=messages,
            output=str(getattr(response, "text", "")),
            tool_calls=tuple(getattr(response, "tool_calls", ()) or ()),
            tools=tuple(tools or ()),
            params=merged,
            usage=getattr(response, "usage", None) or {},
        )
        return response
