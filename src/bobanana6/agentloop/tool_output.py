"""The single door a tool result passes on its way into the transcript.

★ Why the rule lives here and **only** here: every ``tool`` row -- one produced by
an executed call, one synthesized for a guardrail block, one synthesized for
unusable arguments -- is built in ``loop._tool_round``. One implementation there
covers every tool at once. Implemented per tool it would be one copy per tool, and
copies drift (``AGENTS §7③``: single-point closure).

The rule is **paging, not destruction** (``架构设计`` "上下文当虚拟内存"):

```
① at or under the threshold -> byte-for-byte unchanged (no spill, no extra text,
   no event). Small returns are not touched at all.
② over the threshold -> the first ``head_chars`` and the last ``tail_chars``
   characters, with the omitted middle **announced**: how much was dropped and
   where the full text can be read back.
③ both ends are kept -> the head carries the header / first rows, the tail carries
   the error / final value. Keeping only one end hides the other half.
④ if the full text could not be written somewhere readable -> **do not page**:
   hand over the whole content. A page that cannot be read back is destruction,
   not paging.
```

★ Metadata rides **inside** the ``content`` text, never as new message keys: a
strict OpenAI-compatible endpoint rejects unknown fields (see
``adapters/openai_compat.py::INTERNAL_MESSAGE_KEYS``, which only drops the shell's
own keys).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "DEFAULT_HEAD_CHARS",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_TAIL_CHARS",
    "ENV_HEAD_CHARS",
    "ENV_MAX_CHARS",
    "ENV_TAIL_CHARS",
    "PagedOutput",
    "ToolOutputPolicy",
    "page_tool_output",
]

#: ★ Defaults come from the offline estimate over the ``qwen4b6`` archives
#: (4000 chars pages ~83% off the single-turn peak, not just 41% of the total).
#: They are the *fallback*; the environment overrides them (see ``from_env``).
DEFAULT_MAX_CHARS = 4000
DEFAULT_HEAD_CHARS = 800
DEFAULT_TAIL_CHARS = 800

#: ★ Scoped name with an explicit unit, following the same discipline as
#: ``BOBANANA6_TIMEOUT_S`` / ``BASH_MAX_OUTPUT_LENGTH``. Never a bare ``_MAX``.
ENV_MAX_CHARS = "BOBANANA6_TOOL_MAX_OUTPUT_CHARS"
ENV_HEAD_CHARS = "BOBANANA6_TOOL_HEAD_CHARS"
ENV_TAIL_CHARS = "BOBANANA6_TOOL_TAIL_CHARS"


def _env_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} must be an integer number of characters, got {raw!r}"
        ) from None


@dataclass(frozen=True)
class ToolOutputPolicy:
    """When a tool result is paged, and how much of each end survives.

    ``head_chars + tail_chars <= max_chars`` is required: with a larger sum the
    two windows could overlap (the same characters shown twice) and the
    "omitted" reading would lie. A return **exactly at** ``max_chars`` is not
    paged -- the comparison is strict (``length > max_chars``), so the boundary is
    not ambiguous.
    """

    max_chars: int = DEFAULT_MAX_CHARS  # blame: fail-closed — 省略即用出厂阈值 4000（把大返回换页），方向保守；真正的值由 env 提供
    head_chars: int = DEFAULT_HEAD_CHARS  # blame: fail-closed — 省略即头留 800 字符（表头/前几行），方向保守
    tail_chars: int = DEFAULT_TAIL_CHARS  # blame: fail-closed — 省略即尾留 800 字符（报错/最终值），方向保守

    def __post_init__(self) -> None:
        if self.max_chars < 1:
            raise ValueError(f"ToolOutputPolicy.max_chars must be >= 1, got {self.max_chars}")
        if self.head_chars < 1 or self.tail_chars < 1:
            raise ValueError(
                "ToolOutputPolicy.head_chars and tail_chars must be >= 1, "
                f"got {self.head_chars} / {self.tail_chars}"
            )
        if self.head_chars + self.tail_chars > self.max_chars:
            raise ValueError(
                "ToolOutputPolicy.head_chars + tail_chars must be <= max_chars "
                f"(got {self.head_chars} + {self.tail_chars} > {self.max_chars}); "
                "otherwise the two windows overlap"
            )

    def pages(self, length: int) -> bool:
        """Strict: a return exactly at the threshold is left alone."""
        return length > self.max_chars

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ToolOutputPolicy:  # blame: whitelist — None = 读进程环境（生产路径）；测试传显式映射，方向不需要另判
        """Read the three knobs from the environment (defaults when unset/empty)."""
        values = os.environ if environ is None else environ
        return cls(
            max_chars=_env_int(values, ENV_MAX_CHARS, DEFAULT_MAX_CHARS),
            head_chars=_env_int(values, ENV_HEAD_CHARS, DEFAULT_HEAD_CHARS),
            tail_chars=_env_int(values, ENV_TAIL_CHARS, DEFAULT_TAIL_CHARS),
        )


@dataclass(frozen=True)
class PagedOutput:
    """The paged body plus the readings the caller reports (never guessed)."""

    body: str
    total_chars: int
    total_lines: int
    head_chars: int
    tail_chars: int
    omitted_chars: int


def _marker(page: PagedOutput, full_path: str) -> str:
    percent = page.omitted_chars * 100.0 / page.total_chars if page.total_chars else 0.0
    return (
        f"…[tool output paged: {page.total_chars} chars / {page.total_lines} lines in total; "
        f"kept the first {page.head_chars} and the last {page.tail_chars} chars, "
        f"{page.omitted_chars} chars ({percent:.1f}%) omitted. "
        f"Full content: {full_path} (workspace-relative). "
        f"Read it back in windows, e.g. `sed -n '1,200p' {full_path}`; "
        f"a paged source can also be re-read by calling sources.read again with a "
        f"larger offset or a smaller limit.]…"
    )


def page_tool_output(content: str, policy: ToolOutputPolicy, *, full_path: str) -> PagedOutput:
    """Build the head + tail + pointer body for ``content``.

    The caller must have established that ``content`` is over the threshold and
    that its full text really is at ``full_path`` -- this function assumes the
    page is readable back. It never truncates silently: the omitted size and the
    location are in the body.
    """
    length = len(content)
    if not policy.pages(length):
        raise ValueError(
            f"page_tool_output called on {length} chars, which is not over "
            f"the {policy.max_chars}-char threshold"
        )
    head = content[: policy.head_chars]
    tail = content[length - policy.tail_chars :]
    page = PagedOutput(
        body="",
        total_chars=length,
        total_lines=len(content.split("\n")),
        head_chars=len(head),
        tail_chars=len(tail),
        omitted_chars=length - len(head) - len(tail),
    )
    return PagedOutput(
        body=f"{head}\n{_marker(page, full_path)}\n{tail}",
        total_chars=page.total_chars,
        total_lines=page.total_lines,
        head_chars=page.head_chars,
        tail_chars=page.tail_chars,
        omitted_chars=page.omitted_chars,
    )
