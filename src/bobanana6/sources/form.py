"""源载体的**形式**：派活前把它规整成任务声明的样子（`#32`）。

★ 这是**机制层**，不是领域补丁：

```
声明（领域层）  任务在 sources[] 上写 encoding / eol —— 报表题写"单位/列名"在别处，
                这里写的是"这份载体的字节形式"
动作（机制层）  解码 + 行尾，只有这一套；壳里【不出现任何领域词】
```

为什么放在"派活前"：块拿到的应当是**下游能直接用的形式**。修前的投递点
（`services/registry.py::snapshot_payload` 与 `sources/reader.py::SnapshotFileReader.body`）
把字节一律 `decode("utf-8", errors="replace")`：非 UTF-8 的源被**静默**替换成 U+FFFD，
CRLF / BOM 原样塞进文本 —— 块看不出自己拿到的是被改过的字（`#32` 第 0 步的读数）。

两条边界（照着 `#18` 的「假设要说出来，不许沉默」）：

* **声明了**就用声明解，解不动就抛具名错（`SourceFormError`，重试无用）；
* **没声明**就按 UTF-8 猜，但**把假设写进载荷**（`assumed` + 替换了几个字符）——
  不许静默替换。
"""

from __future__ import annotations

import codecs
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core.model import EOL_MODES as _EOL_MODES

__all__ = [
    "DEFAULT_ENCODING",
    "DEFAULT_FORM",
    "EOL_MODES",
    "DecodedBody",
    "SourceForm",
    "SourceFormError",
    "decode_body",
    "form_for",
    "forms_for",
]

#: 没声明 encoding 时按它猜（与修前一致），但这句话要写进载荷。
DEFAULT_ENCODING = "utf-8"

#: 行尾词表在 `core/model.py`（声明解析与规整动作都读它，不各写一份）。
EOL_MODES = _EOL_MODES


class SourceFormError(RuntimeError):
    """声明了形式、但字节不符合这个声明 —— 任务侧的错，重试无用。"""

    #: A wrong declaration is not a transient fault and not the model's answer:
    #: re-asking changes nothing, the request itself has to change.
    reason = "fix_constraint"


@dataclass(frozen=True)
class SourceForm:
    """One source's declared form. Empty strings mean *not declared*."""

    encoding: str = ""
    eol: str = ""

    def __post_init__(self) -> None:
        if self.encoding and not _known(self.encoding):
            # 未知编码当场炸（任务文件写错），不留到运行时才发现。
            raise ValueError(f"unknown encoding {self.encoding!r}")
        if self.eol not in EOL_MODES:
            raise ValueError(f"unknown eol mode {self.eol!r}; expected one of {EOL_MODES!r}")

    @property
    def declared(self) -> bool:
        return bool(self.encoding or self.eol)

    @property
    def touches_text(self) -> bool:
        """这份声明有没有可能改到交付给块的文本（决定载荷要不要带说明）。"""
        return bool(self.encoding or self.eol in ("lf", "crlf"))


@dataclass(frozen=True)
class DecodedBody:
    text: str
    #: 只有壳真的动了字节、或者只能靠猜时才非空 —— 空 = 原样交付。
    note: Mapping[str, Any]


def _known(encoding: str) -> bool:
    try:
        codecs.lookup(encoding)
    except LookupError:
        return False
    return True

#: ★ 默认实例在**拥有者包内**构造：AGENTS §1.4 的装配点守卫禁止别的包 new 边界实现
#: （`services/` 里写 `SourceForm()` 会被守卫抓住 —— 这是守卫在正常工作）。
DEFAULT_FORM = SourceForm()


def form_for(forms: Mapping[str, SourceForm], ref: str) -> SourceForm:
    return forms.get(ref, DEFAULT_FORM)


def forms_for(sources: Iterable[Any]) -> dict[str, SourceForm]:
    """``SourceRef`` 序列 → ``{ref: SourceForm}``（任务声明的那张清单）。"""
    return {
        str(source.ref): SourceForm(
            encoding=str(source.encoding or ""),
            eol=str(source.eol or ""),
        )
        for source in sources
    }


def decode_body(body: bytes, form: SourceForm | None = None) -> DecodedBody:
    """把冻结的字节解成文本，并把"我做了什么 / 假设了什么"一起交出去。"""
    form = form or DEFAULT_FORM
    if form.encoding:
        try:
            text = body.decode(form.encoding)
        except UnicodeDecodeError as exc:
            raise SourceFormError(
                f"the source is declared as {form.encoding!r} but the bytes are not valid "
                f"{form.encoding!r} (byte {exc.start}: {body[exc.start:exc.start + 4]!r})"
            ) from exc
        note: dict[str, Any] = {
            "encoding": form.encoding,
            "assumed": False,
            "replacement_chars": 0,
        }
    else:
        text = body.decode(DEFAULT_ENCODING, errors="replace")
        replaced = text.count("\ufffd")
        if replaced == 0 and not form.eol:
            # 干净 + 没声明：跟修前逐字一样，不带说明。
            return DecodedBody(text=text, note={})
        note = {
            "encoding": DEFAULT_ENCODING,
            "assumed": True,
            "replacement_chars": replaced,
        }

    if form.eol in ("lf", "crlf"):
        wanted = "\n" if form.eol == "lf" else "\r\n"
        unified = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = unified.replace("\n", wanted)
        note["eol"] = form.eol
        note["eol_normalized"] = normalized != text
        text = normalized
    elif form.eol == "preserve" or not form.eol:
        note.setdefault("eol", form.eol or "preserve")

    return DecodedBody(text=text, note=note)
