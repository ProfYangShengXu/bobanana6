"""★ 缺口③ —— 子进程输出的编码兜底。

**为什么单独一个模块**：编码策略必须只有一份。`shell.run`（`workspace.py`）、
契约的 `command` / `execution` 判据（`checks.py`）、痕迹仓库的 git 子进程
（`artifacts.py`）过去各自 `text=True` —— 等价于「locale 编码 + `errors="strict"`」。
子进程一吐非该编码的字节（中文 Windows 下 `cmd /c echo 中文` 是 GBK / cp936，
首字节 `0xd5`），`subprocess` 的读取线程就抛 `UnicodeDecodeError`：

* POSIX / `communicate()`：异常在主线程抛出，**整份输出丢**；
* Windows（`subprocess.py:1599 _readerthread`）：异常只在子线程打印，线程死掉，
  `communicate()` 可能返回空/残缺，而工具仍报 `exit 0` —— 静默。

**策略（三条一起才完整）**：

1. **不假定 UTF-8**：先取平台本地编码（Windows = ANSI 代码页，中文即 GBK /
   cp936；POSIX = locale 的 preferred encoding），再依次回退 `utf-8`、`gbk`。
2. 一律 `errors="replace"` 兜底：崩线程比丢几个字符糟得多。所有 `text=True` 的
   调用点用 :func:`text_mode_kwargs` 拿同一组 `encoding` / `errors`。
3. **不许静默**：`shell.run` 走字节管道 + :func:`decode_process_output`，回退链
   全失败时按 `errors="replace"` 解码，并把替换字符数写进工具结果
   （`decode_replacements` / `decode_note`）—— 结果本身就是 trace 的一部分。

★ 这里只做解码，不改任何判定；`shell.run` 的 `expect` 断言仍然对解码后的
全量 stdout+stderr 做（编码不会改变命令自己的退出码）。
"""

from __future__ import annotations

import locale
import os
from functools import lru_cache
from typing import Any, NamedTuple

__all__ = [
    "DEFAULT_FALLBACK_ENCODINGS",
    "DecodedOutput",
    "candidate_encodings",
    "decode_process_output",
    "text_mode_kwargs",
]

#: 回退链的尾部。`gbk` 是中文 Windows 的 ANSI 代码页（cp936 的超集别名），
#: 放在 `utf-8` 之后：UTF-8 文本先按 UTF-8 解，GBK 字节才轮到它。
DEFAULT_FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gbk")


@lru_cache(maxsize=1)
def _platform_encoding() -> str:
    """The platform's own encoding -- **never** hard-coded UTF-8.

    Windows native commands (`netstat`, `tasklist`, `cmd /c echo`) write the ANSI
    code page even when `PYTHONUTF8=1`; on a Chinese Windows that is cp936/GBK.
    POSIX takes the locale's preferred encoding (usually UTF-8).
    """
    if os.name == "nt":
        try:
            return locale.getencoding()
        except AttributeError:  # pragma: no cover - Python < 3.11
            return locale.getpreferredencoding(False)
    return locale.getpreferredencoding(False) or "utf-8"


def candidate_encodings() -> tuple[str, ...]:
    """Encodings tried in order: platform first, then UTF-8, then GBK (deduped)."""
    chain: list[str] = []
    for encoding in (_platform_encoding(), *DEFAULT_FALLBACK_ENCODINGS):
        normalized = (encoding or "").strip()
        if normalized and normalized.lower() not in {item.lower() for item in chain}:
            chain.append(normalized)
    return tuple(chain) or ("utf-8",)


def text_mode_kwargs() -> dict[str, Any]:
    """The kwargs every ``text=True`` subprocess call must pass.

    Explicit `encoding` + `errors="replace"` is the whole point: `text=True` alone
    means locale-encoding + strict decoding, which is what killed the reader
    thread. Returning a fresh dict keeps call sites from mutating a shared one.
    """
    return {"text": True, "encoding": candidate_encodings()[0], "errors": "replace"}


class DecodedOutput(NamedTuple):
    """Decoded text plus the replacement fact (empty/replacements=0 on a clean decode)."""

    text: str
    replacements: int
    note: str


def decode_process_output(raw: bytes | None) -> DecodedOutput:
    """Decode a child's byte output; never raise on bad bytes.

    Tries the candidate encodings with strict decoding first (so GBK bytes come
    back as 中文 when the locale is UTF-8), and only if **all** fail falls back to
    ``errors="replace"`` on the platform encoding. The returned second value is a
    trace payload: empty on a clean decode, otherwise
    ``{"decode_replacements": n, "decode_note": "..."}`` -- "replacement happened"
    is a fact the caller must be able to see, not a silent loss.
    """
    if not raw:
        return DecodedOutput("", 0, "")
    for encoding in candidate_encodings():
        try:
            return DecodedOutput(raw.decode(encoding), 0, "")
        except (UnicodeDecodeError, LookupError):
            continue
    text = raw.decode(candidate_encodings()[0], errors="replace")
    replaced = text.count("\ufffd")
    return DecodedOutput(
        text,
        replaced,
        "the child's output was not valid in any known encoding "
        f"({list(candidate_encodings())!r}); {replaced} byte(s) were replaced with U+FFFD",
    )
