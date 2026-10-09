"""两种**形状**：题面按契约产物切节 + 来源表的结构画像。

这一组测的都是"壳算出来的形状"，判据只用**表头/行列**与**契约声明的名字**，
所以换任何题都成立。★ 本文件里**不出现任何一个具体任务的名字**（见最后一条测试：
它自己扫自己）—— fixtures 全是合成的，规模/列名/产物名都换过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl.artifact_shape import (
    artifact_sections,
    clip_section,
    source_shapes,
    table_shape,
)
from bobanana6.core.model import ModelResponse

STATIC_MARKER = (
    "(identical for every block in this task; it is deliberately NOT\nrepeated in the per-block brief below):\n"
)


def _static(system: str) -> dict:
    assert STATIC_MARKER in system, system[:200]
    return json.loads(system.split(STATIC_MARKER, 1)[1].split("\n", 1)[0])


# --------------------------------------------------------------------------- #
# ① 题面按契约声明的产物切节
# --------------------------------------------------------------------------- #


def test_a_section_is_attached_to_the_declared_name_that_heads_it() -> None:
    document = (
        "# Title\n\nintro\n\n"
        "## Required outputs\n\n"
        "### `first.csv`\nfirst body\n\n"
        "### `second.csv`\nsecond body\n\n"
        "### `third.csv`\nthird body\n"
    )
    sections = artifact_sections(["out/first.csv", "out/second.csv", "out/third.csv"], document)
    assert sorted(sections) == ["out/first.csv", "out/second.csv", "out/third.csv"]
    assert sections["out/second.csv"]["text"] == "### `second.csv`\nsecond body"
    assert sections["out/third.csv"]["heading"] == "### `third.csv`"


def test_a_section_is_a_verbatim_slice_of_the_document() -> None:
    document = "# T\n\n### `x.csv`\nalpha\n- `a`\n- `b`\n\n### `y.csv`\nbeta\n"
    sections = artifact_sections(["x.csv", "y.csv"], document)
    assert sections["x.csv"]["text"] in document
    assert sections["y.csv"]["text"] in document


def test_a_subheading_does_not_close_the_section() -> None:
    document = "### `x.csv`\nbody\n\n#### columns\n- `a`\n\n### `y.csv`\nother\n"
    sections = artifact_sections(["x.csv", "y.csv"], document)
    assert "#### columns" in sections["x.csv"]["text"]
    assert "other" not in sections["x.csv"]["text"]


def test_a_name_that_heads_nothing_is_left_out() -> None:
    document = "### `present.csv`\nbody\n"
    assert artifact_sections(["present.csv", "absent.csv"], document) == {"present.csv": {"heading": "### `present.csv`", "text": "### `present.csv`\nbody"}}


def test_nothing_is_attached_without_names_or_headings() -> None:
    assert artifact_sections([], "### `x.csv`\nbody\n") == {}
    assert artifact_sections(["x.csv"], "") == {}
    assert artifact_sections(["x.csv"], "just prose, no headings at all\n") == {}


def test_a_hash_without_a_space_is_not_a_heading() -> None:
    assert artifact_sections(["x.csv"], "#x.csv\nbody\n") == {}


def test_a_clip_is_visible() -> None:
    assert clip_section("short", 100) == "short"
    clipped = clip_section("x" * 50, 10)
    assert clipped.startswith("x" * 10) and clipped.endswith("…[truncated]")


# --------------------------------------------------------------------------- #
# ② 来源表的结构画像
# --------------------------------------------------------------------------- #


def test_a_table_is_read_as_rows_columns_and_groups() -> None:
    text = "a,b\np,1\np,2\nq,1\nq,1\n"
    shape = table_shape(text)
    assert shape is not None
    assert shape["rows"] == 4 and shape["columns"] == 2
    assert shape["column_names"] == ["a", "b"]
    assert shape["repeated_columns"][0]["name"] == "b"
    grouped = {(tuple(item["on"]), item["largest_groups"][0]) for item in shape["grouped_columns"]}
    assert (("a", "b"), 2) in grouped


def test_prose_that_merely_contains_commas_is_not_a_table() -> None:
    """★ fail-closed 的关键一条：散文里也有逗号，宽松判据会把一段散文算成表。"""
    prose = (
        "# Report, with a comma\n\n"
        "This sentence has, commas, in it.\n"
        "Another line, with more.\n"
        "And one, more, line, here.\n"
    )
    assert table_shape(prose) is None


def test_ragged_rows_are_not_a_table() -> None:
    assert table_shape("a,b\n1,2\n3\n") is None


def test_a_single_column_is_not_a_table() -> None:
    assert table_shape("only\n1\n2\n") is None


def test_a_table_needs_a_data_row() -> None:
    assert table_shape("a,b\n") is None


def test_the_shape_never_carries_a_cell_value() -> None:
    """★ 只陈述形状：除了列名，一个格子的值都不许出现在画像里。"""
    text = "k,v\nzzz,qqq\nzzz,qqq\nwww,eee\n"
    rendered = json.dumps(table_shape(text), ensure_ascii=False)
    for value in ("zzz", "qqq", "www", "eee"):
        assert value not in rendered


def test_a_column_that_never_repeats_is_not_a_grouping_key() -> None:
    text = "id,kind\n1,one\n2,one\n3,two\n4,two\n"
    shape = table_shape(text)
    assert shape is not None
    names = {item["name"] for item in shape["repeated_columns"]}
    assert "kind" in names and "id" not in names
    assert all("id" not in item["on"] for item in shape.get("grouped_columns", []))


def test_a_constant_column_is_not_a_grouping_key() -> None:
    """★ 只有一个取值的列不折叠任何东西 —— 拿它分组只会把整张表折成一组，不是形状。"""
    text = "a,b,flat\n1,x,same\n2,x,same\n3,y,same\n"
    shape = table_shape(text)
    assert shape is not None
    assert all("flat" not in item["on"] for item in shape.get("grouped_columns", []))
    assert "flat" not in {item["name"] for item in shape.get("repeated_columns", [])}


def test_a_source_that_cannot_be_read_is_skipped() -> None:
    assert source_shapes({"unreadable": ""}, 5000, 400) == {}
    assert source_shapes({"prose": "no table here\n"}, 5000, 400) == {}


def test_the_whole_section_obeys_the_budget_and_says_what_it_dropped() -> None:
    header = ",".join(f"c{index:02d}" for index in range(30))
    rows = [",".join(str((index + step) % 4) for index in range(30)) for step in range(40)]
    text = header + "\n" + "\n".join(rows) + "\n"
    full = json.dumps(table_shape(text), ensure_ascii=False)
    small = source_shapes({"t": text}, 400, 300)
    rendered = json.dumps(small, ensure_ascii=False)
    assert len(rendered) < len(full)
    assert "omitted" in small["t"]
    assert small["t"]["rows"] == 40 and small["t"]["columns"] == 30


# --------------------------------------------------------------------------- #
# 接线：真跑一遍，看 system / brief 里到底多了什么
# --------------------------------------------------------------------------- #


def _shaped_task(
    tmp_path: Path, *, name: str, headed: str = "a.csv", deliverables: tuple[str, ...] = ("a.csv", "b.csv")
) -> Path:
    """一道合成题：题面里有产物标题，来源里有一张会重复的表。

    ``headed`` 决定哪一个产物名在题面里**有标题** —— 另一个恰好用来验 fail-closed。
    """
    task = tmp_path / name
    (task / "input" / "data").mkdir(parents=True, exist_ok=True)
    (task / "input" / "checks").mkdir(parents=True, exist_ok=True)
    (task / "input" / "checks" / "test_outputs.py").write_text("# grader\n", encoding="utf-8")
    (task / "input" / "data" / "counts.tsv").write_text(
        "site,unit,value\nA,1,10\nA,1,11\nA,2,12\nB,1,13\n", encoding="utf-8"
    )
    bodies = {
        "a.csv": "### `a.csv`\nOne line per kept row.\n",
        "b.csv": "### `b.csv`\nOne line per other row.\n",
    }
    (task / "input" / "data" / "notes.md").write_text(
        "# Notes\n\n## Required outputs\n\n" + bodies[headed] + "\n", encoding="utf-8"
    )
    (task / "ontology.yaml").write_text(
        'domain: text\nversion: "2026.1"\nconcepts:\n  site: {label: 站点, kind: entity}\n',
        encoding="utf-8",
    )
    declared = "".join(f"  - {{name: {artifact}, schema: {{site: str}}}}\n" for artifact in deliverables)
    (task / "task.yaml").write_text(
        "task_id: shaped\n"
        "goal: keep the rows\n"
        "version: 1\n"
        "statement: notes\n"
        "deliverables:\n"
        + declared
        + "sources:\n"
        "  - {ref: notes, locator: input/data/notes.md}\n"
        "  - {ref: counts, locator: input/data/counts.tsv}\n"
        "constraints:\n"
        "  hard:\n"
        '    - {id: C-1, kind: presence, target: "*", spec: {min_rows: 1}, rationale: "no empty"}\n',
        encoding="utf-8",
    )
    return task


def _first_prompt(task: Path) -> tuple[str, dict]:
    captured: dict[str, object] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            if not captured:
                captured["system"] = next(
                    (str(m.get("content") or "") for m in messages if m.get("role") == "system"), ""
                )
                captured["brief"] = json.loads(
                    next((str(m.get("content") or "") for m in messages if m.get("role") == "user"), "{}")
                )
            return ModelResponse(text=json.dumps({"declined": True, "reason": "stop after the first prompt"}))

    run_task(task, client=_Client(), env={}, workspace=True, strategy="single")
    return str(captured["system"]), dict(captured["brief"])  # type: ignore[arg-type]


def test_the_block_is_handed_its_own_section_and_the_statement_loses_nothing(tmp_path: Path) -> None:
    task = _shaped_task(tmp_path, name="headed")
    system, brief = _first_prompt(task)
    spec = brief["deliverable_spec"]
    assert spec["artifact"] == "a.csv"
    assert spec["verbatim_excerpt"] is True
    statement = _static(system)["statement"]["text"]
    # ★ 切出来的那一节是题面**逐字**的一段，不是改写
    assert spec["section"] in statement
    # ★ 题面全文一个字没少（原文与盘上那份逐字节相同）
    assert statement == (task / "input" / "data" / "notes.md").read_text(encoding="utf-8")


def test_the_other_artifact_gets_the_other_section(tmp_path: Path) -> None:
    """★ 换一个产物名，切出来的就是另一节 —— 归属按块的 output 现算。"""
    task = _shaped_task(tmp_path, name="headed-other", headed="b.csv", deliverables=("b.csv",))
    _system, brief = _first_prompt(task)
    assert brief["deliverable_spec"]["artifact"] == "b.csv"
    assert "`b.csv`" in brief["deliverable_spec"]["section"]


def test_a_block_whose_artifact_has_no_section_gets_nothing(tmp_path: Path) -> None:
    """★ fail-closed：题面里没有这个产物的标题 ⇒ 一个字都不加。"""
    task = _shaped_task(tmp_path, name="headingless", headed="a.csv")
    # 把交付物换成题面里没有标题的那个名字
    yaml = (task / "task.yaml").read_text(encoding="utf-8").replace("a.csv", "c.csv")
    (task / "task.yaml").write_text(yaml, encoding="utf-8")
    _system, brief = _first_prompt(task)
    assert "deliverable_spec" not in brief


def test_the_table_shape_lands_in_the_constant_prefix_not_in_the_brief(tmp_path: Path) -> None:
    """★ 放置纪律：全 DAG 恒定的 ⇒ 恒定前缀；**不许两边都放**。"""
    task = _shaped_task(tmp_path, name="placed")
    system, brief = _first_prompt(task)
    static = _static(system)
    assert "source_profiles" in static
    assert "source_profiles" not in brief
    profile = static["source_profiles"]["counts"]
    assert profile["rows"] == 4 and profile["columns"] == 3
    assert profile["column_names"] == ["site", "unit", "value"]


def test_a_source_that_is_not_a_table_changes_nothing(tmp_path: Path) -> None:
    """★ 反向样：来源不是表（这里只有一份纯文本题面）⇒ 恒定前缀里一个字都不加。

    `tests/test_convert_three.py::test_the_real_run_moves_the_dag_constant_keys_*`
    就是这条的既有读数：那道题的来源只有 `draft\\n` 一个纯文本，
    所以它的恒定键集合**逐字不变**仍是 `{statement, source_files}`。
    """
    task = _shaped_task(tmp_path, name="prose-only")
    (task / "input" / "data" / "counts.tsv").write_text("just prose, no columns\n", encoding="utf-8")
    system, _brief = _first_prompt(task)
    assert "source_profiles" not in _static(system)


def test_the_constant_prefix_is_identical_for_the_two_blocks(tmp_path: Path) -> None:
    """★ 恒定前缀的定义：**每个块逐字节相同**（这正是它进 system 的前提）。"""
    from tests.test_convert_three import _block_of, _tool_call

    task = _shaped_task(tmp_path, name="two-blocks")
    systems: dict[str, str] = {}

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            block = _block_of(messages) or "?"
            systems.setdefault(
                block, next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
            )
            if not any(m.get("role") == "tool" for m in messages):
                # ★ 先读一次来源：壳把"读过来源"当答案的前置条件（不是嘱托）。
                return ModelResponse(tool_calls=(_tool_call(f"r-{block}", "sources.snapshot", {"ref": "counts"}),))
            return ModelResponse(
                text=json.dumps(
                    {"ref": "counts", "columns": [{"name": "site", "dtype": "str"}], "rows": [{"site": "A"}], "done": True}
                )
            )

    run_task(task, client=_Client(), env={}, workspace=True, strategy="single")
    prefixes = {
        block: system.split(STATIC_MARKER, 1)[0]
        + STATIC_MARKER
        + system.split(STATIC_MARKER, 1)[1].split("\n", 1)[0]
        for block, system in systems.items()
    }
    assert len(prefixes) >= 2, sorted(prefixes)
    assert len(set(prefixes.values())) == 1


# --------------------------------------------------------------------------- #
# ★ 泛化自检：新代码里不许出现任何一个**具体任务**的名字
# --------------------------------------------------------------------------- #

#: 这七个词是"照着一道题写死"的指纹。新代码（含注释与 fixtures）里出现任何一个都算过拟合。
_ONE_TASK_NAMES = ("cik", "period", "amendment", "13f", "filing", "manager", "crowding")

#: `produce.py` 里属于本次改动的那些行（按它们引用到的名字认，不按行号）。
_WIRING = (
    "artifact_shape",
    "source_profiles",
    "deliverable_spec",
    "shape_cache",
    "_source_body",
    "_statement_text",
    "clip_section",
    "_SOURCE_PROFILE",
    "_DELIVERABLE_SPEC",
)


def _offenders(text: str, where: str) -> list[str]:
    lowered = text.lower()
    return [f"{where}: {name}" for name in _ONE_TASK_NAMES if name in lowered]


def test_the_new_code_names_no_single_task() -> None:
    """★ 红线：壳只算形状，不写领域规则 —— 新增代码里这七个词出现 **0** 次。

    ★ 唯一的例外是**判据自己的词表**（下面那一行）：它必须把七个词写出来才能扫它们，
      所以自检**跳过定义它的那一行**，其余每一行都要干净。
    """
    offenders = _offenders(
        (REPO / "src" / "bobanana6" / "assemble_impl" / "artifact_shape.py").read_text(encoding="utf-8"),
        "artifact_shape.py",
    )
    wiring = (REPO / "src" / "bobanana6" / "assemble_impl" / "produce.py").read_text(encoding="utf-8")
    for number, line in enumerate(wiring.splitlines(), start=1):
        if any(name in line for name in _WIRING):
            offenders += _offenders(line, f"produce.py:{number}")
    for number, line in enumerate(Path(__file__).read_text(encoding="utf-8").splitlines(), start=1):
        if "_ONE_TASK_NAMES = (" in line:
            continue
        offenders += _offenders(line, f"test_artifact_shape.py:{number}")
    assert offenders == [], offenders


# --------------------------------------------------------------------------- #
# ⑤ 泛化检验：换规模 / 换格式 / 换列名 —— 同一套判据
# --------------------------------------------------------------------------- #


def test_the_same_reader_works_on_every_supported_separator() -> None:
    for separator in ("\t", ",", ";", "|"):
        header = separator.join(("k", "v"))
        body = "\n".join(separator.join(row) for row in (("p", "1"), ("p", "1"), ("q", "2"), ("q", "2")))
        shape = table_shape(header + "\n" + body + "\n")
        assert shape is not None, separator
        assert shape["columns"] == 2 and shape["rows"] == 4, separator
        assert shape["grouped_columns"], separator


def test_a_table_over_the_row_cap_never_lies_about_it() -> None:
    rows = [f"{index % 3},{index}" for index in range(20050)]
    shape = table_shape("k,v\n" + "\n".join(rows) + "\n")
    assert shape is not None
    assert shape["rows_capped"] is True
    assert shape["rows"] <= 20000


def test_unfamiliar_column_names_are_read_the_same_way() -> None:
    """★ 判据不含任何一本词典：换成完全不认识的列名，读法一字不差。"""
    latin = table_shape("alpha,beta,gamma\n1,x,s\n2,x,s\n3,y,s\n")
    other = table_shape("站点,单位,数量\n1,x,s\n2,x,s\n3,y,s\n")
    assert latin is not None and other is not None
    assert latin["rows"] == other["rows"] == 3
    assert [item["distinct"] for item in latin["repeated_columns"]] == [
        item["distinct"] for item in other["repeated_columns"]
    ]


def test_the_same_text_twice_gives_the_same_shape() -> None:
    text = "a,b\n1,x\n1,x\n2,y\n"
    assert table_shape(text) == table_shape(text)
