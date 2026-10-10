"""Text-form tool calls: detect them, never execute them, tell the model.

★ Measured (archives, not a hunch): a model sometimes writes a tool call as **text in
  the reply body** instead of through the `tool_calls` field. The call never runs and,
  before this module, nothing said so -- the model kept using the same shape.

```
qwen4b6  T-05/65  227 chars   <function=shell_run><parameter=command>...
qwen4b6  T-06/120 21,072 chars (same dialect, truncated: no closing tags)
xing29b  T-01/8   24,597 chars <tool_call>write<param_key>path</param_key><param_value>...
```

The dialect table is a **constant, extended only from what was actually observed** --
copying another project's full list would guess at shapes nobody here has seen.

★★ Two invariants:

* **Never execute.** Recovering and running the text would mean the shell guessing the
  model's intent; a wrong guess silently runs a command that was never authorised.
  The call did not happen -- so nothing should happen.
* **宁可漏认，不许错认.** A false positive injects a bogus notice and teaches the loop
  to distrust a perfectly good answer. So each dialect needs its *own* start marker, a
  parameter-shaped marker, and non-empty parameter content; and matches **inside a code
  fence are ignored** (an example is not a call -- every archived leak was unfenced).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "DIALECT_FUNCTION_PARAMETER",
    "DIALECT_TOOL_CALL_PARAM_KEY",
    "TEXT_TOOL_CALL_NOTICE",
    "TextToolCall",
    "detect_text_tool_call",
]

DIALECT_FUNCTION_PARAMETER = "function_parameter"
DIALECT_TOOL_CALL_PARAM_KEY = "tool_call_param_key"

#: The two shapes actually observed. Marker regexes are deliberately narrow:
#: a bare `<function>` (no `=name`) or `<parameter>` (no `=name`) is not a dialect.
_FUNCTION_OPEN = re.compile(r"<\s*function\s*=\s*([A-Za-z_][\w.\-]*)\s*>")
_PARAMETER_OPEN = re.compile(r"<\s*parameter\s*=\s*([A-Za-z_][\w.\-]*)\s*>")
_PARAMETER_CLOSE = re.compile(r"<\s*/\s*parameter\s*>")
_FUNCTION_CLOSE = re.compile(r"<\s*/\s*function\s*>")
_TOOL_CALL_OPEN = re.compile(r"<\s*tool_call\s*>\s*([A-Za-z_][\w.\-]*)")
_PARAM_KEY = re.compile(r"<\s*param_key\s*>\s*([A-Za-z_][\w.\-]*)\s*<\s*/\s*param_key\s*>")
_PARAM_VALUE = re.compile(r"<\s*param_value\s*>")
_PARAM_VALUE_CLOSE = re.compile(r"<\s*/\s*param_value\s*>")
#: 行内反引号里的东西也是"示例"，不是调用（与代码围栏同一条取舍）。
_INLINE_CODE = re.compile(r"`[^`\n]*`")

#: The one sentence the model is shown on the next turn. It states what happened and
#: what to do -- it does not pretend to be a tool result, and it does not scold.
TEXT_TOOL_CALL_NOTICE = (
    "上一轮你的回复把（某个）工具调用写成了**正文里的文本**，而不是通过 tool_calls 通道："
    "它没有被解析，也没有被执行。工具调用只认工具通道 —— "
    "请把刚才那一步**改用工具通道重发一次**；你已经写下的正文原样保留。"
)


@dataclass(frozen=True)
class TextToolCall:
    """One detected leak. ``chars`` is the whole reply length -- that is what was burned."""

    dialect: str
    chars: int
    names: tuple[str, ...]
    snippet: str


def detect_text_tool_call(text: str) -> TextToolCall | None:
    """Return the first known text-form call outside code fences, or ``None``.

    ``None`` is the common answer and must stay cheap: a normal answer with a JSON body,
    a code example, or stray angle brackets returns immediately.
    """
    if not text or ("<" not in text):
        return None
    fences = _ignored_spans(text)
    found = _dialect_function_parameter(text, fences) or _dialect_tool_call_param_key(text, fences)
    return found


def _dialect_function_parameter(text: str, fences: list[tuple[int, int]]) -> TextToolCall | None:
    for opener in _FUNCTION_OPEN.finditer(text):
        if _in_fence(opener.start(), fences):
            continue
        tail = text[opener.end():]
        parameter = _PARAMETER_OPEN.search(tail)
        if parameter is None:
            continue
        body_start = opener.end() + parameter.end()
        if _in_fence(body_start, fences):
            continue
        body_end = _parameter_body_end(text, body_start)
        if not text[body_start:body_end].strip():
            continue  # `<parameter=x></parameter>` with nothing in it is not a call
        return TextToolCall(
            dialect=DIALECT_FUNCTION_PARAMETER,
            chars=len(text),
            names=(opener.group(1), parameter.group(1)),
            snippet=_snippet(text, opener.start()),
        )
    return None


def _dialect_tool_call_param_key(text: str, fences: list[tuple[int, int]]) -> TextToolCall | None:
    for opener in _TOOL_CALL_OPEN.finditer(text):
        if _in_fence(opener.start(), fences):
            continue
        tail = text[opener.end():]
        key = _PARAM_KEY.search(tail)
        if key is None:
            continue
        value = _PARAM_VALUE.search(tail, key.end())
        if value is None:
            continue
        # ★ `<param_value></param_value>`（空值）不是调用（只读审计 #135）：与方言 A 的
        #   "参数体非空"同一条判据。
        value_start = opener.end() + value.end()
        if not text[value_start:_param_value_body_end(text, value_start)].strip():
            continue
        return TextToolCall(
            dialect=DIALECT_TOOL_CALL_PARAM_KEY,
            chars=len(text),
            names=(opener.group(1), key.group(1)),
            snippet=_snippet(text, opener.start()),
        )
    return None


def _parameter_body_end(text: str, start: int) -> int:
    """End of a parameter body: the earliest close, the next parameter, or end-of-text.

    End-of-text matters: one archived leak was **truncated mid-command** (21,072 chars,
    no closing tag at all) -- requiring a close would miss exactly the expensive case.
    """
    ends = [
        match.start()
        for match in (
            _PARAMETER_CLOSE.search(text, start),
            _PARAMETER_OPEN.search(text, start),
            _FUNCTION_CLOSE.search(text, start),
        )
        if match is not None
    ]
    return min(ends) if ends else len(text)


def _param_value_body_end(text: str, start: int) -> int:
    """End of a `<param_value>` body: the close, the next `<param_key>`, or end-of-text."""
    ends = [
        match.start()
        for match in (_PARAM_VALUE_CLOSE.search(text, start), _PARAM_KEY.search(text, start))
        if match is not None
    ]
    return min(ends) if ends else len(text)


def _ignored_spans(text: str) -> list[tuple[int, int]]:
    """Fenced code blocks plus inline-code spans -- "an example is not a call"."""
    return _fence_spans(text) + [(m.start(), m.end()) for m in _INLINE_CODE.finditer(text)]


def _fence_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of fenced code blocks (``` / ~~~), toggled per line."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            if start is None:
                start = offset
            else:
                spans.append((start, offset + len(line)))
                start = None
        offset += len(line)
    if start is not None:
        spans.append((start, len(text)))
    return spans


def _in_fence(position: int, fences: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in fences)


def _snippet(text: str, start: int, limit: int = 120) -> str:  # blame: whitelist — 只为诊断截一段，120 是纯展示预算
    return text[start : start + limit]
