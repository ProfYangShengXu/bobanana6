"""★ 产物读盘的反证测试：坏产物绝不许把异常抛出解析边界。

背景（13f 跑测，qwen4b10，实测整进程崩溃）：

    模型写了一个内容恰好是两个双引号 ``""`` 的产物文件
    ``workspace/output/effective_holdings.csv``。``artifact_from_disk`` 把它的表头解析成
    空列名，拿它去构造 ``Column(name="")``，被 ``core/model.py`` 的
    ``Column.__post_init__`` 拒收（``ValueError: Column.name must not be empty``）。
    异常冒泡到顶层 ⇒ 整个 run 死掉，后面 6 个块全没跑、``summary.json`` 也没生成。

    官方硬约束：**「a wrong answer is scored, an exception is not」** ——
    一个坏产物绝不该让整跑抛异常。

本文件把 23 种坏产物锁进测试，分两半：

  · :data:`BLAST_CASES`（9 例）：改动前【全部】抛异常，改动后一个都不许抛。
    - 病根 ①：空列名（8 例）⇒ 退回只填 ``text``（不许过滤空列名 —— 那会让数据列错位）。
    - 病根 ②：超长单行（1 例）⇒ 抬高 ``csv.field_size_limit`` 后能正常解析。
  · :data:`HANDLED_CASES`（18 例）：改动前就兜住的形态，改动后行为必须**逐字一致**
    （照旧兜住，列数/行数不变）。

★★ 反证：把本文件的 ``BLAST_CASES`` 对着**改动前**的 ``artifact_from_disk`` 跑，
必须 9/9 抛异常（读数见 ``产物读盘兜底-20261010.md``）。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from bobanana6.assemble_impl.produce import artifact_from_disk

#: 用户实测 23 例里「会炸的 9 例」。
#:
#: 每项 = (用例名, 产物名, 文件原文, 改动后的期望形态)：
#:   · ``"fallback"`` ⇒ 退回只填 ``text``（``columns`` 为空）。
#:   · ``"parsed"``   ⇒ 正常解析成行列（``columns`` 非空）。
BLAST_CASES: list[tuple[str, str, str, str]] = [
    # 病根 ①：空列名 —— 8 例同一个根因。
    ("empty_string", "x.csv", '""', "fallback"),
    ("spaces_only", "x.csv", "   ", "fallback"),
    ("quoted_empty_nl", "x.csv", '""\n', "fallback"),
    ("empty_header_mid", "x.csv", "a,,b\n1,2,3\n", "fallback"),
    ("empty_header_all", "x.csv", ",,,\n1,2,3,4\n", "fallback"),
    ("commas_only", "x.csv", ",,,", "fallback"),
    ("many_empty_cols", "x.csv", "," * 2000 + "\n1", "fallback"),
    ("blank_header_cells", "x.csv", " , \n1,2\n", "fallback"),
    # 病根 ②：超长单行 —— csv 模块默认 field_size_limit=131072。
    ("huge_field", "x.csv", "a,b\n" + "x" * 500_000, "parsed"),
]

#: 第 2 节「已兜住的 18 例」：改动前就兜住，改动后行为必须一致。
#: 每项 = (用例名, 产物名, 文件原文, 期望列数, 期望行数)。
HANDLED_CASES: list[tuple[str, str, str, int, int]] = [
    ("empty_file", "x.csv", "", 0, 0),
    ("newline_only", "x.csv", "\n", 0, 0),
    ("dup_header", "x.csv", "a,a\n1,2\n", 2, 1),
    ("header_padded", "x.csv", " a , b \n1,2\n", 2, 1),
    ("unclosed_quote", "x.csv", 'a,b\n"unclosed\n', 2, 1),
    ("bom_only", "x.csv", "\ufeff", 1, 0),
    ("binary_garbage", "x.csv", "\x01\x02\x03\xff\xfe\n", 1, 0),
    ("crlf_mixed", "x.csv", "a,b\r\n1,2\n3,4\r\n", 2, 2),
    ("tab_as_sep_csv", "x.csv", "a\tb\n1\t2\n", 1, 1),
    ("comma_in_tsv", "x.tsv", "a,b\n1,2\n", 1, 1),
    ("row_longer", "x.csv", "a,b\n1,2,3\n", 2, 1),
    ("row_shorter", "x.csv", "a,b\n1\n", 2, 1),
    ("header_only", "x.csv", "a,b\n", 2, 0),
    ("unknown_suffix", "x.txt", "a,b\n1,2\n", 0, 0),
    ("binary_nul", "x.csv", "a,b\n1,\x002\n", 2, 1),
    ("single_column", "x.csv", "a\n1\n2\n", 1, 2),
    ("bom_header", "x.csv", "\ufeffa,b\n1,2\n", 2, 1),
    ("quoted_comma", "x.csv", 'a,b\n"x,y",2\n', 2, 1),
]


@pytest.mark.parametrize(
    ("case", "filename", "content", "expect"),
    BLAST_CASES,
    ids=[case for case, _f, _c, _e in BLAST_CASES],
)
def test_a_bad_artifact_never_raises(
    case: str, filename: str, content: str, expect: str, tmp_path: Path
) -> None:
    """★ 那 9 个坏产物，一个都不许抛；退回路径 ``text`` 原文，或正常解析出列。"""
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")

    artifact = artifact_from_disk("x.csv", path)  # ← 改动前：这里抛 ValueError / csv.Error

    assert artifact.text == content  # 内容一字不改地留着（不截断、不造假）
    if expect == "fallback":
        assert artifact.columns == (), f"{case}: 空列名必须退回只填 text"
        assert artifact.rows == ()
    else:
        assert artifact.columns, f"{case}: 超长字段应已被解析成列"
        assert len(artifact.rows) == 1


@pytest.mark.parametrize(
    ("case", "filename", "content", "columns", "rows"),
    HANDLED_CASES,
    ids=[case for case, _f, _c, _n, _r in HANDLED_CASES],
)
def test_already_handled_shapes_stay_handled(
    case: str, filename: str, content: str, columns: int, rows: int, tmp_path: Path
) -> None:
    """★ 18 个对照：兜住的照旧兜住，且列数/行数与改动前逐字一致。"""
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")

    artifact = artifact_from_disk("x.csv", path)

    assert len(artifact.columns) == columns, f"{case}: 列数变了"
    assert len(artifact.rows) == rows, f"{case}: 行数变了"
    # 读盘走 `Path.read_text`（通用换行会归一 CRLF），断言的是"与这条真实读数一致、未截断"。
    assert artifact.text == path.read_text(encoding="utf-8")


def test_empty_column_names_are_not_silently_dropped() -> None:
    """★ 不许用「过滤掉空列名」的修法：那会让数据列整体错位（静默错数据）。

    具体锁：``a,,b`` 的三列里，第 3 列 ``b`` 的值必须仍然对得上 ``b``。
    退回 ``text`` 路径没有任何列，所以这个错位【不可能】发生。
    """
    from bobanana6.assemble_impl.produce import _artifact_from_disk
    from bobanana6.core.model import Artifact

    artifact = _artifact_from_disk(Artifact(name="x.csv"), Path("x.csv"), "a,,b\n1,2,3\n")

    assert artifact.columns == ()
    assert artifact.rows == ()
    assert artifact.text == "a,,b\n1,2,3\n"


def test_the_csv_field_limit_is_raised_and_declared() -> None:
    """★ 上限是**被声明的命名常量**（不是魔数），且模块加载时真的抬高了它。"""
    # ★ 在函数里 import：反证时对改动前的代码跑，模块仍能正常收集，只让这一条红。
    from bobanana6.assemble_impl.produce import MAX_CSV_FIELD_CHARS

    assert MAX_CSV_FIELD_CHARS > 128 * 1024, "必须高于 csv 的默认护栏 131072"
    assert MAX_CSV_FIELD_CHARS < 2**31 - 1, "不许拉满到 ≈2GiB —— 那等于拆掉 OOM 护栏"
    assert csv.field_size_limit() == MAX_CSV_FIELD_CHARS
