"""Paged reading and regex search over a carrier's text.

★ `#47`: 抄的是 Hermes `read_file` / `search_files` 的**行为契约**（带行号的分页读、
正则 + 上下文行 + 分页检索），**不是**它们的装配。这里只做纯文本运算：零 I/O、零
权限判断 —— 谁能读哪个载体由 `core/ports.py::ToolSet`/`BoundSources.resolve` 说了算，
本模块拿到的已经是一段**允许读的文本**。

三个语义是这个模块存在的理由（对应 `抄Hermes的工具调用-九条设计对照`）：
  · **分页**：一次只回一个窗口，`next_offset` 指到下一窗（整份取回来会撑爆上下文）；
  · **行号**：输出带 `LINE_NUM|CONTENT`，引用才指得回去；
  · **检索**：正则 + 上下文行 + 分页，`output_mode` 决定只要行号还是要内容行。

★ 一处的取值域是**闭集**：越界的 `offset` / `limit` / `context` 一律**具名拒**，
不静默夹取 —— 静默夹取会让调用方以为它要的那一段已经看完了。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "OUTPUT_MODES",
    "TARGETS",
    "TextArgumentError",
    "grep_names",
    "line_window",
    "search_lines",
    "split_lines",
]

#: 一窗最多回多少行 / 调用方可以把它调到多大。
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
#: `sources.grep` 的两种取值域（Hermes `search_files` 的 `target`）。
TARGETS: tuple[str, ...] = ("content", "files")
#: `output_mode`：只要命中行的行号 / 连内容一起给。默认连内容给 —— 分页读的意义就是内容。
OUTPUT_MODES: tuple[str, ...] = ("content", "files_only")


class TextArgumentError(ValueError):
    """The caller's arguments cannot be honoured -- refuse by name, never clamp.

    The caller wrote the arguments, so the declaring layer is the model: it can
    see the refusal and correct itself (``AGENTS.md §7⑫``).
    """

    blame = "model"


@dataclass(frozen=True)
class _Line:
    number: int  # 1-based, as an editor shows it
    text: str


def split_lines(text: str) -> tuple[str, ...]:
    """The carrier's lines, with a trailing newline **not** counted as an extra line.

    ``"a\\nb\\n".splitlines()`` gives two lines, which is what a reader means by
    "line 2 is the last line". A file that is only a newline therefore has one
    (empty) line rather than zero.
    """
    return tuple(text.splitlines())


def _integer(name: str, raw: Any, *, default: int, minimum: int, maximum: int | None = None) -> int:
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        raise TextArgumentError(f"{name} must be an integer, got a boolean")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise TextArgumentError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise TextArgumentError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise TextArgumentError(f"{name} must be <= {maximum}, got {value}")
    return value


def _flag(name: str, raw: Any, *, default: bool = False) -> bool:
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise TextArgumentError(f"{name} must be a boolean, got {raw!r}")


def line_window(text: str, *, offset: int = 0, limit: int = DEFAULT_LIMIT) -> Mapping[str, Any]:
    """One line-numbered window of ``text`` -- the `read_file(path, offset, limit)` contract.

    ``offset`` is a 0-based **line** offset and ``limit`` a line count, so a
    caller that ran out of window asks for ``next_offset`` instead of guessing.
    """
    start = _integer("offset", offset, default=0, minimum=0)
    count = _integer("limit", limit, default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
    lines = split_lines(text)
    total = len(lines)
    end = min(start + count, total)
    window = lines[start:end] if start < total else ()
    return {
        "total_lines": total,
        "total_chars": len(text),
        "offset": start,
        "limit": count,
        "returned_lines": len(window),
        # ★ 行号从 1 起（编辑器口径）；越界窗口返回空列表 + `next_offset=None`，
        #   而不是"回到第 0 行"这种会让调用方死循环的静默夹取。
        "lines": [{"line": start + index + 1, "text": line} for index, line in enumerate(window)],
        "next_offset": end if end < total else None,
        "truncated": end < total,
    }


def _compile(pattern: str, *, ignore_case: bool) -> re.Pattern[str]:
    if not isinstance(pattern, str) or pattern == "":
        raise TextArgumentError("pattern must be a non-empty regular expression")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise TextArgumentError(f"pattern {pattern!r} is not a valid regular expression: {exc}") from exc


def search_lines(
    text: str,
    pattern: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    context: int = 0,
    ignore_case: bool = False,
    output_mode: str = "content",
) -> Mapping[str, Any]:
    """Regex search over the carrier's lines, with context lines and pagination.

    ``offset``/``limit`` paginate over **matches** (not lines) so a file with a
    million hits still answers in bounded chunks; ``context`` adds that many
    lines on either side of each match (clipped to the carrier's edges).
    """
    compiled = _compile(pattern, ignore_case=ignore_case)
    if output_mode not in OUTPUT_MODES:
        raise TextArgumentError(f"output_mode must be one of {list(OUTPUT_MODES)!r}, got {output_mode!r}")
    skip = _integer("offset", offset, default=0, minimum=0)
    count = _integer("limit", limit, default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
    around = _integer("context", context, default=0, minimum=0, maximum=MAX_LIMIT)
    lines = split_lines(text)

    hits = [(index, line) for index, line in enumerate(lines) if compiled.search(line)]
    window = hits[skip : skip + count]
    matches: list[dict[str, Any]] = []
    for index, line in window:
        entry: dict[str, Any] = {"line": index + 1}
        if output_mode == "content":
            entry["text"] = line
            if around:
                low = max(0, index - around)
                high = min(len(lines), index + around + 1)
                entry["context"] = [
                    {"line": position + 1, "text": lines[position], "match": position == index}
                    for position in range(low, high)
                ]
        matches.append(entry)
    return {
        "pattern": pattern,
        "total_lines": len(lines),
        "total_matches": len(hits),
        "offset": skip,
        "limit": count,
        "returned_matches": len(matches),
        "matches": matches,
        "next_offset": skip + count if skip + count < len(hits) else None,
        "truncated": skip + count < len(hits),
    }


def grep_names(names: Sequence[str], pattern: str, *, ignore_case: bool = False) -> Mapping[str, Any]:
    """``target="files"``: which of the names this block may read match the pattern.

    Searching *names* is the half of Hermes' `search_files(target="files")` that
    survives the move to a source-ref world: a block has a handful of authorized
    carriers, and finding the right one by name is a real step before reading it.
    """
    compiled = _compile(pattern, ignore_case=ignore_case)
    matched = [name for name in names if compiled.search(name)]
    return {"pattern": pattern, "target": "files", "total_matches": len(matched), "matches": matched}
