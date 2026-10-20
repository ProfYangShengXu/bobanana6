"""★ 缺口③：子进程输出编码兜底（不崩线程、不静默丢字节）。

改前 `shell.run` 的 `subprocess.Popen(... text=True)` 等于「locale 编码 + strict」：
子进程吐 GBK 字节（中文 Windows 的 `cmd /c echo 中文`，首字节 `0xd5`）时
POSIX 上 `communicate()` 直接抛 `UnicodeDecodeError`、整份输出丢；Windows 上错只打在
`subprocess.py:1599 _readerthread` 子线程，线程死掉而工具仍报 `exit 0`。

这里的反证是确定性的、不依赖平台：
`decode_process_output("中文".encode("gbk")) == "中文"` —— 即使当前 locale 是 UTF-8
（本机就是），GBK 也在回退链里；全失败才 `errors="replace"`，并把替换计数写进结果。
"""

from __future__ import annotations

import sys
from pathlib import Path

from bobanana6.services.proc_text import (
    DEFAULT_FALLBACK_ENCODINGS,
    candidate_encodings,
    decode_process_output,
    text_mode_kwargs,
)
from bobanana6.services.workspace import BoundWorkspace


def test_gbk_bytes_come_back_as_chinese() -> None:
    """★ 核心反证：locale 是 UTF-8 也要能解 GBK。"""
    raw = "中文输出".encode("gbk")
    decoded = decode_process_output(raw)
    assert decoded.text == "中文输出"
    assert decoded.replacements == 0  # 干净解码，不加噪声键


def test_utf8_still_decodes_as_utf8() -> None:
    decoded = decode_process_output("中文".encode())
    assert decoded.text == "中文"
    assert decoded.replacements == 0


def test_undecodable_bytes_are_replaced_and_counted_not_raised() -> None:
    """全失败时必须 `errors="replace"`，且替换事实**可观测**（不许静默）。"""
    decoded = decode_process_output(b"\xff\xfe\xff\xfe")
    assert "\ufffd" in decoded.text
    assert decoded.replacements > 0
    assert "U+FFFD" in decoded.note


def test_empty_output_is_empty() -> None:
    assert decode_process_output(None).text == ""
    assert decode_process_output(None).replacements == 0
    assert decode_process_output(b"").text == ""


def test_the_platform_encoding_is_first_and_never_assumed_utf8() -> None:
    chain = candidate_encodings()
    assert chain
    assert "utf-8" in {item.lower() for item in chain}
    assert "gbk" in {item.lower() for item in chain}
    # 回退链尾部固定；平台编码在最前（Windows 中文 = cp936/GBK）
    assert tuple(item.lower() for item in chain[-2:]) == tuple(
        item.lower() for item in DEFAULT_FALLBACK_ENCODINGS
    )


def test_text_mode_kwargs_always_replace() -> None:
    """所有 `text=True` 调用点必须同时给 encoding + errors。"""
    kwargs = text_mode_kwargs()
    assert kwargs["text"] is True
    assert kwargs["encoding"] == candidate_encodings()[0]
    assert kwargs["errors"] == "replace"


def _run_script(workspace: BoundWorkspace, body: bytes) -> dict:
    """Run a child that writes exactly ``body`` to stdout (no shell quoting games)."""
    script = workspace.root / "emit.py" if workspace.root is not None else Path("emit.py")
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import sys\n"
        f"sys.stdout.buffer.write({body!r})\n"
        "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    return dict(workspace.run(f'"{sys.executable}" emit.py'))


def test_shell_run_returns_gbk_content_without_a_crash(tmp_path: Path) -> None:
    workspace = BoundWorkspace(root=tmp_path / "ws", write_region=(), frozen_paths=())
    result = _run_script(workspace, "中文".encode("gbk"))
    assert result["exit_code"] == 0
    assert result["stdout"] == "中文"
    # 干净回退 = 不虚报替换
    assert "decode_replacements" not in result


def test_shell_run_records_replacement_in_its_result(tmp_path: Path) -> None:
    """编解码失败要能在 trace 里看到：结果里带 `decode_replacements` + 说明。"""
    workspace = BoundWorkspace(root=tmp_path / "ws", write_region=(), frozen_paths=())
    result = _run_script(workspace, b"\xff\xfe\xff\xfe")
    assert result["exit_code"] == 0
    assert result["decode_replacements"] > 0
    assert "U+FFFD" in result["decode_note"]
