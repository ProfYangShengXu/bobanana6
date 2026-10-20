"""The fifth gate: intra-block coverage (``item_coverage``).

What this file pins, and why each one is worth a test rather than a comment:

  * a miss is **named** -- "缺：智谱、商汤、Replit", not "coverage 0.22";
  * alias folding is symmetric (``月之暗面 (Kimi)`` == ``月之暗面``) and the
    normalisation order is asserted (NFKC before bracket stripping, because NFKC
    manufactures brackets from single glyphs);
  * the checker never raises -- a miss is a decidable failure, not a crash;
  * a short checklist is **refused by name** before it can judge anything;
  * the obligation wording is the measured one, verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import fakes
import pytest

from bobanana6.agentloop.brief import render_coverage_obligation
from bobanana6.assemble_impl import RunRefused
from bobanana6.assemble_impl.runner import Runner
from bobanana6.contract import compile_bundle, parse_contract
from bobanana6.contract.parser import COVERAGE_FAILURE_CODE, ContractParseError
from bobanana6.core.checkers.base import CheckContext
from bobanana6.core.checkers.item_coverage import (
    check_coverage,
    item_present,
    normalize,
    undercount,
    ungrounded,
)
from bobanana6.core.kernel import admit
from bobanana6.core.model import Artifact, Snapshot
from bobanana6.core.ports import Checklist
from bobanana6.sources.prefetch import checklist_problems, parse_count, parse_names

#: 23 家公司 —— 与实测材料同规模。`missing` 的名单必须能一个一个点出来。
TWENTY_THREE = (
    "字节跳动", "阿里巴巴", "腾讯", "百度", "美团", "快手", "小红书", "华为",
    "蚂蚁集团", "月之暗面", "智谱", "商汤", "MiniMax", "DeepSeek", "阶跃星辰",
    "百川智能", "零一万物", "微软 Azure", "OpenAI", "Anthropic", "Glean", "Vercel", "Replit",
)

FIVE_ROWS = TWENTY_THREE[:5]


def _table(names) -> str:
    lines = ["| 公司 | 岗位 | 年薪 |", "|------|------|------|"]
    lines += [f"| {name} | 工程师 | 40-60K |" for name in names]
    return "\n".join(lines)


def _artifact(text: str, name: str = "output/jd-matrix.md") -> Artifact:
    return Artifact(name=name, text=text)


# --------------------------------------------------------------------------- #
# ① 漏项必须具名
# --------------------------------------------------------------------------- #


def test_a_miss_names_every_missing_item() -> None:
    result = check_coverage(_table(FIVE_ROWS), TWENTY_THREE, ("公司",), 0.65)
    assert result.total == 23
    assert result.covered == 5
    assert result.missing == TWENTY_THREE[5:]
    assert len(result.missing) == 18
    assert result.ratio == pytest.approx(5 / 23)
    assert result.passed is False
    fact = result.fact()
    # ★ 要求逐字：18 个名字一个都不许省，也不许只报一句"覆盖率不足"
    for name in TWENTY_THREE[5:]:
        assert name in fact, name
    assert "coverage_missing" in fact
    # ★ 判据是"不能只报覆盖率"：名字必须列全，而不是把 0.22 当结论。
    assert fact.count("、") >= 17
    assert "缺 18 项" in fact


def test_a_full_table_passes() -> None:
    result = check_coverage(_table(TWENTY_THREE), TWENTY_THREE, ("公司",), 0.65)
    assert (result.total, result.covered, result.ratio, result.passed) == (23, 23, 1.0, True)
    assert result.missing == ()


def test_the_threshold_is_the_pass_line() -> None:
    names = TWENTY_THREE[:16]  # 16/23 = 0.6957
    assert check_coverage(_table(names), TWENTY_THREE, (), 0.65).passed is True
    assert check_coverage(_table(names), TWENTY_THREE, (), 0.70).passed is False


# --------------------------------------------------------------------------- #
# ② 别名归一后比对
# --------------------------------------------------------------------------- #


def test_a_bracketed_alias_suffix_is_folded_both_ways() -> None:
    assert item_present("月之暗面", _table(("月之暗面 (Kimi)",))) is True
    assert item_present("Kimi", _table(("月之暗面 (Kimi)",))) is True
    assert item_present("月之暗面", _table(("月之暗面Kimi",))) is True
    assert item_present("Azure Microsoft", _table(("Azure, Microsoft",))) is True


def test_normalisation_runs_nfkc_before_dropping_brackets() -> None:
    # ★ NFKC 会把全角括号折成半角，也会把单字形造出括号（㈱ → (株)）——
    #   顺序反过来就有一半的括号匹配不上。
    assert normalize("（Kimi）") == normalize("(Kimi)")
    assert normalize("月之暗面（Kimi）") == "月之暗面"
    assert normalize("㈱") == ""  # NFKC 造出的 (株) 随后被当作别名后缀剥掉
    assert normalize("ＡＢＣ") == "abc"


def test_fuzzy_ratio_is_deliberately_not_used() -> None:
    """0.8 的模糊阈值会把正确项判成漏项 —— 这条把"我们不用它"钉住。

    ``SequenceMatcher.ratio()`` for the real alias pair is 0.571 / 0.533, both
    below the 0.8 that fuzzy matchers ship with. Bracket folding solves it
    exactly, so a fuzzy scorer would trade a certain error for an uncertain one.
    """
    from difflib import SequenceMatcher

    assert SequenceMatcher(None, "月之暗面(Kimi)", "月之暗面").ratio() < 0.8
    assert item_present("月之暗面", "月之暗面(Kimi)") is True


def test_only_the_named_column_is_counted() -> None:
    """正文里提到一家公司 ≠ 这一家有一行。表头命中轴名时只读那一列。"""
    prose = "本报告调研了字节跳动、阿里巴巴、腾讯、百度、美团、快手。\n\n" + _table(FIVE_ROWS)
    result = check_coverage(prose, TWENTY_THREE, ("公司",), 0.65)
    assert result.mode == "column"
    assert result.covered == 5
    assert check_coverage(prose, TWENTY_THREE, (), 0.65).mode == "text"


# --------------------------------------------------------------------------- #
# ③ 不抛异常
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "checklist", "alias", "threshold"),
    [
        (None, TWENTY_THREE, None, 0.65),
        (b"bytes", (None, 7, "字节跳动"), "公司", None),
        ("", (), (), "not-a-number"),
        (_table(FIVE_ROWS), TWENTY_THREE, 3, 2.0),
        (_table(FIVE_ROWS), TWENTY_THREE, ("公司",), -1),
    ],
)
def test_the_checker_never_raises_on_garbage(text, checklist, alias, threshold) -> None:
    result = check_coverage(text, checklist, alias, threshold)
    assert isinstance(result.passed, bool)


def test_an_empty_checklist_scores_zero_not_full() -> None:
    """没有项 = 没有依据，不是全覆盖。"""
    result = check_coverage(_table(TWENTY_THREE), (), (), 0.65)
    assert (result.total, result.covered, result.ratio, result.passed) == (0, 0, 0.0, False)
    assert "无依据不等于全覆盖" in result.fact()


# --------------------------------------------------------------------------- #
# ④ 清单可信性：接地 + 独立计数
# --------------------------------------------------------------------------- #


def test_an_invented_item_is_not_grounded() -> None:
    assert ungrounded(("字节跳动", "不存在的公司"), "字节跳动和腾讯的 JD") == ("不存在的公司",)
    assert ungrounded(TWENTY_THREE, "字节跳动") == TWENTY_THREE[1:]


def test_undercount_uses_the_independent_count_not_the_checklist() -> None:
    assert undercount(TWENTY_THREE[:8], 23) is True  # 8 < 0.8*23
    assert undercount(TWENTY_THREE, 23) is False
    # ★ 没有独立读数（0）= 不是欠数的证据；凭它拒会让每个离线跑都失败。
    assert undercount((), 0) is False


def test_a_short_checklist_is_refused_by_name() -> None:
    assert checklist_problems((), "nothing", 3) == ("checklist_undecided",)
    codes = checklist_problems(TWENTY_THREE[:8], " ".join(TWENTY_THREE), 23)
    assert "checklist_undercount" in codes


def test_a_checklist_item_absent_from_the_material_is_named() -> None:
    codes = checklist_problems(("字节跳动", "凭空出现的公司"), "字节跳动 腾讯", 2)
    assert "checklist_ungrounded" in codes


def test_the_reply_parsers_survive_prose_and_fences() -> None:
    assert parse_names('```json\n["字节跳动", "腾讯"]\n```') == ("字节跳动", "腾讯")
    assert parse_names('{"count": 2, "names": ["字节跳动", "腾讯"]}') == ("字节跳动", "腾讯")
    assert parse_names("- 字节跳动\n2. 腾讯\n") == ("字节跳动", "腾讯")
    assert parse_count('{"count": 23, "names": []}') == 23
    assert parse_count("一共 17 个不同的公司") == 17
    assert parse_count("no number here") == 0


# --------------------------------------------------------------------------- #
# ⑤ 契约：声明了才算，声明了就必须成形
# --------------------------------------------------------------------------- #


_BASE = """task_id: t
goal: g
deliverables:
  - name: output/jd-matrix.md
    schema: {text: str}
sources:
  - {ref: hunt, locator: input/a.md}
"""


def test_a_contract_without_coverage_parses_exactly_as_before() -> None:
    document = parse_contract(_BASE)
    assert document.coverage == ()
    assert all(c.kind != "item_coverage" for c in document.hard)


def test_a_declared_coverage_keeps_its_fields() -> None:
    document = parse_contract(
        _BASE
        + """coverage:
  axis: company
  source: hunt
  alias: [公司, 厂商]
  threshold: 0.7
"""
    )
    (spec,) = document.coverage
    assert spec.deliverable == "output/jd-matrix.md"
    assert spec.axis == "company"
    assert spec.source == "hunt"
    assert spec.alias == ("公司", "厂商")
    assert spec.threshold == 0.7


@pytest.mark.parametrize(
    ("fragment", "needle"),
    [
        ("coverage:\n  source: hunt\n", "$.coverage.axis"),
        ("coverage:\n  axis: company\n", "$.coverage.source"),
        ("coverage:\n  axis: company\n  source: nope\n", "not a declared source"),
        ("coverage:\n  axis: company\n  source: hunt\n  colour: red\n", "unknown keys"),
        ("coverage:\n  axis: company\n  source: hunt\n  threshold: 1.5\n", "threshold"),
        ("coverage:\n  axis: company\n  source: hunt\n  alias: 公司\n", "alias"),
        ("coverage:\n  axis: company\n  source: hunt\n  deliverable: output/nope.md\n", "not a declared deliverable"),
        ("coverage: 7\n", "mapping or a list"),
        ("coverage: []\n", "declared but empty"),
    ],
)
def test_malformed_coverage_is_refused_by_name(fragment: str, needle: str) -> None:
    with pytest.raises(ContractParseError) as raised:
        parse_contract(_BASE + fragment)
    assert needle in str(raised.value)
    assert "coverage_malformed" in str(raised.value)


# --------------------------------------------------------------------------- #
# ⑥ 判定：块级 verdict 里出现具名失败
# --------------------------------------------------------------------------- #


def _bundle_and_context(checklist):
    document = parse_contract(_BASE + "coverage:\n  axis: company\n  source: hunt\n")
    plan = fakes.plan((fakes.subtask("T-01", output="output/jd-matrix.md"),))
    bundle = compile_bundle(document, plan)
    return bundle, CheckContext(checklists={"output/jd-matrix.md": checklist})


def test_compile_bundle_synthesizes_the_fifth_gate_without_touching_the_four() -> None:
    document = parse_contract(_BASE + "coverage:\n  axis: company\n  source: hunt\n")
    plan = fakes.plan((fakes.subtask("T-01", output="output/jd-matrix.md"),))
    bundle = compile_bundle(document, plan)
    kinds = sorted(constraint.kind for constraint in bundle.hard)
    # ★ 第五道门是**新增**的：既有的 presence 该补还补（AUTO 一条不少）。
    assert "item_coverage" in kinds
    assert "presence" in kinds
    gate = next(constraint for constraint in bundle.hard if constraint.kind == "item_coverage")
    assert gate.target == "output/jd-matrix.md"
    assert gate.id.startswith(COVERAGE_FAILURE_CODE)


def test_a_short_artifact_fails_the_block_verdict_with_names() -> None:
    bundle, context = _bundle_and_context(TWENTY_THREE)
    candidate = fakes.candidate(
        artifacts=(_artifact(_table(FIVE_ROWS)),),
    )
    verdict = admit(bundle, candidate, context)
    assert verdict.ok is False
    gate = next(cid for cid in verdict.failed if cid.startswith(COVERAGE_FAILURE_CODE))
    evidence = verdict.evidence[gate]
    for name in TWENTY_THREE[5:]:
        assert name in evidence, name
    assert "缺" in evidence


def test_a_full_artifact_passes_the_block_verdict() -> None:
    bundle, context = _bundle_and_context(TWENTY_THREE)
    candidate = fakes.candidate(artifacts=(_artifact(_table(TWENTY_THREE)),))
    verdict = admit(bundle, candidate, context)
    assert not [cid for cid in verdict.failed if cid.startswith(COVERAGE_FAILURE_CODE)]


def test_a_missing_checklist_is_a_named_refusal_not_a_pass() -> None:
    """拿不到分母 ⇒ 具名拒。静默通过会把"没查"记成"查过了没问题"。"""
    bundle, _ = _bundle_and_context(TWENTY_THREE)
    verdict = admit(bundle, fakes.candidate(artifacts=(_artifact(_table(FIVE_ROWS)),)), CheckContext())
    gate = next(cid for cid in verdict.failed if cid.startswith(COVERAGE_FAILURE_CODE))
    assert "no frozen checklist" in verdict.evidence[gate]


def test_a_block_without_the_artifact_is_named() -> None:
    bundle, context = _bundle_and_context(TWENTY_THREE)
    verdict = admit(bundle, fakes.candidate(artifacts=()), context)
    gate = next(cid for cid in verdict.failed if cid.startswith(COVERAGE_FAILURE_CODE))
    assert "no artifact named" in verdict.evidence[gate]


# --------------------------------------------------------------------------- #
# ⑦ 覆盖义务的措辞（逐字，不许优化）
# --------------------------------------------------------------------------- #


def test_the_obligation_wording_is_the_measured_one() -> None:
    text = render_coverage_obligation(("字节跳动", "腾讯"))
    assert text.splitlines()[0] == "★ 这份产物必须逐项覆盖以下清单（共 2 项）："
    assert "   字节跳动" in text
    assert "- 清单上每一项都必须出现在产物里" in text
    assert "- 某一项信息不足时，那一格写「材料未提及」，但这一行要保留" in text
    assert "- 写完自己数一遍：清单 2 项，产物交代了几项" in text
    assert render_coverage_obligation(()) == ""


# --------------------------------------------------------------------------- #
# ⑧ 壳：预扫描 → 落盘冻结 → 具名拒
# --------------------------------------------------------------------------- #


class _Port:
    def __init__(self, checklist: Checklist) -> None:
        self.checklist = checklist
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def enumerate(self, axis, materials, alias=()):
        self.calls.append((axis, tuple(snapshot.ref for snapshot in materials)))
        return self.checklist


def _shell(tmp_path: Path, port, *, body: str = "字节跳动 腾讯 阿里巴巴") -> Runner:
    document = parse_contract(
        _BASE.replace("locator: input/a.md", "locator: input/a.md")
        + "coverage:\n  axis: company\n  source: hunt\n  alias: [公司]\n"
    )
    (tmp_path / "material.md").write_text(body, encoding="utf-8")
    runner = Runner(
        document=document,
        ontology_port=object(),
        decomposer=object(),
        constraint_source=object(),
        runtime=object(),
        fetcher=object(),
        store=object(),
        now="2026-10-20T00:00:00",
        epoch=lambda: 0.0,
        report_dir=tmp_path / "reports",
        workspace=tmp_path / "workspace",
        coverage_port=port,
    )
    return runner


def _plan():
    return fakes.plan((fakes.subtask("T-01", output="output/jd-matrix.md"),))


def test_the_shell_freezes_the_checklist_before_any_block_runs(tmp_path: Path) -> None:
    runner = _shell(tmp_path, _Port(Checklist(items=("字节跳动", "腾讯"), expected=2, axis="company")))
    runner._prefetch_checklists(runner.document.contract, _plan(), {"hunt": Snapshot(
        ref="hunt", body_path=tmp_path / "material.md", captured_at="2026-10-20T00:00:00")})
    assert runner.checklists["output/jd-matrix.md"] == ("字节跳动", "腾讯")
    frozen = tmp_path / "checklist" / "output__jd-matrix.md.json"
    assert frozen.is_file(), "the checklist must land on disk before the blocks start"
    payload = json.loads(frozen.read_text(encoding="utf-8"))
    assert payload["items"] == ["字节跳动", "腾讯"]
    assert payload["digest"] and payload["axis"] == "company" and payload["source"] == "hunt"
    # ★ 登记进 frozen_paths：工作区里的**清单台账**必须出现在"谁都不许写"的声明里。
    #   ★ 只能登记工作区**相对**路径：绝对路径会让 `may_write` 的归一化直接抛
    #   `WorkspaceError`，于是一块想写自己的草稿（写区之外的合法动作）就整跑 abort。
    manifest = tmp_path / "workspace" / ".bobanana6" / "checklist.json"
    assert manifest.is_file()
    assert runner._checklist_paths() == (".bobanana6/checklist.json",)
    ledger = json.loads(manifest.read_text(encoding="utf-8"))
    assert ledger["checklists"]["output/jd-matrix.md"]["digest"] == payload["digest"]


def test_a_short_checklist_refuses_the_run_and_never_judges(tmp_path: Path) -> None:
    short = Checklist(items=("字节跳动", "腾讯"), expected=23, axis="company")
    runner = _shell(tmp_path, _Port(short))
    with pytest.raises(RunRefused) as raised:
        runner._prefetch_checklists(runner.document.contract, _plan(), {"hunt": Snapshot(
            ref="hunt", body_path=tmp_path / "material.md", captured_at="2026-10-20T00:00:00")})
    assert "checklist_undercount" in raised.value.codes
    assert raised.value.gate == "coverage"
    assert runner.checklists == {}, "a refused checklist must not be left where judgment can read it"


def test_an_ungrounded_checklist_refuses_the_run(tmp_path: Path) -> None:
    invented = Checklist(items=("字节跳动", "凭空公司"), expected=2, axis="company")
    runner = _shell(tmp_path, _Port(invented))
    with pytest.raises(RunRefused) as raised:
        runner._prefetch_checklists(runner.document.contract, _plan(), {"hunt": Snapshot(
            ref="hunt", body_path=tmp_path / "material.md", captured_at="2026-10-20T00:00:00")})
    assert "checklist_ungrounded" in raised.value.codes


def test_a_declared_coverage_without_a_port_is_refused(tmp_path: Path) -> None:
    runner = _shell(tmp_path, None)
    with pytest.raises(RunRefused) as raised:
        runner._prefetch_checklists(runner.document.contract, _plan(), {})
    assert "checklist_unavailable" in raised.value.codes


def test_a_missing_source_snapshot_is_refused(tmp_path: Path) -> None:
    runner = _shell(tmp_path, _Port(Checklist(items=("字节跳动",), expected=1)))
    with pytest.raises(RunRefused) as raised:
        runner._prefetch_checklists(runner.document.contract, _plan(), {})
    assert "checklist_source_missing" in raised.value.codes


def test_the_obligation_only_reaches_the_block_that_owns_the_deliverable(tmp_path: Path) -> None:
    runner = _shell(tmp_path, _Port(Checklist(items=("字节跳动", "腾讯"), expected=2)))
    runner.checklists = {"output/jd-matrix.md": ("字节跳动", "腾讯")}
    mine = fakes.subtask("T-02", output="output/jd-matrix.md")
    other = fakes.subtask("T-03", output="output/interview-bank.md")
    assert "字节跳动" in runner._coverage_instruction(mine)
    assert runner._coverage_instruction(other) == ""


# --------------------------------------------------------------------------- #
# ⑨ 审计查出来的四个"会咬人"点 —— 每条一个回归
# --------------------------------------------------------------------------- #


def test_a_header_that_merely_contains_the_axis_is_not_preferred() -> None:
    """`alias: [公司]` 不能命中 `公司规模` —— 读错列，覆盖率要么恒 0 要么恒 1。"""
    from bobanana6.core.checkers.item_coverage import item_column

    text = "| 公司规模 | 公司 | 营收 |\n|---|---|---|\n| big | 字节跳动 | 1 |"
    assert item_column(text, "公司", ("公司",)) == ("字节跳动",)


def test_a_header_only_table_does_not_mask_the_real_one() -> None:
    """第一张带轴表头的表没有数据行 ⇒ 继续找下一张，不要判 0/N。"""
    text = (
        "| 公司 | 备注 |\n|---|---|\n\n"
        "| 公司 | 营收 |\n|---|---|\n| 字节跳动 | 1 |"
    )
    result = check_coverage(text, ("字节跳动",), ("公司",), 0.65, axis="公司")
    assert result.mode == "column"
    assert (result.covered, result.passed) == (1, True)


def test_frozen_paths_never_carry_an_absolute_path(tmp_path: Path) -> None:
    """绝对路径进 `frozen_paths` 会让 `may_write` 抛 WorkspaceError，整跑 abort。"""
    from bobanana6.services.workspace import may_write

    runner = _shell(tmp_path, _Port(Checklist(items=("字节跳动", "腾讯"), expected=2)))
    snapshots = {"hunt": Snapshot(
        ref="hunt", body_path=tmp_path / "material.md", captured_at="2026-10-20T00:00:00")}
    runner._prefetch_checklists(runner.document.contract, _plan(), snapshots)
    paths = runner._checklist_paths()
    assert paths == (".bobanana6/checklist.json",)
    assert not any(Path(name).is_absolute() for name in paths)
    # 写区之外的草稿写（frozen_paths 放开时要支持的那件事）必须仍然放行。
    assert may_write("_code/x.py", ("output/jd-matrix.md",), paths) is True
    # 而台账本身仍然写不了。
    assert may_write(".bobanana6/checklist.json", ("output/jd-matrix.md",), paths) is False


def test_a_research_source_enumerates_its_material_not_the_manifest(tmp_path: Path) -> None:
    """`source: hunt` 的材料是 `hunt:1..N`；`hunt` 本身是检索清单（种子地址表）。"""
    from bobanana6.assemble_impl.runner import Runner

    manifest = Snapshot(ref="hunt", body_path=tmp_path / "m.md", captured_at="2026-10-20T00:00:00")
    one = Snapshot(ref="hunt:1", body_path=tmp_path / "1.md", captured_at="2026-10-20T00:00:00")
    two = Snapshot(ref="hunt:2", body_path=tmp_path / "2.md", captured_at="2026-10-20T00:00:00")
    got = Runner._coverage_materials("hunt", {"hunt": manifest, "hunt:1": one, "hunt:2": two})
    assert [snapshot.ref for snapshot in got] == ["hunt:1", "hunt:2"]
    # 载体来源没有 `<ref>:i` ⇒ 退回 `<ref>` 本身（它就是材料）。
    assert [s.ref for s in Runner._coverage_materials("jd", {"jd": manifest})] == ["hunt"]


def test_an_invented_item_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    """剔掉编的项是对的；"它编过"必须留痕，否则读数会说成"清单只是短"。"""
    port = _Port(Checklist(items=("字节跳动",), expected=1, invented=("凭空公司",)))
    runner = _shell(tmp_path, port)
    snapshots = {"hunt": Snapshot(
        ref="hunt", body_path=tmp_path / "material.md", captured_at="2026-10-20T00:00:00")}
    runner._prefetch_checklists(runner.document.contract, _plan(), snapshots)
    payload = json.loads((tmp_path / "checklist" / "output__jd-matrix.md.json").read_text(encoding="utf-8"))
    assert payload["invented"] == ["凭空公司"]
    events = [event for event in runner.event_log.events if event.detail.get("gate") == "coverage"]
    assert events and events[-1].detail["invented"] == ["凭空公司"]


def test_a_truncated_material_is_recorded(tmp_path: Path) -> None:
    port = _Port(Checklist(items=("字节跳动",), expected=1, truncated=True))
    runner = _shell(tmp_path, port)
    snapshots = {"hunt": Snapshot(
        ref="hunt", body_path=tmp_path / "material.md", captured_at="2026-10-20T00:00:00")}
    runner._prefetch_checklists(runner.document.contract, _plan(), snapshots)
    payload = json.loads((tmp_path / "checklist" / "output__jd-matrix.md.json").read_text(encoding="utf-8"))
    assert payload["truncated"] is True
