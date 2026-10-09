"""★ 块之间的【过程级信息交换】—— 存过程 + 下游按需检索（照 ARTEX 机制 ② 的语义）。

**为什么要有它**（实测缺口，A 臂 `qwen4b2`）：T-03 的 `handoff_note` 原话写着

> 「④ 不确定处：上游 T-02 被判 failed 的具体原因【我没能从产物中定位】」

于是 T-03 花自己的预算**重建上游产物**来验证上游 —— 因为它拿不到"上游到底试过什么、
被什么拒了、卡在哪一步"。这些观察**没有被写成正式产物**，所以交接件里没有。

**与现有机制的关系（互补，不是替代）**：

```
交接件（已有，`produce.py` 的 handoff）  = 结果，【推】给下游（被动接收）
本模块（过程检索）                      = 过程，下游【主动拉】
```

**两条硬边界**：

```
① 可见范围 = 本块的【上游闭包】内、且已跑过的块。判据用 DAG 依赖现算，不硬编码块名。
   下游拿不到、平行的别块也拿不到 —— 过程里常有半成品与错路，扩散出去只会污染。
② 读了工作区之外的文件（判分器那种）的那一步：**结果内容不进索引**，
   只留"它跑了什么命令"。过程检索是复习上游的现场，不是绕开工作区的后门。
```

★ 预算：只存**文本 + 必要元数据**；单条与单块都有上限（见下面的常量），
上限用尽**具名记录**，不静默丢。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.model import Subtask

__all__ = [
    "REDACTED_RESULT",
    "TRACE_ARGS_MAX_CHARS",
    "TRACE_MAX_CHARS_PER_BLOCK",
    "TRACE_MAX_ENTRIES_PER_BLOCK",
    "TRACE_RESULT_MAX_CHARS",
    "TRACE_SEARCH_HITS",
    "TRACE_SEARCH_LIMIT",
    "TraceEntry",
    "TraceError",
    "TraceStore",
    "upstream_closure",
]

#: 单条 args / result 的字符上限。超了**可见地**截断（不静默）—— 过程是"现场"，不是全文备份。
TRACE_ARGS_MAX_CHARS = 1000
TRACE_RESULT_MAX_CHARS = 2000
#: 单块的**条数**与**总字符**上限（预算：只存文本 + 必要元数据）。
#: 200 条 × 平均 ~1KB ≈ 200KB —— 一次块级探索的工具调用规模上限就在这个量级。
TRACE_MAX_ENTRIES_PER_BLOCK = 200
TRACE_MAX_CHARS_PER_BLOCK = 200_000
#: 一个块在一次跑测里能调 `traces.*` 多少次（search 与 read **合并**计）。
#: 12：够"搜两三个关键词 + 读两三组步骤"，又不足以把上游过程整份拖进上下文。
TRACE_SEARCH_LIMIT = 12
#: 一次 search 最多回多少条命中（再多就该换关键词，而不是拿更大的列表）。
TRACE_SEARCH_HITS = 20

#: 结果未索引时，`read` 给模型看的那句话 —— 内容**一个字节都不出现**。
REDACTED_RESULT = "(结果未索引：这一步读了工作区之外的文件；只保留它跑了什么命令)"


class TraceError(RuntimeError):
    """过程检索被拒（调用方引起 —— 越权、超限、查不到）。具名，不静默降级。"""

    blame = "model"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated {len(text) - limit} chars]"


def _safe_text(text: Any) -> str:
    """把**孤立代理字符**换掉 —— 它们能写坏 UTF-8 编码。

    ★ 工具结果里可能带**孤立代理字符**（任何工具输出都可能）；直接落盘会抛
      `UnicodeEncodeError`，那会把整个 run 打挂 —— 痕迹绝不许有这种权力。
    """
    return str(text).encode("utf-8", "replace").decode("utf-8")


def upstream_closure(subtask: Subtask, subtasks: Sequence[Subtask]) -> tuple[str, ...]:
    """本块的**上游闭包**（产出它每一份直接/间接输入的那些块 id），按 id 排序。

    ★ 判据是 DAG 声明本身（`Subtask.inputs` 是**输出名**，`Subtask.output` 是产出名），
      不硬编码任何块名：换一道题、换一种切分，这里照样对。
    ★ 排除自己：过程检索是"看上游的现场"，不是"复习自己"。
    """
    by_output = {block.output: block for block in subtasks}
    seen: set[str] = set()
    pending = list(subtask.inputs)
    while pending:
        name = pending.pop()
        producer = by_output.get(name)
        if producer is None or producer.id == subtask.id or producer.id in seen:
            continue
        seen.add(producer.id)
        pending.extend(producer.inputs)
    return tuple(sorted(seen))


@dataclass(frozen=True)
class TraceEntry:
    """一次工具调用留下的**过程**记录（不是判定结论）。"""

    block_id: str
    seq: int
    #: 这是块内第几次【模型调用】（`LoopIOImpl.calls`）—— `seq` 是第几次**工具调用**。
    #: 两个都要：`seq` 定位"哪一步"，`turn` 说明"它当时在第几轮"（复盘时很不一样）。
    turn: int
    tool: str
    args_text: str
    result_text: str
    at: float
    ok: bool
    error_type: str = ""  # blame: whitelist — 失败类型的**证据字段**，缺省空串不参与任何判定
    #: False = 这一步读了工作区外的文件 ⇒ 结果内容**不进索引**（`result_text` 为空）。
    result_indexed: bool = False  # blame: fail-closed — 省略即"不索引这条结果"（宁可不索引，也不漏判分器内容）

    def hit(self, snippet: str) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "seq": self.seq,
            "tool": self.tool,
            "ok": self.ok,
            "result_indexed": self.result_indexed,
            "snippet": snippet,
        }

    def step(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "seq": self.seq,
            "turn": self.turn,
            "tool": self.tool,
            "ok": self.ok,
            "error_type": self.error_type,
            "at": self.at,
            "args": self.args_text,
            "result": self.result_text if self.result_indexed else REDACTED_RESULT,
            "result_indexed": self.result_indexed,
        }


@dataclass
class TraceStore:
    """一次跑测里所有块的**过程**记录（内存 + 事后落到痕迹仓库）。

    ★ 它是**共享**的：runner 持有一份，每个块都往同一份里写、也都从同一份里读
      （读的范围由 `BoundTraces.visible` 限死在上游闭包）。
    """

    by_block: dict[str, list[TraceEntry]] = field(default_factory=dict)  # blame: fail-closed — 省略即"一条过程都没记过"，不会凭空造
    chars_by_block: dict[str, int] = field(default_factory=dict)  # blame: fail-closed — 省略即"零消耗"，只会更早触发上限
    #: 预算用尽被丢掉的条目（**具名**，不静默）—— 丢的是过程，不是判定。
    dropped: list[dict[str, Any]] = field(default_factory=list)  # blame: whitelist — 只是"被丢过什么"的证据清单，缺省空 = 没丢过

    # -- 写 ------------------------------------------------------------------ #

    def add(
        self,
        *,
        block_id: str,
        turn: int,
        tool: str,
        args_text: str,
        result_text: str,
        at: float,
        ok: bool,
        error_type: str = "",  # blame: whitelist — 失败类型的证据字段，缺省空串不改变任何行为
        result_indexed: bool = False,  # blame: fail-closed — 省略即"不索引这条结果"（宁可不索引，也不漏进判分器内容）
    ) -> TraceEntry | None:
        """记一条过程。超预算 ⇒ 记进 `dropped` 并返回 `None`（不静默丢）。"""
        # ★ 这两个默认值是**显式**的（默认值审计要求逐条说明方向），不是隐式吞掉。
        entries = self.by_block.get(block_id, [])  # blame: fail-closed — 省略即空列表（"没记过"），不会凭空造一条
        seq = len(entries) + 1
        # ★ 孤立代理字符在边界上就换掉：trace 不许把业务跑挂（见 `_safe_text`）。
        clipped_args = _clip(_safe_text(args_text), TRACE_ARGS_MAX_CHARS)
        # ★ 不索引的结果**根本不存正文**（不是存了再遮）—— 这是那条硬边界唯一诚实的实现。
        clipped_result = _clip(_safe_text(result_text), TRACE_RESULT_MAX_CHARS) if result_indexed else ""
        cost = len(clipped_args) + len(clipped_result)
        used = self.chars_by_block.get(block_id, 0)  # blame: fail-closed — 省略即零消耗，只会更早触发上限
        if len(entries) >= TRACE_MAX_ENTRIES_PER_BLOCK or used + cost > TRACE_MAX_CHARS_PER_BLOCK:
            self.dropped.append(
                {
                    "block": block_id,
                    "seq": seq,
                    "turn": int(turn),
                    "tool": str(tool),
                    "reason": "trace budget exhausted",
                    "used_chars": used,
                    "capacity_chars": TRACE_MAX_CHARS_PER_BLOCK,
                    "capacity_entries": TRACE_MAX_ENTRIES_PER_BLOCK,
                }
            )
            return None
        entry = TraceEntry(
            block_id=block_id,
            seq=seq,
            turn=int(turn),
            tool=str(tool),
            args_text=clipped_args,
            result_text=clipped_result,
            at=float(at),
            ok=bool(ok),
            error_type=str(error_type or ""),
            result_indexed=bool(result_indexed),
        )
        entries.append(entry)
        self.by_block[block_id] = entries
        self.chars_by_block[block_id] = used + cost
        return entry

    def dropped_for(self, block_id: str) -> int:
        """这一块被预算丢掉了多少条（**具名**读数：丢的是过程，不是判定）。"""
        return sum(1 for entry in self.dropped if entry.get("block") == block_id)

    # -- 读 ------------------------------------------------------------------ #

    def blocks(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_block))

    def entries(self, block_id: str) -> tuple[TraceEntry, ...]:
        return tuple(self.by_block.get(str(block_id), ()))  # blame: fail-closed — 省略即"这块没有过程"，返回空

    def search(
        self,
        query: str,
        blocks: Sequence[str],
        limit: int = TRACE_SEARCH_HITS,  # blame: fail-closed — 省略即给一个**有上限**的命中数，方向保守
    ) -> dict[str, Any]:
        """在**给定的可见块**里按关键字找（大小写不敏感）。

        ★ 只搜 `args_text` + （`result_indexed` 的）`result_text`：
          未索引的结果正文根本没存，所以**不可能**从这条路径泄漏出去。
        ★ 截断**可见**：回 `total` 与 `truncated` —— "真就这么多"与"被砍了"必须分得清
          （`textops.line_window` 同一条纪律）。
        """
        needle = _safe_text(query).strip().lower()
        if not needle:
            raise TraceError("traces.search needs a non-empty query (violated: query is empty)")
        visible = {str(name) for name in blocks}
        matched: list[dict[str, Any]] = []
        for block_id in sorted(self.by_block):
            if block_id not in visible:
                continue
            for entry in self.by_block[block_id]:
                haystacks = [entry.args_text]
                if entry.result_indexed:
                    haystacks.append(entry.result_text)
                found = next((text for text in haystacks if needle in text.lower()), None)
                if found is None:
                    continue
                index = found.lower().index(needle)
                matched.append(entry.hit(_clip(found[max(0, index - 80) : index + 160], 240)))
        return {
            "hits": matched[:limit],
            "total": len(matched),
            "truncated": len(matched) > limit,
        }

    def read(self, block_id: str, seqs: Sequence[int]) -> tuple[dict[str, Any], ...]:
        wanted = {int(seq) for seq in seqs}
        return tuple(entry.step() for entry in self.entries(block_id) if entry.seq in wanted)

    def summary(self) -> dict[str, Any]:
        """给验收/报告看的读数：条数 + 单条大小分布 + 总大小。"""
        sizes = [len(e.args_text) + len(e.result_text) for entries in self.by_block.values() for e in entries]
        return {
            "blocks": list(self.blocks()),
            "entries": len(sizes),
            "entries_by_block": {block: len(entries) for block, entries in sorted(self.by_block.items())},
            "chars_total": sum(sizes),
            "chars_min": min(sizes) if sizes else 0,
            "chars_max": max(sizes) if sizes else 0,
            "dropped": len(self.dropped),
        }

    # -- 落盘（复用痕迹仓库） -------------------------------------------------- #

    def as_lines(self, block_id: str) -> list[dict[str, Any]]:
        """这一块的过程记录，一行一条（给 `ArtifactStore` 写 `traces.jsonl`）。"""
        return [entry.step() for entry in self.entries(block_id)]
