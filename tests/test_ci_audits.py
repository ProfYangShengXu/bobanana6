"""三条「能被机制吸收」的 CI 检查（补充规格 §一 / §二 / §三）。

```
① 默认值审计（fail-open 扫描）  tools/default_audit.py       —— 每个默认值三选一
② 判据唯一性                    tools/criterion_uniqueness.py —— 变异判据 ⇒ 报告必须变
③ 传导率 A/B                    tools/conduction_ab.py        —— 内联 vs 工具背后的传递读数
```

★ 这一层测的是**三个工具本身能分辨**（能永远通过的守卫比没有守卫更坏），
以及它们的输出是**机读结构化**的（§五④）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools import default_audit

# --------------------------------------------------------------------------- #
# ① 默认值审计
# --------------------------------------------------------------------------- #

UNANNOTATED = """
from dataclasses import dataclass

@dataclass(frozen=True)
class W:
    limit: int = 8

def f(x: bool = True) -> None:
    ...
"""

FAIL_CLOSED = """
from dataclasses import dataclass

@dataclass(frozen=True)
class W:
    limit: int = 8  # blame: fail-closed — 省略即给一个有上限的值
"""

FAIL_OPEN_WITH_REASON = """
def f(x: bool = True):  # blame: fail-open — 省略即宣称"是"；判据不读它但读数会谎报
    ...
"""

FAIL_OPEN_WITHOUT_REASON = """
def f(x: bool = True):  # blame: fail-open
    ...
"""

WHITELISTED = """
def f(x: bool = True):  # blame: whitelist — 这个布尔只在打印里用，不进任何判据
    ...
"""

GET_FORM = """
def f(spec):
    return spec.get("min_rows", 0)
"""


def _scan(source: str) -> list[default_audit.Finding]:
    return default_audit.scan_source(source, path="src/bobanana6/fixture.py")


def test_an_unannotated_default_is_found_with_file_line_value_and_how_to_annotate() -> None:
    """★ 报错必须带 file:line + 当前默认值 + 该怎么标（规格 §一③④）。"""
    finding = next(f for f in _scan(UNANNOTATED) if f.symbol == "W.limit")
    assert (finding.line, finding.symbol, finding.default_src) == (6, "W.limit", "8")
    assert finding.bucket == default_audit.BUCKET_UNANNOTATED
    assert "fixture.py:6" in finding.how_to_annotate
    assert "W.limit" in finding.how_to_annotate
    assert "fail-closed" in finding.how_to_annotate and "fail-open" in finding.how_to_annotate


def test_a_fail_closed_marker_is_recognised() -> None:
    finding = next(f for f in _scan(FAIL_CLOSED) if f.symbol == "W.limit")
    assert finding.bucket == default_audit.BUCKET_FAIL_CLOSED


def test_a_fail_open_marker_carries_its_reason() -> None:
    [finding] = _scan(FAIL_OPEN_WITH_REASON)
    assert finding.bucket == default_audit.BUCKET_FAIL_OPEN
    assert "谎报" in finding.reason


def test_fail_open_without_a_reason_is_not_an_annotation() -> None:
    """★ 没有理由的 `fail-open` 等于没标 —— 否则标记就成了垃圾桶。"""
    [finding] = _scan(FAIL_OPEN_WITHOUT_REASON)
    assert finding.bucket == default_audit.BUCKET_UNANNOTATED


def test_a_whitelist_marker_is_its_own_bucket_and_keeps_its_reason() -> None:
    [finding] = _scan(WHITELISTED)
    assert finding.bucket == default_audit.BUCKET_WHITELIST
    assert "打印" in finding.reason


def test_the_mapping_get_form_is_found_too() -> None:
    """★ 「该有却没有」那一族：`spec.get("min_rows", 0)` 的 `0` 才是让约束恒过的默认值。"""
    [finding] = _scan(GET_FORM)
    assert finding.shape == default_audit.SHAPE_GET_DEFAULT
    assert finding.default_src == "0"
    assert "min_rows" in finding.symbol


def test_a_bool_true_default_is_enforced_anywhere_in_the_tree() -> None:
    """`= True` 是"省略即宣称是"这一形态 —— 不论在哪个包，都在强制范围内。"""
    [finding] = _scan(FAIL_OPEN_WITH_REASON)
    assert default_audit.enforced(finding)


def test_the_real_tree_has_nothing_unannotated_inside_the_enforced_scope() -> None:
    """★ 真仓库读数：强制范围内 0 条未标注（范围外由棘轮看着，见下一个用例）。"""
    findings = default_audit.scan_tree()
    left = [f for f in findings if default_audit.enforced(f) and f.bucket == default_audit.BUCKET_UNANNOTATED]
    assert left == [], [f"{f.location} {f.symbol} = {f.default_src}" for f in left]
    # 而且缩放不能是空转的：强制范围里确实有东西
    assert len([f for f in findings if default_audit.enforced(f)]) >= 20


def test_the_ratchet_refuses_growth_and_only_growth() -> None:
    """★ 范围外的默认值不静默通过：逐形态的未标注数只许变少。

    （`= True` 那一条在强制范围内，与棘轮无关 —— 所以这里挑 `numeric` 形态的字段来量棘轮。）
    """
    findings = [f for f in _scan(UNANNOTATED) if not default_audit.enforced(f)]
    assert [f.symbol for f in findings] == ["W.limit"]
    assert default_audit.check(findings, {"numeric": 0}) != []
    assert default_audit.check(findings, {"numeric": 1}) == []
    # 收紧不算问题（棘轮是"只许变少"）
    assert default_audit.check(findings, {"numeric": 5}) == []
    # ★ 而强制范围内的那一条，棘轮再松也照样报
    forced = [f for f in _scan(UNANNOTATED) if default_audit.enforced(f)]
    assert [f.symbol for f in forced] == ["f(x)"]
    assert default_audit.check(forced, {"bool_true": 99}) != []


def test_the_checked_in_ratchet_matches_the_tree() -> None:
    assert default_audit.RATCHET_PATH.is_file(), "the ratchet file must be reviewable in the diff"
    assert default_audit.check(default_audit.scan_tree(), default_audit.read_ratchet()) == []


def test_the_scanner_reports_a_machine_readable_document() -> None:
    """§五④：三条都必须是机读结构化输出，不许是散文。"""
    document = default_audit.as_document(default_audit.scan_tree())
    assert document["schema"] == "bobanana6.default-audit/1"
    assert set(document["counts"]) == {"buckets", "shapes", "enforced"}
    assert document["counts"]["buckets"]["enforced_total"] >= 20
    for finding in document["findings"]:
        assert set(finding) >= {"location", "symbol", "default", "bucket", "shape", "enforced"}


# --------------------------------------------------------------------------- #
# ② 判据唯一性
# --------------------------------------------------------------------------- #


def test_criterion_uniqueness_alarms_when_the_report_does_not_follow() -> None:
    """★ §五② 的反证：把"报告没变"喂给判定函数 ⇒ 必须报警，并说清是哪一份判据没跟着变。"""
    from tools import criterion_uniqueness

    same = {"block_layer": [], "blocks": {"T-01": "ok"}, "digest": "x"}
    problems = criterion_uniqueness.compare(same, dict(same))
    assert len(problems) == 1
    assert "second" in problems[0] and "hardcoded copy" in problems[0]
    # 变了就不报
    assert criterion_uniqueness.compare(same, {**same, "digest": "y"}) == []


def test_the_criterion_check_has_a_self_test_that_passes() -> None:
    from tools import criterion_uniqueness

    assert criterion_uniqueness.main(["--self-test"]) == 1  # 报警 ⇒ 返回值是 1（"有问题"）


def test_every_criterion_mutation_names_a_real_anchor_and_a_reader() -> None:
    """每个变异都必须钉在一个**真实存在且唯一**的判据行上，并有明确的期望读数。"""
    from tools import criterion_uniqueness

    assert len(criterion_uniqueness.MUTATIONS) >= 3
    for mutation in criterion_uniqueness.MUTATIONS:
        path = REPO / "src" / "bobanana6" / mutation.relative_path
        assert path.is_file(), mutation.relative_path
        assert path.read_text(encoding="utf-8").count(mutation.old) == 1, mutation.ident
        assert mutation.note and mutation.expect


# --------------------------------------------------------------------------- #
# ③ 传导率 A/B
# --------------------------------------------------------------------------- #


def test_the_conduction_table_covers_the_audited_points() -> None:
    """§五③：首批要量的四个点都在表里，且**真模型读数一律标"未跑"**。"""
    from tools import conduction_ab

    names = [point.name for point in conduction_ab.POINTS]
    assert names == ["statement", "upstream", "contract_deliverables", "ontology"]
    assert "未内联" in next(p for p in conduction_ab.POINTS if p.name == "contract_deliverables").note
    assert "没有专属工具" in next(p for p in conduction_ab.POINTS if p.name == "ontology").note


def test_the_conduction_table_is_machine_readable_and_never_invents_model_numbers() -> None:
    """§五④ + §五⑤：结构化输出；模型那部分**没跑就写未跑**，一个数字都不编。"""
    from tools import conduction_ab

    rows = [conduction_ab.measure(point) for point in conduction_ab.POINTS]
    for row in rows:
        assert set(row) >= {"point", "inline", "behind_tool", "delta", "model_reading"}
        assert "未跑" in row["model_reading"]
        assert set(row["inline"]) >= {"in_first_message_bytes", "fetch_calls", "reachable"}
        assert set(row["behind_tool"]) >= {"tool", "tool_payload_bytes", "fetch_calls", "reachable"}
    by_name = {row["point"]: row for row in rows}
    # ★ 本轮的两条确定性读数：题面与上游件**都已经内联**（A 臂可达、字节 > 0）
    assert by_name["statement"]["inline"]["in_first_message_bytes"] > 0
    assert by_name["upstream"]["inline"]["in_first_message_bytes"] > 0
    # ★ 还没内联的那一个：A 臂不可达、要走 1 次调用
    assert by_name["contract_deliverables"]["inline"]["reachable"] is False
    assert by_name["contract_deliverables"]["behind_tool"]["fetch_calls"] >= 1
    # ★ 本体：两臂都不可达（没有专属工具）——这是审计①第 2 条的形状
    assert by_name["ontology"]["behind_tool"]["reachable"] is False
    # 而且这张表是 JSON 可序列化的
    json.dumps([{k: v for k, v in row.items() if k != "blocks_ran"} for row in rows], default=str)


# --------------------------------------------------------------------------- #
# 三条都在 CI 分档里挂着
# --------------------------------------------------------------------------- #


def test_all_three_audits_are_wired_into_the_ci_tiers() -> None:
    """★ 检查做了但没挂进 CI = 没做（§三④ 的同一条纪律）。"""
    from bobanana6.services.ci import CI_TIERS

    commands = {tier.name: " ".join(tier.command) for tier in CI_TIERS}
    assert "tools/default_audit.py --check" in commands["defaults"]
    assert "tools/criterion_uniqueness.py" in commands["criterion"]
    for tier in CI_TIERS:
        if tier.name in {"defaults", "criterion"}:
            tool = tier.command[1]
            assert (REPO / tool).is_file(), tool


def test_the_three_tools_run_as_scripts(tmp_path: Path) -> None:
    """三条都必须能**当命令行跑**（CI 挂的是命令，不是 import）。"""
    for relative, extra in (
        ("tools/default_audit.py", "--check"),
        ("tools/criterion_uniqueness.py", "--self-test"),
        ("tools/conduction_ab.py", "--help"),
    ):
        completed = subprocess.run(
            [sys.executable, relative, extra],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            check=False,
        )
        # `--self-test` 的返回值是"发现了问题"（1），所以这里只断言它跑起来了、有输出
        assert completed.stdout or completed.stderr, relative
        assert "Traceback" not in completed.stderr, (relative, completed.stderr[-800:])


# --------------------------------------------------------------------------- #
# ① 那三处真的改了默认值（不是标注，是改行为）—— 契约必须被钉住
# --------------------------------------------------------------------------- #


def test_the_is_stale_defaults_are_now_fail_closed() -> None:
    """★ 审计 ① 的头号形态：`last_edit_at` / `edit_clock` 的省略必须**判过期**。

    改前两处默认都是 `0.0` ⇒ `is_stale` 恒假 ⇒ 旧验证永久有效（fail-open）。
    现在省略即视为"产物刚被改过" ⇒ 一律过期（fail-closed）。断言钉的就是这个方向。
    """
    import math

    from bobanana6.agentloop.gate import VerificationEvidence, evaluate
    from bobanana6.services.checks import BlockJudge

    assert BlockJudge.__dataclass_fields__["last_edit_at"].default == math.inf
    evidence = VerificationEvidence(
        command="true", output_digest="d", exit_code=0, attributable=True, created_at=1.0
    )
    # 省略 last_edit_at ⇒ 过期（而不是"通过"）
    assert evaluate(evidence, required_scope="full", last_edit_at=BlockJudge.__dataclass_fields__["last_edit_at"].default).status == "stale"

    from bobanana6.assemble_impl.runtime import BlockRuntime

    clock = BlockRuntime.__dataclass_fields__["edit_clock"].default
    assert clock(None) == math.inf  # type: ignore[arg-type]


def test_a_file_record_that_forgets_stored_never_claims_it_was_stored() -> None:
    """★ `FileRecord.stored` 的省略必须说"没存"，不许默认宣称存了（改前是 True）。"""
    from bobanana6.services.artifacts import FileRecord

    assert FileRecord.__dataclass_fields__["stored"].default is False
    forgotten = FileRecord("a.csv", "sha", 3, 1, inline=False)
    assert forgotten.stored is False
    assert forgotten.as_pointer()["stored"] is False
