"""`files.patch` 的【多条 edits】形状：原文匹配 / 唯一 / 不重叠 / 原子 / 具名报错。

背景（实测，不是设计洁癖）：4B 模型在 13f 的 T-01 上 33~41 次 `code.run`，
每次都把整段脚本推倒重写（相邻轮代码重合度 3%~15%），错误类型一路在换
—— 它**从未消灭过任何一个错**。而 `files.patch` 一直在工具菜单里（两次实测的
`tools` 数组 43/43、55/55 都含它），**却被调用 0 次**：因为它是【单条】
`old_string`/`new_string`，描述却说"改几处文字" —— 说得到、做不到。

本文件考的是补齐后的形状（语义照 flash `edit.py`）：

```
① 原文匹配，不用行号     ② 每条 oldText 恰好出现一次   ③ 多条互不重叠
④ 原子：任一条失败 ⇒ 一个字节都不落盘                 ⑤ 失败具名：哪条 / 期望 / 附近实际
```
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.core.model import ModelResponse
from bobanana6.services.workspace import (
    BoundWorkspace,
    WorkspaceError,
    WorkspaceLedger,
    WriteOutsideRegion,
)
from tests.test_convert_three import (
    _block_of,
    _briefing,
    _task,
    _tool_call,
)
from tests.test_services import _full_toolset


def _workspace(tmp_path: Path, **kw) -> BoundWorkspace:  # type: ignore[no-untyped-def]
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    ledger = WorkspaceLedger(root=root, region=kw.get("write_region", ()))
    return BoundWorkspace(root=root, ledger=ledger, **kw)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# ① 成功路径：一次多条，各对原文件匹配
# --------------------------------------------------------------------------- #


def test_many_edits_are_applied_in_one_call(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, write_region=("out.py",), frozen_paths=())
    (tmp_path / "ws" / "out.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    result = ws.patch_edits(
        "out.py",
        [
            {"oldText": "a = 1", "newText": "a = 10"},
            {"oldText": "c = 3", "newText": "c = 30"},
        ],
    )
    assert (tmp_path / "ws" / "out.py").read_text(encoding="utf-8") == "a = 10\nb = 2\nc = 30\n"
    assert result["replacements"] == 2 and result["path"] == "out.py"


def test_an_empty_new_text_deletes_that_span(tmp_path: Path) -> None:
    """★ `newText: ""` 是**显式**删除 —— 与"没给 newText"（下面那条会拒）不是一回事。"""
    ws = _workspace(tmp_path, write_region=("out.txt",), frozen_paths=())
    (tmp_path / "ws" / "out.txt").write_text("keep\ndrop\nkeep2\n", encoding="utf-8")
    ws.patch_edits("out.txt", [{"oldText": "drop\n", "newText": ""}])
    assert (tmp_path / "ws" / "out.txt").read_text(encoding="utf-8") == "keep\nkeep2\n"


def test_edits_may_be_a_json_string_because_endpoints_are_lenient(tmp_path: Path) -> None:
    """宽松端点 / 模型偶发把数组写成一段 JSON 文本时，照样受理（不静默丢弃）。"""
    ws = _workspace(tmp_path, write_region=("out.txt",), frozen_paths=())
    (tmp_path / "ws" / "out.txt").write_text("x\n", encoding="utf-8")
    ws.patch_edits("out.txt", json.dumps([{"oldText": "x", "newText": "y"}]))
    assert (tmp_path / "ws" / "out.txt").read_text(encoding="utf-8") == "y\n"


# --------------------------------------------------------------------------- #
# ① fail-closed：找不到 / 不唯一 / 重叠 —— 三条都拒，且报文说清
# --------------------------------------------------------------------------- #


def test_a_missing_old_text_is_refused_by_name_and_shows_what_is_there(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, write_region=("out.py",), frozen_paths=())
    (tmp_path / "ws" / "out.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    try:
        ws.patch_edits("out.py", [{"oldText": "gamma = 3", "newText": "gamma = 30"}])
    except WorkspaceError as exc:
        message = str(exc)
    else:  # pragma: no cover
        raise AssertionError("a missing oldText was accepted")
    # ★ 具名：哪一条 / 期望什么 / 文件里附近实际是什么（不是一句 "not found"）
    assert "edits[0].oldText was not found" in message
    assert "expected" in message and "got" in message
    assert "alpha = 1" in message  # 附近实际内容（`_nearby` 退回文件开头）
    assert (tmp_path / "ws" / "out.py").read_text(encoding="utf-8") == "alpha = 1\nbeta = 2\n"


def test_a_non_unique_old_text_is_refused_with_the_match_count(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, write_region=("out.py",), frozen_paths=())
    (tmp_path / "ws" / "out.py").write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")
    try:
        ws.patch_edits("out.py", [{"oldText": "x = 1", "newText": "x = 9"}])
    except WorkspaceError as exc:
        message = str(exc)
    else:  # pragma: no cover
        raise AssertionError("a non-unique oldText was accepted")
    assert "appears 2 times" in message and "not unique" in message
    assert "include the line above/below" in message  # 可照做的修复建议
    assert (tmp_path / "ws" / "out.py").read_text(encoding="utf-8") == "x = 1\ny = 2\nx = 1\n"


def test_overlapping_edits_are_refused_and_name_both_indices(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, write_region=("out.py",), frozen_paths=())
    (tmp_path / "ws" / "out.py").write_text("abcdef\n", encoding="utf-8")
    try:
        ws.patch_edits(
            "out.py",
            [
                {"oldText": "abcd", "newText": "ABCD"},
                {"oldText": "cdef", "newText": "CDEF"},  # 与上一条在偏移 2 处相交
            ],
        )
    except WorkspaceError as exc:
        message = str(exc)
    else:  # pragma: no cover
        raise AssertionError("overlapping edits were accepted")
    assert "edits[0] and edits[1] match overlapping spans" in message
    assert "merge them into one edit" in message
    assert (tmp_path / "ws" / "out.py").read_text(encoding="utf-8") == "abcdef\n"


def test_two_edits_hitting_the_same_span_are_refused_as_overlap(tmp_path: Path) -> None:
    """同一条 oldText 写两遍 ⇒ 两个 span 完全重合 ⇒ 拒（不猜、不合并）。"""
    ws = _workspace(tmp_path, write_region=("out.py",), frozen_paths=())
    (tmp_path / "ws" / "out.py").write_text("only_one\n", encoding="utf-8")
    try:
        ws.patch_edits(
            "out.py",
            [{"oldText": "only_one", "newText": "a"}, {"oldText": "only_one", "newText": "b"}],
        )
    except WorkspaceError as exc:
        assert "overlapping spans" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a duplicated span was accepted")


def test_missing_new_text_is_refused_not_treated_as_a_deletion(tmp_path: Path) -> None:
    """★ 只读审计 #41 的同族：缺字段**不许**默认成空串（那会静默删内容还回 ok=True）。"""
    ws = _workspace(tmp_path, write_region=("out.py",), frozen_paths=())
    (tmp_path / "ws" / "out.py").write_text("hello world\n", encoding="utf-8")
    try:
        ws.patch_edits("out.py", [{"oldText": "world"}])
    except WorkspaceError as exc:
        assert "edits[0].newText must be text" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a missing newText was treated as a deletion")
    assert (tmp_path / "ws" / "out.py").read_text(encoding="utf-8") == "hello world\n"


def test_empty_or_absent_edits_are_refused(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, write_region=("out.py",), frozen_paths=())
    (tmp_path / "ws" / "out.py").write_text("x\n", encoding="utf-8")
    for bad in ([], "not json at all", [{"oldText": "", "newText": "y"}]):
        try:
            ws.patch_edits("out.py", bad)
        except WorkspaceError:
            continue
        raise AssertionError(f"accepted bad edits: {bad!r}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# ② 原子性：第 2 条失败 ⇒ 文件一个字节都没变（贴前后 sha256）
# --------------------------------------------------------------------------- #


def test_a_late_failure_leaves_the_file_byte_identical(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, write_region=("out.py",), frozen_paths=())
    target = tmp_path / "ws" / "out.py"
    target.write_text("one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8")
    before = _sha(target)
    try:
        ws.patch_edits(
            "out.py",
            [
                {"oldText": "one = 1", "newText": "one = 10"},  # 第 1 条合法
                {"oldText": "three = 3", "newText": "three = 30"},
                {"oldText": "NOPE", "newText": "x"},  # 第 3 条找不到 ⇒ 整调用拒
            ],
        )
    except WorkspaceError as exc:
        assert "edits[2].oldText was not found" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a batch with a bad edit was accepted")
    assert _sha(target) == before, "a refused call must not touch a single byte"
    assert target.read_text(encoding="utf-8") == "one = 1\ntwo = 2\nthree = 3\n"


def test_overlap_is_detected_before_any_byte_is_written(tmp_path: Path) -> None:
    """★ 重叠是在写盘**之前**判的（内存里算完所有 span 才动盘）。"""
    ws = _workspace(tmp_path, write_region=("out.py",), frozen_paths=())
    target = tmp_path / "ws" / "out.py"
    target.write_text("abcdef\n", encoding="utf-8")
    before = _sha(target)
    try:
        ws.patch_edits("out.py", [{"oldText": "abc", "newText": "A"}, {"oldText": "bcd", "newText": "B"}])
    except WorkspaceError:
        pass
    assert _sha(target) == before


# --------------------------------------------------------------------------- #
# ③ 写区：和 `files.write` 同一条判据
# --------------------------------------------------------------------------- #


def test_editing_outside_the_write_region_is_refused_by_name(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, write_region=("mine.csv",), frozen_paths=("theirs.csv",))
    (tmp_path / "ws" / "theirs.csv").write_text("x\n", encoding="utf-8")
    try:
        ws.patch_edits("theirs.csv", [{"oldText": "x", "newText": "y"}])
    except WriteOutsideRegion as exc:
        assert "theirs.csv" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("editing a frozen path was accepted")
    assert (tmp_path / "ws" / "theirs.csv").read_text(encoding="utf-8") == "x\n"


def test_without_a_declared_frozen_set_only_the_region_is_writable(tmp_path: Path) -> None:
    """`frozen_paths=None` ⇒ 退回旧规则（fail-closed）：不在写区就拒。"""
    ws = _workspace(tmp_path, write_region=("mine.csv",), frozen_paths=None)
    (tmp_path / "ws" / "other.csv").write_text("x\n", encoding="utf-8")
    try:
        ws.patch_edits("other.csv", [{"oldText": "x", "newText": "y"}])
    except WriteOutsideRegion:
        pass
    else:  # pragma: no cover
        raise AssertionError("a path outside the region was accepted with no frozen set")


# --------------------------------------------------------------------------- #
# ④ 溯源/账本：改了哪个文件要进 ledger（通道 2 同一条路）
# --------------------------------------------------------------------------- #


def test_the_ledger_sees_the_file_that_was_patched(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    ledger = WorkspaceLedger(root=root, region=("out.csv",))
    ws = BoundWorkspace(root=root, write_region=("out.csv",), frozen_paths=(), ledger=ledger)
    ws.write("out.csv", "obligor\nA-1\nB-2\n")
    assert ledger.used is True
    ws.patch_edits("out.csv", [{"oldText": "B-2", "newText": "C-3"}])
    changed = ledger.changed()
    assert "out.csv" in changed, changed
    assert (root / "out.csv").read_text(encoding="utf-8") == "obligor\nA-1\nC-3\n"


def test_the_judge_still_sees_the_product_after_it_was_patched(tmp_path: Path) -> None:
    """★ 端到端：块先写、再定点改，判定器与下游读到的都是**改完的那份**。"""
    task = _task(tmp_path, name="patched-deliverable")
    turns: dict[str, int] = {}
    downstream: dict = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages)
            if block == "T-02":
                downstream.setdefault("entry", (_briefing(messages).get("upstream") or {}).get("a.csv") or {})
            turn = turns.get(block, 0)
            turns[block] = turn + 1
            if block == "T-01":
                if turn == 0:
                    return ModelResponse(tool_calls=(_tool_call("r1", "sources.snapshot", {"ref": "seed"}),))
                if turn == 1:
                    return ModelResponse(
                        tool_calls=(
                            _tool_call("w1", "files.write", {"path": "a.csv", "content": "obligor\nA-1\nB-2\n"}),
                        )
                    )
                if turn == 2:
                    return ModelResponse(
                        tool_calls=(
                            _tool_call(
                                "p1",
                                "files.patch",
                                {"path": "a.csv", "edits": [{"oldText": "B-2", "newText": "C-3"}]},
                            ),
                        )
                    )
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}],
                        "rows": [{"obligor": "A-1"}, {"obligor": "C-3"}],
                        "done": True,
                    }
                )
            )

    run = run_task(task, client=_Client(), env={}, workspace=True)
    assert run.verdicts["T-01"].ok is True, run.verdicts["T-01"].evidence
    assert run.workspace is not None
    on_disk = (run.workspace / "a.csv").read_text(encoding="utf-8")
    assert on_disk == "obligor\nA-1\nC-3\n", "the patched bytes are what the shell sees"
    assert downstream.get("entry"), "T-02 never got the hand-off"


# --------------------------------------------------------------------------- #
# ⑤ 载体保真：CRLF 不许被整份翻成 LF（否则"定点"名不副实）
# --------------------------------------------------------------------------- #


def test_a_crlf_file_keeps_its_line_endings(tmp_path: Path) -> None:
    """★ 只改一行 ⇒ 文件里其它字节一个都不动（读时 `newline=''` 的等价做法）。"""
    ws = _workspace(tmp_path, write_region=("out.txt",), frozen_paths=())
    target = tmp_path / "ws" / "out.txt"
    target.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    ws.patch_edits("out.txt", [{"oldText": "line2", "newText": "LINE2"}])
    assert target.read_bytes() == b"line1\r\nLINE2\r\nline3\r\n"


def test_a_non_utf8_file_is_refused_by_name(tmp_path: Path) -> None:
    """★ 非 UTF-8 补成具名 `WorkspaceError`（原来会冒成无 blame 的 UnicodeDecodeError）。"""
    ws = _workspace(tmp_path, write_region=("out.bin",), frozen_paths=())
    (tmp_path / "ws" / "out.bin").write_bytes(b"\xff\xfe\x00abc")
    try:
        ws.patch_edits("out.bin", [{"oldText": "a", "newText": "b"}])
    except WorkspaceError as exc:
        assert "not valid UTF-8" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a non-UTF-8 file was patched")


# --------------------------------------------------------------------------- #
# ⑥ 接线：注册表分派 + 广告出来的 schema
# --------------------------------------------------------------------------- #


def test_the_registry_dispatches_edits_and_still_accepts_the_old_shape(tmp_path: Path) -> None:
    tools, registry = _full_toolset(tmp_path)
    del tools
    root = tmp_path / "ws"
    (root / "out.csv").write_text("x\ny\n", encoding="utf-8")

    ids = _call(registry, "files.patch", {"path": "out.csv", "edits": [{"oldText": "y", "newText": "z"}]})
    assert ids.ok is True, ids.content
    assert (root / "out.csv").read_text(encoding="utf-8") == "x\nz\n"

    legacy = _call(registry, "files.patch", {"path": "out.csv", "old_string": "z", "new_string": "w"})
    assert legacy.ok is True, legacy.content
    assert (root / "out.csv").read_text(encoding="utf-8") == "x\nw\n"


def test_the_registry_refuses_the_old_shape_when_a_key_is_missing(tmp_path: Path) -> None:
    """★ 只读审计 #41：缺 `new_string` 以前会**静默删掉 old_string** 并回 ok=True。"""
    _tools, registry = _full_toolset(tmp_path)
    root = tmp_path / "ws"
    (root / "out.csv").write_text("hello world\n", encoding="utf-8")
    result = _call(registry, "files.patch", {"path": "out.csv", "old_string": "world"})
    assert result.ok is False, "a missing new_string must not be a silent deletion"
    assert "new_string" in result.content
    assert (root / "out.csv").read_text(encoding="utf-8") == "hello world\n"


def test_the_advertised_schema_shapes_edits_as_an_array(tmp_path: Path) -> None:
    tools, _registry = _full_toolset(tmp_path)
    entries = {entry["function"]["name"]: entry["function"] for entry in _tool_schemas(tools)}
    entry = entries["files.patch"]
    parameters = entry["parameters"]
    assert parameters["required"] == ["path"]
    assert parameters["properties"]["edits"]["type"] == "array"
    item = parameters["properties"]["edits"]["items"]
    assert item["required"] == ["oldText", "newText"]
    # ★ 三件套：何时用 / 输入 / 何时不用（这一条最常被省，也最贵）
    assert "何时用" in entry["description"] and "何时不用" in entry["description"]


def _call(registry, name, arguments):  # type: ignore[no-untyped-def]
    raw = {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
    call = registry.calls([raw])[0]
    return registry.execute((call,))[0]


def _tool_schemas(tools):  # type: ignore[no-untyped-def]
    from bobanana6.assemble_impl.produce import tool_schemas

    return tool_schemas(tools)
