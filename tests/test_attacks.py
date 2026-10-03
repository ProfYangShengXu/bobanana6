"""注入集：逐行开关、三步分开报、以及 A11 那条【会红】的验收证据。

★ 这里判的是**机制**：每条攻击能不能被独立开关、三步是不是分开的、
以及被注入的缺陷今天到底有没有被发现。**红了就是产出。**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acceptance.inject.attacks import (
    HIT,
    MISS,
    NA,
    load_attacks,
    render_gap_notes,
    render_table,
    run_attacks,
)
from acceptance.inject.injections import INJECTORS

ATTACKS = Path(__file__).resolve().parents[1] / "acceptance" / "stress" / "attacks.yaml"


# --------------------------------------------------------------------------- #
# 执行器本身：逐行开关 + 三步分开
# --------------------------------------------------------------------------- #


def test_every_row_in_the_table_has_an_injector() -> None:
    """★ 不许有「表里有、没人跑」的行 —— 那会让覆盖声明说谎。"""
    ids = {attack.id for attack in load_attacks()}
    assert ids == set(INJECTORS), f"unimplemented: {sorted(ids - set(INJECTORS))}"


def test_every_row_can_be_switched_off_individually(tmp_path: Path) -> None:
    """★ §14.2「每行一个可独立开关的攻击」—— 开关读的是**表里那一行**。"""
    import yaml

    table = yaml.safe_load(ATTACKS.read_text(encoding="utf-8"))
    table["attacks"][0]["enabled"] = False
    custom = tmp_path / "attacks.yaml"
    custom.write_text(yaml.safe_dump(table, allow_unicode=True), encoding="utf-8")

    rows = load_attacks(custom)
    assert rows[0].id == table["attacks"][0]["id"]
    assert rows[0].enabled is False, "the per-row switch in the table was ignored"
    assert all(row.enabled for row in rows[1:])

    outcomes = run_attacks(path=custom)
    switched_off = outcomes[0]
    assert switched_off.ran is False
    assert (switched_off.found, switched_off.blame_step, switched_off.action_step) == (NA, NA, NA)
    assert "disabled" in switched_off.evidence[0]


def test_a_row_with_no_injector_is_a_miss_not_a_pass() -> None:
    """★ 「表里有、没人跑」不许看起来像通过。"""
    outcomes = run_attacks(injectors={})
    assert outcomes, "the table has no rows at all"
    for outcome in outcomes:
        if not outcome.ran:
            continue
        assert outcome.found == MISS, outcome.attack.id
        assert "no injector" in outcome.evidence[0]
        assert outcome.blame_step == MISS and outcome.action_step == MISS


def test_the_report_keeps_the_three_steps_apart() -> None:
    """★ §14.5：发现对了但归因错、归因对了但动作错，两种失败处置完全相反。"""
    outcomes = run_attacks()
    table = render_table(outcomes)
    assert "① found" in table and "② blame" in table and "③ action" in table
    for forbidden in ("overall", "total", "score"):
        assert forbidden not in table.lower()
    # 每一行都按三步分开报：A07 的归因是 miss 而不是被「发现=hit」盖过去
    a07 = next(o for o in outcomes if o.attack.id.startswith("A07"))
    assert a07.found == HIT and a07.blame_step == MISS


def test_an_injection_that_breaks_is_a_miss_not_a_pass() -> None:
    from acceptance.inject.attacks import AttackOutcome, Observation

    attack = load_attacks()[0]
    broken = AttackOutcome(attack=attack, error="RuntimeError: boom")
    assert broken.observation is None
    assert broken.found == MISS and broken.blame_step == MISS and broken.action_step == MISS
    assert "raised" in broken.evidence[0]

    # ★ 真分支：注入器自己炸了 —— 只有经 run_attacks 才走得到（直接构造 AttackOutcome 走不到）
    def boom() -> Observation:
        raise RuntimeError("boom")

    row = attack.id
    outcome = next(o for o in run_attacks(only=[row], injectors={row: boom}) if o.attack.id == row)
    assert outcome.found == MISS and outcome.blame_step == MISS and outcome.action_step == MISS
    assert "raised" in outcome.evidence[0] and "RuntimeError" in outcome.evidence[0]


def test_only_runs_what_was_asked_for() -> None:
    outcomes = run_attacks(only=["A09-impossible"])
    assert [o.attack.id for o in outcomes] == ["A09-impossible"]


def test_the_gap_notes_name_the_rows_that_missed() -> None:
    """★ §14.3 的覆盖声明：没达到期望的攻击必须被点名。"""
    notes = render_gap_notes(run_attacks())
    assert "A07-same-symptom-different-layer" in notes
    assert "A11-provenance-content" in notes


# --------------------------------------------------------------------------- #
# 十条攻击的读数（当前真实状态；红了写着红）
# --------------------------------------------------------------------------- #


def _outcome(prefix: str):
    return next(o for o in run_attacks() if o.attack.id.startswith(prefix))


def test_a01_cross_block_dependency_is_declared() -> None:
    outcome = _outcome("A01")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence


def test_a03_real_conflict_is_recorded_not_failed() -> None:
    outcome = _outcome("A03")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence


def test_a05_transient_failure_is_retried_locally() -> None:
    outcome = _outcome("A05")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence


def test_a09_impossible_block_declines_instead_of_inventing() -> None:
    outcome = _outcome("A09")
    assert outcome.found == HIT and outcome.action_step == HIT, outcome.evidence


def test_a07_the_three_root_causes_get_the_same_blame_today() -> None:
    """★ 假归因，实测复现：工具坏 / 上下文没给到 / 判定器太严 → 三条都归成 tool。

    §14.2 的 `fail_if` 就是「三条给同一个 blame」。**这条是红的**，
    而且**不许为了让 A07 变绿去改 attribution**（本块只测不修）。
    等 attribution 修好，这条会变红成「不再是同一个 blame」→ 改成正式断言。
    """
    outcome = _outcome("A07")
    assert outcome.found == HIT  # 三条都注意到了
    assert outcome.blame_step == MISS, outcome.evidence  # ★ 但三条同一个 blame
    blames = outcome.observation.blames
    assert len(set(blames)) == 1


def test_a04_missing_and_zero_are_indistinguishable_today() -> None:
    """★ A04：空值与被填平的 0 在判定器眼里是同一个东西 —— 缺口，如实记。"""
    outcome = _outcome("A04")
    assert outcome.found == MISS and outcome.action_step == MISS
    assert "indistinguishable" in " ".join(outcome.evidence)


def test_a06_a_persistent_failure_refuses_the_run_instead_of_degrading() -> None:
    """★ A06：不会无限重试（fail_if 不触发），但出口是「前置门拒跑」而不是降级。"""
    outcome = _outcome("A06")
    assert outcome.found == HIT  # 停下来了，没有无限重试
    assert outcome.action_step == MISS, outcome.evidence
    assert "refusal" in " ".join(outcome.evidence)


def test_a08_the_global_invariant_cannot_see_a_multi_piece_assembly() -> None:
    """★ A08：`target="*"` 的全局不变量在多块装配上只会「看不见产物」。"""
    outcome = _outcome("A08")
    assert outcome.found == MISS and outcome.action_step == MISS
    assert "artifact for target '*' not found" in " ".join(outcome.evidence)


def test_a10_unit_consistency_only_compares_declarations() -> None:
    """★ A10：单位只比【声明】；候选里的值一个都不读，所以「归一」没有判定者。"""
    outcome = _outcome("A10")
    assert outcome.found == MISS and outcome.action_step == MISS
    assert "declarations only" in " ".join(outcome.evidence)


def test_a02_polysemy_is_detected_at_compare_time_not_blocked_at_L1() -> None:
    """★ A02：口径不一致**被发现**了（不是静默混用），但发现点在 compare 期 ——
    §14.2 的 `fail_if` 说的是「出现 DefinitionMismatch 说明没在 L-1 拦住」。"""
    outcome = _outcome("A02")
    assert outcome.found == HIT
    assert outcome.action_step == MISS, outcome.evidence
    assert "L-1" in " ".join(outcome.evidence)


# --------------------------------------------------------------------------- #
# ★★ A11：溯源引用的真实性没人校验 —— 本条的验收证据【现在必须是红的】
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason=(
        "★ 已知缺口（见 溯源缺口-引用真实性未校验-20261003.md）：core/provenance.py 只查 "
        "snapshot_ref 这个名字在不在注册表，从不打开快照，所以引用了不存在的行也算 coverage=1.0。"
        "这条 xfail 就是缺口活着的证据：修好之后它会 XPASS → strict 会让它变红 → 逼着把它改成正式断言。"
    ),
)
def test_a11_citing_rows_that_do_not_exist_is_caught() -> None:
    """3 行快照 + 30 行候选（每行都引用它）→ 期望当场抓住 `row=3..29` 不存在。"""
    from bobanana6.core.model import Artifact, Candidate, Column, Provenance, Snapshot
    from bobanana6.core.provenance import validate_points

    rows = 30
    snapshot = Snapshot(ref="panel-a", body_path=Path("/nonexistent/panel-a.csv"), captured_at="2026-10-03T00:00:00")
    candidate = Candidate(
        subtask_id="T-04",
        source_id="panel-a",
        artifacts=(Artifact(name="panel.csv", columns=(Column(name="obligor", dtype="str"),), rows=tuple({"obligor": f"X{i}"} for i in range(rows))),),
        provenance=tuple(
            Provenance(value_ref=f"panel.csv:row={index}", snapshot_ref="panel-a", locator="L") for index in range(rows)
        ),
    )
    required = tuple(f"panel.csv:row={index}" for index in range(rows))
    report = validate_points(required, candidate.provenance, {"panel-a": snapshot})
    assert not report.ok, (
        "the cited rows 3..29 do not exist in a 3-row snapshot, yet the chain reports full coverage"
    )


def test_a11_the_executor_reports_the_gap_as_a_miss() -> None:
    """★ 执行器那一侧：A11 今天必须是 miss（与上面那条 xfail 是同一件事的两个读数）。"""
    outcome = _outcome("A11")
    assert outcome.found == MISS
    assert "30" in " ".join(outcome.evidence)


def test_a_pure_merge_block_can_cite_any_registered_snapshot() -> None:
    """★★ 缺口看门狗（夜报 #13 报的「纯合并块假 ref」）。

    纯合并块 `authorized_sources` 为空，`produce.py` 里那条 `unknown_ref` 守卫
    （`elif authorized_sources and snapshot_ref not in authorized_sources`）**整个不生效**：
    模型写什么 ref 都收下，而且它会成为 `candidate.source_id`。只要那个名字在快照
    注册表里，链就报 coverage=1.0 / 无 breaks —— ref 证明的只是「这个名字存在」，
    不是「这些行来自它」；换来一个没注册的名字才会被抓。等「合并块只能引用它真正
    拉过的上游」落地，本测试会红 → 升级成正式断言。
    """
    from bobanana6.assemble_impl.produce import parse_candidate
    from bobanana6.core.model import Snapshot, Subtask
    from bobanana6.core.provenance import validate_points

    merge = Subtask(
        id="T-04",
        goal="merge the parts",
        inputs=("part-panel-a.csv",),
        output="panel.csv",
        contract_ref="panel",
        axis="field",
        mode="write",
    )
    candidate = parse_candidate(
        '{"ref": "panel-b", "rows": [{"obligor": "A-001"}]}',
        subtask=merge,
        source_id="T-04",
        authorized_sources=(),  # ★ 纯合并：没有任何被授权的快照
    )
    # 它只拉过 part-panel-a.csv，却说自己来自 panel-b —— 没人质疑
    assert candidate.source_id == "panel-b"
    assert {p.snapshot_ref for p in candidate.provenance} == {"panel-b"}

    registry = {
        "panel-a": Snapshot(ref="panel-a", body_path=Path("/nonexistent/panel-a.csv"), captured_at="T0"),
        "panel-b": Snapshot(ref="panel-b", body_path=Path("/nonexistent/panel-b.csv"), captured_at="T0"),
    }
    required = tuple(p.value_ref for p in candidate.provenance)
    assert validate_points(required, candidate.provenance, registry).ok, "名字在注册表里 → 链判为可达"

    # 反证：同一条链只在名字【没注册】时才断 —— 今天查的就只有名字
    broken = validate_points(required, candidate.provenance, {"panel-a": registry["panel-a"]})
    assert not broken.ok and broken.breaks[0].missing_snapshot == "panel-b"
