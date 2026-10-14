"""研究来源在【契约】与【接线】那一层：声明怎么解、权限怎么拒。

零模型、确定性。被测的是：
  · `contract/parser.py` 的 `kind: research` 解析
  · `assemble_impl/runner.py::Runner._research` 的展开与两道拒
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bobanana6.assemble_impl.runner import RunRefused
from bobanana6.contract import ContractParseError, parse_contract
from bobanana6.core.model import Snapshot

# --------------------------------------------------------------------------- #
# 契约侧
# --------------------------------------------------------------------------- #

BASE = """
task_id: t
goal: 查点东西
deliverables:
  - {name: out.md, schema: {text: str}}
sources:
__SOURCES__
"""


def _parse(sources: str) -> Any:
    return parse_contract(BASE.replace("__SOURCES__", sources), source="<inline>")


def test_a_plain_source_stays_a_plain_source() -> None:
    """★ 缺省 `kind` = 现状：既有契约一个字不改，解析结果逐字不变。"""
    document = _parse("  - {ref: a, locator: input/a.md}")
    source = document.contract.sources[0]
    assert source.kind == ""
    assert source.question == ""
    assert source.seeds == ()


def test_a_research_source_keeps_its_question_and_seeds() -> None:
    document = _parse(
        "  - {ref: hunt, kind: research, question: \"大厂 agent 岗 招聘要求\", seeds: [\"https://a.example\", \"https://b.example\"]}"
    )
    source = document.contract.sources[0]
    assert source.kind == "research"
    assert source.question == "大厂 agent 岗 招聘要求"
    assert source.seeds == ("https://a.example", "https://b.example")
    # ★ 研究来源不需要 locator —— 位置正是它要找的东西
    assert source.locator == ""


def test_a_research_source_without_a_question_is_refused() -> None:
    """没有问题的"研究"是空转 —— 声明期就拒，别留到取数时才炸。"""
    with pytest.raises(ContractParseError) as excinfo:
        _parse("  - {ref: hunt, kind: research, seeds: [\"https://a.example\"]}")
    assert "question" in str(excinfo.value)


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ContractParseError) as excinfo:
        _parse("  - {ref: hunt, kind: telepathy, locator: x}")
    assert "kind" in str(excinfo.value)


def test_seeds_must_be_a_list_not_a_string() -> None:
    """`seeds: "https://a"` 是最容易写错的一种 —— 当场拒，不静默当成单元素。"""
    with pytest.raises(ContractParseError) as excinfo:
        _parse("  - {ref: hunt, kind: research, question: q, seeds: \"https://a.example\"}")
    assert "seeds" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 接线侧
# --------------------------------------------------------------------------- #


class _FakeResearcher:
    """装作研究者：不碰网络，只按 ref 造几条材料。"""

    def __init__(self, count: int = 2, boom: bool = False) -> None:
        self.count = count
        self.boom = boom
        self.seen: list[Any] = []

    def research(self, question: Any, budget: Any) -> tuple[Snapshot, ...]:
        self.seen.append((question, budget))
        if self.boom:
            raise RuntimeError("后端全都不通")
        out = [Snapshot(ref=question.ref, body_path=Path(f"/tmp/{question.ref}.md"), captured_at="T")]
        for index in range(1, self.count + 1):
            out.append(
                Snapshot(
                    ref=f"{question.ref}:{index}",
                    body_path=Path(f"/tmp/{question.ref}-{index}.md"),
                    captured_at="T",
                    backend="jina",
                    degraded=True,
                )
            )
        return tuple(out)


def _runner(sources_spec: str, researcher: Any, visibility: str = "open") -> Any:
    """造一个最小 Runner —— 只测 `_research`，所以别的依赖都塞最小替身。"""
    spec = (
        BASE.replace("__SOURCES__", sources_spec)
        + f"\ncontent_checks: {{none: true, visibility: {visibility}}}\n"
    )
    document = parse_contract(spec, source="<inline>")
    from bobanana6.assemble_impl.runner import Runner

    return Runner(
        document=document,
        ontology_port=None,  # type: ignore[arg-type]
        decomposer=None,  # type: ignore[arg-type]
        constraint_source=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        now="2026-10-14T12:00:00",
        epoch=lambda: 0.0,
        researcher=researcher,
    )


RESEARCH_SOURCE = (
    "  - {ref: hunt, kind: research, question: \"大厂 agent 岗 招聘要求\", seeds: [\"https://a.example\"]}"
)


def test_a_research_source_expands_into_a_manifest_plus_materials() -> None:
    """一条研究来源 ⇒ 一组快照（清单 + 材料），下游照旧只认识 Snapshot。"""
    runner = _runner(RESEARCH_SOURCE, _FakeResearcher(count=3))
    source = runner.document.contract.sources[0]

    found = runner._research(source)

    assert set(found) == {"hunt", "hunt:1", "hunt:2", "hunt:3"}
    assert found["hunt:1"].degraded is True, "降级标记必须原样带过来"


def test_the_question_and_seeds_reach_the_researcher_unchanged() -> None:
    """壳只做搬运：问题原文与种子**一字不改**地送到研究者手里。"""
    runner = _runner(RESEARCH_SOURCE, _FakeResearcher())
    researcher = runner.researcher

    runner._research(runner.document.contract.sources[0])

    question, _budget = researcher.seen[0]
    assert question.ref == "hunt"
    assert question.question == "大厂 agent 岗 招聘要求"
    assert question.seeds == ("https://a.example",)


def test_a_sealed_task_may_not_go_looking_on_the_network() -> None:
    """★★ 判分结果藏着不给块看、同时让它上网找 ⇒ 当场拒。

    那组合的实际效果是"绕过判分器去网上找答案"（13f 那类题的底层数据是公开的）。
    """
    runner = _runner(RESEARCH_SOURCE, _FakeResearcher(), visibility="sealed")
    source = runner.document.contract.sources[0]

    with pytest.raises(RunRefused) as excinfo:
        runner._research(source)
    assert "research_forbidden" in excinfo.value.codes  # type: ignore[attr-defined]


def test_the_default_visibility_is_the_refusing_one() -> None:
    """★ `visibility` 不声明时是 `sealed` ⇒ 默认就拒。fail-closed 的那一侧。"""
    from bobanana6.contract import parse_contract as parse

    document = parse(BASE.replace("__SOURCES__", RESEARCH_SOURCE), source="<inline>")
    assert document.content_checks.visibility == "sealed"
    runner = _runner(RESEARCH_SOURCE, _FakeResearcher())
    runner.document = document
    with pytest.raises(RunRefused) as excinfo:
        runner._research(document.contract.sources[0])
    assert "research_forbidden" in excinfo.value.codes


def test_no_researcher_wired_is_a_named_refusal() -> None:
    """部署没接研究者 ⇒ 具名拒，**不静默跳过** —— 跳过会让"没上网"读成"查过了但没有"。"""
    runner = _runner(RESEARCH_SOURCE, None)
    with pytest.raises(RunRefused) as excinfo:
        runner._research(runner.document.contract.sources[0])
    assert "research_unavailable" in excinfo.value.codes  # type: ignore[attr-defined]


def test_a_researcher_that_blows_up_is_recorded_not_fatal() -> None:
    """取数失败是一种**已归因**的结果，不是崩点：记进 fetch_failures，返回空。"""
    runner = _runner(RESEARCH_SOURCE, _FakeResearcher(boom=True))
    found = runner._research(runner.document.contract.sources[0])

    assert found == {}
    assert "hunt" in runner.fetch_failures
    assert "RuntimeError" in runner.fetch_failures["hunt"]


# --------------------------------------------------------------------------- #
# ★★ 接缝：取数产出必须**读得到**（这是实测踩出来的账，不是推导）
# --------------------------------------------------------------------------- #


def test_expanded_materials_are_authorized_for_the_blocks() -> None:
    """★★ 研究展开出来的 `hunt:1`…`hunt:N` 必须进授权表。

    实测背景：契约里只声明了 `hunt`（问题，不是位置），材料是**运行时**才展开的。
    授权表若只取契约声明的 sources，`sources.read {"ref":"hunt:1"}` 会被判
    `PermissionError: not authorized` ⇒ 块拿得到清单、**一份材料都读不到**
    ⇒ 只能去 ls 自己的目录乱找 ⇒ 整块 `failed: no_output`（真跑里三个块全中）。
    """
    runner = _runner(RESEARCH_SOURCE, _FakeResearcher(count=3))
    source = runner.document.contract.sources[0]

    snapshots = runner._research(source)
    authorized = {ref.ref for ref in runner._authorized(runner.document.contract, snapshots)}

    assert {"hunt", "hunt:1", "hunt:2", "hunt:3"} <= authorized


def test_authorization_is_still_a_whitelist() -> None:
    """★ 放宽的部分**只**是"本次真冻过的"：块自己编的名字照样进不来。"""
    two_sources = "  - {ref: plain, locator: input/plain.md}\n" + RESEARCH_SOURCE
    runner = _runner(two_sources, _FakeResearcher(count=1))

    snapshots = runner._research(runner.document.contract.sources[1])
    authorized = {ref.ref for ref in runner._authorized(runner.document.contract, snapshots)}

    assert "hunt:99" not in authorized, "没冻过的名字不该因为它像研究产出就放行"
    assert "whatever-i-made-up" not in authorized
    # ★ 契约声明的普通来源照旧在（研究来源的展开不该把它顶掉）
    assert "plain" in authorized


def test_the_runner_measures_each_frozen_source_by_line(tmp_path: Path) -> None:
    """尺寸事实由取数侧量、只留**尺寸**不留正文；行口径与 `sources.read` 相同。"""
    from bobanana6.sources import SnapshotStore

    store = SnapshotStore(root=tmp_path)
    # splitlines: ['alpha', 'bb', '', 'ccc'] —— 行是读工具的寻址单位，不是字符偏移。
    snapshot = store.put(
        ref="s1",
        body=b"alpha\nbb\n\nccc",
        source_kind="file",
        captured_at="2026-10-14T12:00:00",
        suffix=".md",
    )
    runner = _runner("  - {ref: s1}", _FakeResearcher(count=0))
    runner.store = store

    measured = runner._source_lines(runner.document.contract, {"s1": snapshot})

    assert measured == {"s1": (5, 2, 0, 3)}


def test_a_source_whose_size_cannot_be_read_keeps_no_entry(tmp_path: Path) -> None:
    """量不出来 ≠ 尺寸为 0：没有登记 ⇒ 切分器给那一份源单独一块，绝不合并它。"""
    from bobanana6.sources import SnapshotStore

    runner = _runner("  - {ref: s1}", _FakeResearcher(count=0))
    runner.store = SnapshotStore(root=tmp_path)
    missing = runner.document.contract.sources[0]
    orphan = type("S", (), {"ref": missing.ref, "body_path": tmp_path / "nope.md"})()

    measured = runner._source_lines(runner.document.contract, {"s1": orphan})  # type: ignore[dict-item]

    assert measured == {}
    assert "size probe failed" in runner.fetch_failures["s1"]
